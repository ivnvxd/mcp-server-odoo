"""MCP Server implementation for Odoo.

This module provides the MCPServer that exposes Odoo data
and functionality through the Model Context Protocol.
"""

import asyncio
import contextlib
import time
from typing import Any, Dict, Optional, Tuple

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .access_control import AccessController
from .config import OdooConfig, get_config
from .error_handling import (
    ConfigurationError,
    ErrorContext,
    MCPConnectionError,
    error_handler,
)
from .error_sanitizer import ErrorSanitizer
from .logging_config import get_logger, logging_config, perf_logger
from .odoo_connection import OdooConnection, OdooConnectionError, OdooUnreachableError
from .performance import PerformanceManager
from .resources import register_resources
from .tools import register_tools
from .user_context import build_user_context, usage_guidance

# Set up logging
logger = get_logger(__name__)


def _split_allowed_host(entry: str) -> Tuple[str, Optional[str]]:
    """Split one ODOO_MCP_ALLOWED_HOSTS entry into ``(host, port)``.

    A naive ``split(":")`` mangles IPv6 in both directions: ``[::1]:8000``
    yields the host ``"["``, and a bare ``::1`` looks like it already carries
    a port, so it never gets the ``:*`` wildcard it needs. Both produce an
    allowlist that rejects the very host the operator allowlisted.

    Bare IPv6 literals are normalized to bracket form because that is what a
    Host header and a URL authority actually carry (``Host: [::1]:8000``).
    """
    entry = entry.strip()
    if entry.startswith("["):  # [::1] or [::1]:8000
        host, _, rest = entry.partition("]")
        host += "]"
        port = rest[1:] if rest.startswith(":") and len(rest) > 1 else None
        return host, port
    if entry.count(":") > 1:  # bare IPv6 literal: a port needs brackets
        return f"[{entry}]", None
    host, sep, port = entry.partition(":")
    return host, (port or None) if sep else None


# Server version — single-sourced from the package
SERVER_VERSION = __version__


