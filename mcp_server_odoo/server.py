"""MCP Server implementation for Odoo.

This module provides the FastMCP server that exposes Odoo data
and functionality through the Model Context Protocol.
"""

import asyncio
import contextlib
from typing import Any, Dict, Optional, Tuple

from mcp.server import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .access_control import AccessController
from .config import OdooConfig, get_config
from .error_handling import (
    ConfigurationError,
    ErrorContext,
    error_handler,
)
from .logging_config import get_logger, logging_config, perf_logger
from .odoo_connection import OdooConnection, OdooConnectionError
from .performance import PerformanceManager
from .resources import register_resources
from .tools import register_tools
from .user_context import build_user_context

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

    This class manages the FastMCP server instance and maintains
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

        # Initialize connection and access controller (will be created on startup)
        self.connection: Optional[OdooConnection] = None
        self.access_controller: Optional[AccessController] = None
        self.performance_manager: Optional[PerformanceManager] = None
        self.resource_handler = None
        self.tool_handler = None

        # Serializes connection setup/reauth across concurrent lifespan
        # entries (streamable-http enters the lifespan per session)
        self._connect_lock = asyncio.Lock()

        # Set by run_http() — the lifespan teardown keys off which transport
        # actually started, not off config.transport. run_http() is public and
        # can be called with ODOO_MCP_TRANSPORT unset (it binds config.host /
        # config.port either way); tearing the connection down per HTTP
        # session then leaves every
        # registered tool closure bound to a disconnected handler (FastMCP
        # keeps the first registration for a duplicate tool name), which is
        # exactly the failure the gate exists to prevent.
        self._http_transport_active = False

        # Configure transport security for DNS rebinding protection. Left as
        # None (no allowed_hosts configured), the SDK enables protection only
        # for a loopback bind and leaves it OFF for any other host — see
        # _build_transport_security.
        transport_security = self._build_transport_security()

        # Create FastMCP instance with server metadata
        self.app = FastMCP(
            name="odoo-mcp-server",
            instructions="MCP server for accessing and managing Odoo ERP data through the Model Context Protocol",
            lifespan=self._odoo_lifespan,
            host=self.config.host,
            transport_security=transport_security,
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

        logger.info(f"Initialized Odoo MCP Server v{SERVER_VERSION}")

    @contextlib.asynccontextmanager
    async def _odoo_lifespan(self, app: FastMCP):
        """Manage Odoo connection lifecycle for FastMCP.

        Sets up connection, registers resources/tools before serving.

        The low-level MCP server enters this context PER SESSION. Under
        stdio there is exactly one session per process, so cleaning up on
        exit is correct. Under streamable-http every client session (and
        every ``DELETE /mcp``) exits and re-enters it — tearing down the
        authenticated Odoo connection there broke every call after the
        first (#70). The connection must persist across HTTP sessions;
        the OS reclaims it at process exit.
        """
        try:
            with perf_logger.track_operation("server_startup"):
                # Connection setup is sync XML-RPC/urllib I/O (up to the
                # socket timeout) — keep it off the event loop. The lock
                # preserves the serialization that running on the loop's
                # single thread used to provide.
                async with self._connect_lock:
                    await asyncio.to_thread(self._ensure_connection)
                self._register_resources()
                self._register_tools()
            yield {}
        finally:
            if not self._http_transport_active:
                self._cleanup_connection()

    def _ensure_connection(self):
        """Ensure connection to Odoo is established.

        Reuses an existing authenticated connection (streamable-http
        re-enters the lifespan per session — see ``_odoo_lifespan``).

        Raises:
            ConnectionError: If connection fails
            ConfigurationError: If configuration is invalid
        """
        if self.connection and self.connection.is_authenticated:
            logger.info("Reusing existing authenticated Odoo connection")
            return
        if self.connection:
            # Reconnect the existing object IN PLACE: registered tool and
            # resource handlers hold references to this connection, so it
            # must never be replaced with a new instance.
            logger.warning("Existing connection is not authenticated; reconnecting")
            try:
                with perf_logger.track_operation("connection_reauth"):
                    if not self.connection.is_connected:
                        self.connection.connect()
                    self.connection.authenticate()
                # Reauth re-runs the api-key→password fallback chain, so the
                # effective auth method may differ from the initial connect.
                # The controller may not exist at all if the first startup
                # failed after self.connection was assigned but before auth
                # succeeded — without it, handler registration silently skips.
                if self.access_controller is None:
                    self.access_controller = AccessController(
                        self.config,
                        database=self.connection.database,
                        auth_method=self.connection.auth_method,
                    )
                else:
                    self.access_controller.auth_method = self.connection.auth_method
                return
            except Exception as e:
                context = ErrorContext(operation="connection_reauth")
                if isinstance(e, (OdooConnectionError, ConfigurationError)):
                    raise
                # handle_error reraises (reraise defaults to True) — reauth
                # failures always propagate to the session
                error_handler.handle_error(e, context=context)
        if not self.connection:
            try:
                logger.info("Establishing connection to Odoo...")
                with perf_logger.track_operation("connection_setup"):
                    # Create performance manager (shared across components)
                    self.performance_manager = PerformanceManager(self.config)

                    # Create connection with performance manager
                    self.connection = OdooConnection(
                        self.config, performance_manager=self.performance_manager
                    )

                    # Connect and authenticate
                    self.connection.connect()
                    self.connection.authenticate()

                logger.info(f"Successfully connected to Odoo at {self.config.url}")

                # Initialize access controller (pass resolved DB for session
                # auth and the EFFECTIVE auth method — after a password
                # fallback, permission checks must not send the rejected key)
                self.access_controller = AccessController(
                    self.config,
                    database=self.connection.database,
                    auth_method=self.connection.auth_method,
                )
            except Exception as e:
                # self.connection is deliberately NOT cleared here: it is
                # assigned before connect()/authenticate() run, and a later
                # session reauthenticates that same object IN PLACE (see
                # _ensure_connection and the recovery test). Replacing it
                # would strand any handler already holding a reference.
                context = ErrorContext(operation="connection_setup")
                # Let specific errors propagate as-is
                if isinstance(e, (OdooConnectionError, ConfigurationError)):
                    raise
                # Handle other unexpected errors
                error_handler.handle_error(e, context=context)

    def _cleanup_connection(self):
        """Clean up Odoo connection."""
        if self.connection:
            try:
                logger.info("Closing Odoo connection...")
                self.connection.disconnect()
            except Exception as e:
                logger.error(f"Error closing connection: {e}")
            finally:
                # Always clear connection reference. Handlers already
                # registered on self.app stay registered (FastMCP has no
                # deregistration); reusing this instance for another run is
                # not a supported flow (__main__ builds a fresh server per
                # process). If it happens anyway, re-registration is benign
                # at the FastMCP layer: resource templates are overwritten
                # (dict assignment), duplicate tool names keep the existing
                # registration (a warning is logged), and the low-level
                # binary read override replaces itself without chaining
                # (see resources._install_binary_read_override).
                self.connection = None
                self.access_controller = None
                self.resource_handler = None
                self.tool_handler = None

    def _register_resources(self):
        """Register resource handlers after connection is established.

        Idempotent: streamable-http re-enters the lifespan per session and
        handlers must not be registered twice on the shared FastMCP app.
        """
        if self.resource_handler is not None:
            logger.debug("Resources already registered, skipping")
            return
        if self.connection and self.access_controller:
            self.resource_handler = register_resources(
                self.app, self.connection, self.access_controller, self.config
            )
            logger.info("Registered MCP resources")

    def _register_tools(self):
        """Register tool handlers after connection is established.

        Idempotent — see ``_register_resources``.
        """
        if self.tool_handler is not None:
            logger.debug("Tools already registered, skipping")
            return
        if self.connection and self.access_controller:
            self.tool_handler = register_tools(
                self.app, self.connection, self.access_controller, self.config
            )
            logger.info("Registered MCP tools")

    async def _apply_dynamic_instructions(self):
        """Personalize ``initialize.instructions`` with the user context.

        Must run BEFORE the transport starts: the SDK freezes instructions
        when the transport calls ``create_initialization_options()``, which
        happens before the lifespan (where the connection is normally
        established) is entered — hence the eager connect here. Guarded so
        startup never fails on it: on any error the static instructions are
        kept, and a real connection failure will still surface properly from
        the lifespan.
        """
        try:
            async with self._connect_lock:
                await asyncio.to_thread(self._ensure_connection)
            if not (self.connection and self.connection.is_authenticated):
                return
            # build_user_context does sync XML-RPC I/O — keep it off the loop
            context = await asyncio.to_thread(build_user_context, self.connection)
            # Rebuild from the pristine static base (captured at __init__) —
            # reading self.app.instructions here would compound the context
            # block on repeated calls, since it reflects prior mutations.
            static = self._static_instructions
            # FastMCP (mcp 1.27) exposes `instructions` as a read-only
            # property over the low-level server attribute — assign the
            # private attr, which create_initialization_options() reads
            # when the transport starts.
            self.app._mcp_server.instructions = f"{static}\n\n{context}" if static else context
        except Exception as e:
            logger.warning(f"Dynamic instructions unavailable, keeping static instructions: {e}")

    async def run_stdio(self):
        """Run the server using stdio transport."""
        try:
            logger.info("Starting MCP server with stdio transport...")
            await self._apply_dynamic_instructions()
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

        Takes no host/port. FastMCP is constructed with ``config.host`` and
        decides transport security from it right there (``__init__`` passes
        ``transport_security``, which is None whenever ODOO_MCP_ALLOWED_HOSTS
        is empty — the SDK then auto-enables its loopback allowlist, or not,
        from that host). Reassigning ``app.settings.host`` here would move the
        bind without moving that decision, so a caller could bind loopback
        with protection left off. The bind and the decision stay tied to one
        value instead.
        """
        host = self.config.host
        port = self.config.port
        try:
            logger.info(f"Starting MCP server with HTTP transport on {host}:{port}...")
            self._http_transport_active = True
            self._warn_if_exposed(host)
            self.app.settings.port = port
            await self._apply_dynamic_instructions()
            self._preseed_session_manager()
            await self.app.run_streamable_http_async()
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
        except (OdooConnectionError, ConfigurationError):
            raise
        except Exception as e:
            context = ErrorContext(operation="server_run_http")
            error_handler.handle_error(e, context=context)

    def _preseed_session_manager(self) -> None:
        """Apply ODOO_MCP_SESSION_IDLE_TIMEOUT to the streamable-http transport.

        The SDK's StreamableHTTPSessionManager supports evicting idle sessions
        (freeing their transport state, which otherwise accumulates until
        process restart), but FastMCP does not yet expose the parameter. Its session manager is created lazily in
        streamable_http_app(), so constructing it here first — mirroring the
        arguments FastMCP would pass, plus the timeout — makes FastMCP reuse
        this instance. Remove once FastMCP plumbs session_idle_timeout through.
        """
        if self.config.session_idle_timeout is None:
            return

        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        self.app._session_manager = StreamableHTTPSessionManager(
            app=self.app._mcp_server,
            event_store=self.app._event_store,
            retry_interval=self.app._retry_interval,
            json_response=self.app.settings.json_response,
            stateless=self.app.settings.stateless_http,
            security_settings=self.app.settings.transport_security,
            session_idle_timeout=self.config.session_idle_timeout,
        )
        logger.info(
            "Streamable-http session idle timeout enabled: %.0fs",
            self.config.session_idle_timeout,
        )

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
        is_connected = bool(self.connection is not None and self.connection.is_authenticated)

        return {
            "status": "healthy" if is_connected else "unhealthy",
            "version": SERVER_VERSION,
            "connection": {
                "connected": is_connected,
            },
        }

    def _get_model_names(self) -> list[str]:
        """Get available model names for autocomplete."""
        if not self.access_controller:
            return []
        try:
            models = self.access_controller.get_enabled_models()
            if models:
                return [m["model"] for m in models]
            # YOLO mode returns [] meaning "all allowed" — query ir.model directly
            if self.connection and self.connection.is_authenticated:
                records = self.connection.search_read("ir.model", [], ["model"], limit=200)
                return [r["model"] for r in records]
            return []
        except Exception as e:
            logger.debug(f"Failed to get model names for autocomplete: {e}")
            return []