class OdooMCPServer:
    """Main MCP server class for Odoo integration.

    This class manages the MCPServer instance and maintains
    the connection to Odoo. The server lifecycle is managed by
    establishing connection before starting and cleaning up on exit.
    """

    def __init__(self, config: Optional[OdooConfig] = None):
        """Initialize the Odoo MCP server.

        Args:
            config: Optional OdooConfig instance. If not provided,
                   will load from environment variables.
        """
        # Load configuration
        self.config = config or get_config()

        # Set up structured logging with the validated config level
        logging_config.setup(log_level=self.config.log_level)

        # The Odoo objects exist from the start, so the tools and resources
        # are registered before Odoo answers; the connection itself is made
        # on demand (see ensure_connected). Registered handlers hold these
        # references, so they are never replaced, only (re)connected.
        self.performance_manager = PerformanceManager(self.config)
        self.connection = OdooConnection(self.config, performance_manager=self.performance_manager)
        self.access_controller = AccessController(self.config)

        # One connection attempt at a time; after Odoo failed to answer, the
        # next attempt waits _connect_backoff seconds (doubling, capped).
        self._connect_lock = asyncio.Lock()
        self._connect_backoff = 0.0
        self._next_connect_attempt = 0.0
        self._last_connect_error = ""

        # Configure transport security for DNS rebinding protection. Left as
        # None (no allowed_hosts configured), the SDK enables protection only
        # for a loopback bind and leaves it OFF for any other host — see
        # _build_transport_security. mcp 2.x takes it (and the host that
        # decides the loopback default) at run time — see run_http().
        self._transport_security = self._build_transport_security()

        # Create the MCPServer instance with server metadata. Without
        # version=, serverInfo.version is the SDK's own version.
        self.app = MCPServer(
            name="odoo-mcp-server",
            instructions="MCP server for accessing and managing Odoo ERP data through the Model Context Protocol",
            version=SERVER_VERSION,
            lifespan=self._odoo_lifespan,
        )

        # Pristine static instructions, captured before any personalization.
        # _apply_dynamic_instructions() rebuilds from this base so repeated
        # calls (run_stdio/run_http reuse of one instance) never compound
        # the personalized context block.
        self._static_instructions = self.app.instructions or ""

        @self.app.custom_route("/health", methods=["GET"])
        async def health_check(request):
            from starlette.responses import JSONResponse

            return JSONResponse(self.get_health_status())

        @self.app.completion()
        async def handle_completion(ref, argument, context):
            from mcp.types import Completion

            if argument.name == "model":
                model_names = await asyncio.to_thread(self._get_model_names)
                partial = argument.value or ""
                if partial:
                    matches = [m for m in model_names if partial.lower() in m.lower()]
                else:
                    matches = model_names
                return Completion(values=matches[:20])
            return None

        self.resource_handler = register_resources(
            self.app, self.connection, self.access_controller, self.config
        )
        self.tool_handler = register_tools(
            self.app, self.connection, self.access_controller, self.config
        )
        self._install_connection_guard()

        # The usage block names only the registered tools and needs no Odoo,
        # so it joins the static base. MCPServer (mcp 2.2) has no sync tool
        # listing and no instructions setter: private attrs, as in
        # _apply_dynamic_instructions().
        usage = usage_guidance(tool.name for tool in self.app._tool_manager.list_tools())
        if usage:
            self._static_instructions = f"{self._static_instructions}\n\n{usage}".lstrip()
            self.app._lowlevel_server.instructions = self._static_instructions

        logger.info(f"Initialized Odoo MCP Server v{SERVER_VERSION}")

    @contextlib.asynccontextmanager
    async def _odoo_lifespan(self, app: MCPServer):
        """Connect to Odoo at startup and disconnect at shutdown.

        mcp 2.x enters this once per process (stdio and streamable-http
        alike), and a lifespan that raises stops the server. So only
        configuration and authentication errors propagate: an Odoo that does
        not answer yet is logged, and the next request connects on demand.
        """
        try:
            with perf_logger.track_operation("server_startup"):
                await self._connect_or_wait()
            yield {}
        finally:
            self._cleanup_connection()

    async def _connect_or_wait(self) -> None:
        """Connect now; if Odoo does not answer, leave it to the next request."""
        try:
            await self.ensure_connected()
        except OdooUnreachableError as e:
            logger.warning(f"Odoo is unreachable; serving anyway, retrying on demand: {e}")

    def _install_connection_guard(self) -> None:
        """Connect to Odoo on demand before every tool call and resource read.

        MCPServer answers tools/call and resources/read through its public
        call_tool() and read_resource() methods; wrapping them on the app
        instance puts ensure_connected() in front of every handler. The
        wrapped read_resource may already be the resource handler's binary
        dispatcher (see resources._install_binary_read_override).
        """
        app = self.app
        call_tool = app.call_tool
        read_resource = app.read_resource

        async def connected():
            # A connection failure is the request's error, not an unexpected
            # exception: mcp 2.2 logs those with a traceback on every call
            # while Odoo is down. The text is sanitized because it can carry
            # the Odoo URL and port, which a remote HTTP client must not see.
            try:
                await self.ensure_connected()
            except OdooConnectionError as e:
                raise MCPConnectionError(ErrorSanitizer.sanitize_message(str(e))) from e

        async def guarded_call_tool(name, arguments, context=None):
            await connected()
            return await call_tool(name, arguments, context)

        async def guarded_read_resource(uri, context=None):
            await connected()
            return await read_resource(uri, context)

        # Instance attributes shadow the methods for this app only
        app.call_tool = guarded_call_tool  # ty: ignore[invalid-assignment]
        app.read_resource = guarded_read_resource  # ty: ignore[invalid-assignment]

    async def ensure_connected(self) -> None:
        """Connect and authenticate to Odoo unless already done.

        Single-flight under a lock. After Odoo failed to answer, further
        attempts wait out a backoff (1s doubling to 60s) and fail fast with
        the last error meanwhile, so a burst of requests does not pile up
        connection timeouts. Configuration and authentication errors are
        raised every time.

        Raises:
            OdooUnreachableError: Odoo did not answer (now or within the backoff)
            OdooConnectionError: Configuration or authentication error
        """
        if self.connection.is_authenticated:
            return
        async with self._connect_lock:
            if self.connection.is_authenticated:
                return
            wait = self._next_connect_attempt - time.monotonic()
            if wait > 0:
                raise OdooUnreachableError(
                    f"{self._last_connect_error} (next attempt in {wait:.0f}s)"
                )
            try:
                # Sync XML-RPC/urllib I/O, up to the socket timeout
                await asyncio.to_thread(self._connect)
            except OdooUnreachableError as e:
                self._connect_backoff = min(max(self._connect_backoff * 2, 1.0), 60.0)
                self._next_connect_attempt = time.monotonic() + self._connect_backoff
                self._last_connect_error = str(e)
                raise
            self._connect_backoff = 0.0
            self._next_connect_attempt = 0.0
        # Sessions that start from now on get the personalized block
        await self._apply_dynamic_instructions()

    def _connect(self) -> None:
        """Connect and authenticate the existing connection IN PLACE (blocking).

        Registered handlers hold references to the connection and the access
        controller, so both are updated, never replaced. Authentication may
        fall back from the API key to the password, so the access controller
        takes the EFFECTIVE auth method and the resolved database.
        """
        logger.info("Connecting to Odoo...")
        with perf_logger.track_operation("connection_setup"):
            if not self.connection.is_connected:
                self.connection.connect()
            self.connection.authenticate()
            try:
                self.connection.check_allowed_companies()
            except OdooConnectionError:
                # Not left authenticated: every request then raises the same error
                self.connection.disconnect()
                raise
        self.access_controller.database = self.connection.database
        self.access_controller.auth_method = self.connection.auth_method
        transport = "JSON-2" if self.connection.rpc_transport == "json2" else "XML-RPC"
        logger.info(f"Successfully connected to Odoo at {self.config.url} over {transport}")

    def _cleanup_connection(self):
        """Close the Odoo connection; the objects stay for a later reconnect."""
        try:
            if self.connection.is_connected:
                logger.info("Closing Odoo connection...")
                self.connection.disconnect()
        except Exception as e:
            logger.error(f"Error closing connection: {e}")

    async def _apply_dynamic_instructions(self):
        """Personalize ``initialize.instructions`` with the user context.

        Only after a connect (ensure_connected calls this); never connects by
        itself. stdio freezes the instructions when the transport starts, so
        run_stdio() connects before that. Swallows every error: startup must
        never fail on personalization, and the static instructions stay.
        """
        try:
            if not self.connection.is_authenticated:
                return
            # build_user_context does sync XML-RPC I/O — keep it off the loop
            context = await asyncio.to_thread(
                build_user_context, self.connection, self.config.allowed_companies
            )
            # Rebuild from the pristine static base (captured at __init__) —
            # reading self.app.instructions here would compound the context
            # block on repeated calls, since it reflects prior mutations.
            static = self._static_instructions
            # MCPServer (mcp 2.2) exposes `instructions` as a read-only
            # property over the low-level server attribute — assign the
            # private attr. stdio freezes it in create_initialization_options()
            # when the transport starts; HTTP reads it per session.
            self.app._lowlevel_server.instructions = f"{static}\n\n{context}" if static else context
        except Exception as e:
            logger.warning(f"Dynamic instructions unavailable, keeping static instructions: {e}")

    async def run_stdio(self):
        """Run the server using stdio transport."""
        try:
            logger.info("Starting MCP server with stdio transport...")
            # Before the transport starts: stdio freezes the instructions then
            await self._connect_or_wait()
            await self.app.run_stdio_async()
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
        except (OdooConnectionError, ConfigurationError):
            raise
        except Exception as e:
            context = ErrorContext(operation="server_run")
            error_handler.handle_error(e, context=context)

    def run_stdio_sync(self):
        """Synchronous wrapper for run_stdio.

        This is provided for compatibility with synchronous code.
        """
        import asyncio

        asyncio.run(self.run_stdio())

    # No SSE transport (deprecated in MCP; streamable-http replaces it).

    async def run_http(self):
        """Run the server using streamable HTTP transport.

        Takes no host/port: the bind and the transport-security decision both
        follow ``config.host``. With ``transport_security`` None (no
        ODOO_MCP_ALLOWED_HOSTS), the SDK auto-enables its loopback allowlist,
        or not, from the same ``host`` argument it binds, so the two cannot
        drift apart.

        ``session_idle_timeout`` is passed even when unset: mcp 2.x defaults
        to evicting sessions idle for 30 minutes, while an unset
        ODOO_MCP_SESSION_IDLE_TIMEOUT means sessions never expire.
        """
        host = self.config.host
        port = self.config.port
        try:
            logger.info(f"Starting MCP server with HTTP transport on {host}:{port}...")
            self._warn_if_exposed(host)
            await self._connect_or_wait()
            if self.config.session_idle_timeout is not None:
                logger.info(
                    "Streamable-http session idle timeout enabled: %.0fs",
                    self.config.session_idle_timeout,
                )
            await self.app.run_streamable_http_async(
                host=host,
                port=port,
                transport_security=self._transport_security,
                session_idle_timeout=self.config.session_idle_timeout,
            )
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
        except (OdooConnectionError, ConfigurationError):
            raise
        except Exception as e:
            context = ErrorContext(operation="server_run_http")
            error_handler.handle_error(e, context=context)

    def _build_transport_security(self) -> Optional[TransportSecuritySettings]:
        """Build DNS-rebinding-protection settings from ODOO_MCP_ALLOWED_HOSTS.

        Returns None when no hosts are configured, which hands the decision to
        the SDK. Note what that actually means (mcp.server.fastmcp.server):
        the SDK auto-enables protection ONLY when the bind host is loopback
        (``127.0.0.1``/``localhost``/``::1``); for any other bind — notably
        ``0.0.0.0``, the usual Docker setting — it leaves protection DISABLED
        and no Host/Origin validation runs at all. Such a deployment must set
        ODOO_MCP_ALLOWED_HOSTS (and, as ``_warn_if_exposed`` says, front the
        server with an authenticating proxy).

        When hosts are configured, an entry WITHOUT a port matches that host
        on any port — including the implicit 80/443 that browsers and reverse
        proxies omit from ``Host`` and ``Origin`` entirely. The SDK matches a
        ``:*`` pattern with ``startswith(base + ":")``, so the bare form has
        to be listed alongside it; without it the documented
        "odoo.example.com behind a TLS proxy" deployment rejects every
        request. An entry WITH a port is matched exactly.
        """
        if not self.config.allowed_hosts:
            return None

        allowed_hosts: list[str] = []
        allowed_origins: list[str] = []
        for entry in self.config.allowed_hosts:
            host, port = _split_allowed_host(entry)
            if not host:
                continue
            if port:
                # Origins mirror the host entry exactly. A wildcard ":*" here
                # would trust a page served from ANY other port on the same
                # hostname as a cross-origin caller, making the Origin
                # allowlist strictly looser than the Host one it exists to
                # complement — and looser than this docstring promises.
                allowed_hosts.append(f"{host}:{port}")
                allowed_origins.extend([f"http://{host}:{port}", f"https://{host}:{port}"])
            else:
                # ":*" only matches an authority that HAS a port; a port-less
                # Host header needs the bare form listed too.
                allowed_hosts.extend([f"{host}:*", host])
                allowed_origins.extend(
                    [f"http://{host}:*", f"https://{host}:*", f"http://{host}", f"https://{host}"]
                )

        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

    def _warn_if_exposed(self, host: str) -> None:
        """Warn loudly when the HTTP transport binds a non-loopback host.

        The streamable-http transport has NO built-in client authentication:
        anyone who can reach the port gets Odoo access through the server's
        stored credentials. Remote deployments must front it with a reverse
        proxy that enforces authentication.
        """
        if host in ("localhost", "127.0.0.1", "::1"):
            return

        message = (
            f"HTTP transport binding to '{host}' — this transport has NO built-in "
            "authentication. Anyone who can reach this port gets Odoo access with "
            "the server's stored credentials. Bind to localhost or front this "
            "server with an authenticating reverse proxy."
        )
        if not self.config.allowed_hosts:
            message += (
                " DNS-rebinding protection is also OFF for this bind: set "
                "ODOO_MCP_ALLOWED_HOSTS to the Host header(s) you serve."
            )
        if self.config.yolo_mode == "true":
            message += (
                " YOLO FULL-ACCESS MODE IS ENABLED: unauthenticated clients could "
                "read, write and delete ANY record"
            )
            if self.config.enable_method_calls:
                message += " and call arbitrary model methods"
            message += "."
        logger.warning(message)

    def get_capabilities(self) -> Dict[str, Dict[str, bool]]:
        """Get server capabilities.

        Returns:
            Dict with server capabilities
        """
        return {
            "capabilities": {
                "resources": True,  # Exposes Odoo data as resources
                "tools": True,  # Provides tools for Odoo operations
                "prompts": False,  # No prompt support.
            }
        }

    def get_health_status(self) -> Dict[str, Any]:
        """Get server health status.

        Returns:
            Dict with health status
        """
        is_connected = bool(self.connection.is_authenticated)

        return {
            "status": "healthy" if is_connected else "unhealthy",
            "version": SERVER_VERSION,
            "connection": {
                "connected": is_connected,
                "rpc_transport": self.connection.rpc_transport if is_connected else None,
            },
        }

    def _get_model_names(self) -> list[str]:
        """Get available model names for autocomplete."""
        try:
            models = self.access_controller.get_enabled_models()
            if models:
                return [m["model"] for m in models]
            # YOLO mode returns [] meaning "all allowed" — query ir.model directly
            if self.connection.is_authenticated:
                records = self.connection.search_read("ir.model", [], ["model"], limit=200)
                return [r["model"] for r in records]
            return []
        except Exception as e:
            logger.debug(f"Failed to get model names for autocomplete: {e}")
            return []
