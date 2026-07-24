"""MCP Server implementation for Odoo.

This module provides the FastMCP server that exposes Odoo data
and functionality through the Model Context Protocol.
"""

import asyncio
import contextlib
from typing import Any, Dict, Optional

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
from . import request_auth

# Set up logging
logger = get_logger(__name__)

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

        # Configure transport security for DNS rebinding protection. Left as
        # None (no allowed_hosts configured) the SDK middleware defaults to
        # protection DISABLED — preserving prior behavior for stdio and for
        # HTTP deployments that don't set ODOO_MCP_ALLOWED_HOSTS.
        transport_security = self._build_transport_security()

        # Create FastMCP instance with server metadata
        self.app = FastMCP(
            name="odoo-mcp-server",
            instructions="MCP server for accessing and managing Odoo ERP data through the Model Context Protocol",
            lifespan=self._odoo_lifespan,
            host=self.config.host,
            transport_security=transport_security,
        )

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

        if self.config.per_request_auth:
            self._setup_per_request_auth()

        logger.info(f"Initialized Odoo MCP Server v{SERVER_VERSION}")

    def _setup_per_request_auth(self) -> None:
        """Wire per-request auth: the connection and access controller become
        proxies that resolve to the object built for the current request's key.

        No server-wide connection is created (fail-closed): any Odoo access
        outside a request context raises. Handlers are registered eagerly here
        (pure app decoration, no I/O) so tools exist before the first request.
        """
        self.performance_manager = PerformanceManager(self.config)
        request_auth.configure(self.config, self.performance_manager)
        self.connection = request_auth.connection_proxy()
        self.access_controller = request_auth.access_controller_proxy()
        self._register_resources()
        self._register_tools()
        logger.info("Per-request auth enabled: each request authenticates with its own API key")

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
            if self.config.transport != "streamable-http":
                self._cleanup_connection()

    def _ensure_connection(self):
        """Ensure connection to Odoo is established.

        Reuses an existing authenticated connection (streamable-http
        re-enters the lifespan per session — see ``_odoo_lifespan``).

        Raises:
            ConnectionError: If connection fails
            ConfigurationError: If configuration is invalid
        """
        # Per-request auth holds no server-wide connection; each request builds
        # and tears down its own. Nothing to establish at startup.
        if self.config.per_request_auth:
            return
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
                context = ErrorContext(operation="connection_setup")
                # Let specific errors propagate as-is
                if isinstance(e, (OdooConnectionError, ConfigurationError)):
                    raise
                # Handle other unexpected errors
                error_handler.handle_error(e, context=context)

    def _cleanup_connection(self):
        """Clean up Odoo connection."""
        # Per-request connections are closed per request (request_auth.reset);
        # self.connection is a proxy with no state to clean up here.
        if self.config.per_request_auth:
            return
        if self.connection:
            try:
                logger.info("Closing Odoo connection...")
                self.connection.disconnect()
            except Exception as e:
                logger.error(f"Error closing connection: {e}")
            finally:
                # Always clear connection reference
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

    async def run_stdio(self):
        """Run the server using stdio transport."""
        try:
            logger.info("Starting MCP server with stdio transport...")
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

    # SSE transport has been deprecated in MCP protocol version 2025-03-26
    # Use streamable-http transport instead

    async def run_http(self, host: str = "localhost", port: int = 8000):
        """Run the server using streamable HTTP transport.

        Args:
            host: Host to bind to
            port: Port to bind to
        """
        try:
            logger.info(f"Starting MCP server with HTTP transport on {host}:{port}...")
            self._warn_if_exposed(host)
            self.app.settings.host = host
            self.app.settings.port = port
            if self.config.per_request_auth:
                await self._run_http_per_request(host, port)
                return
            self._preseed_session_manager()
            await self.app.run_streamable_http_async()
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
        except (OdooConnectionError, ConfigurationError):
            raise
        except Exception as e:
            context = ErrorContext(operation="server_run_http")
            error_handler.handle_error(e, context=context)

    async def _run_http_per_request(self, host: str, port: int) -> None:
        """Serve streamable-http with per-request API-key auth.

        Runs the transport STATELESS so each POST is handled inline in the
        request task: this is what lets the API key set by the ASGI middleware
        reach the tool through the context variable (a stateful session would
        dispatch the tool on a separate long-lived task the header never
        touches). The middleware also returns a real 401 before dispatch.
        """
        import uvicorn

        # Stateless: no server-side session state between requests, so the
        # per-request key set in middleware is visible to the inline handler.
        self.app.settings.stateless_http = True
        self.app.settings.json_response = True

        http_app = self.app.streamable_http_app()
        guarded = request_auth.PerRequestAuthMiddleware(http_app, guarded_prefix="/mcp")

        config = uvicorn.Config(
            guarded,
            host=host,
            port=port,
            log_level=self.config.log_level.lower(),
        )
        await uvicorn.Server(config).serve()

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

        Returns None when no hosts are configured, which leaves the SDK
        middleware at its default (protection disabled) — unchanged behavior
        for stdio and for HTTP deployments behind a proxy that don't set the
        variable. When hosts are configured, each is allowed on any port and
        matching http/https origins are derived.
        """
        if not self.config.allowed_hosts:
            return None

        allowed_hosts: list[str] = []
        allowed_origins: list[str] = []
        for host in self.config.allowed_hosts:
            base = host.split(":")[0] if ":" in host else host
            allowed_hosts.append(host if ":" in host else f"{host}:*")
            allowed_origins.extend([f"http://{base}:*", f"https://{base}:*"])

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

        if self.config.per_request_auth:
            # Per-request auth DOES gate every request: no valid API key in the
            # Authorization header -> HTTP 401 before dispatch. So the generic
            # "no authentication" warning would be false here. Note the real
            # residual risk instead: bearer tokens cross the network in the
            # clear unless TLS terminates in front.
            logger.info(
                "HTTP transport binding to '%s' with per-request auth: every request "
                "must carry a valid 'Authorization: Bearer <api-key>' (else 401). Keys "
                "are Odoo credentials sent in the clear over plain HTTP — keep this on a "
                "trusted network or terminate TLS in front.",
                host,
            )
            return

        message = (
            f"HTTP transport binding to '{host}' — this transport has NO built-in "
            "authentication. Anyone who can reach this port gets Odoo access with "
            "the server's stored credentials. Bind to localhost or front this "
            "server with an authenticating reverse proxy."
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
                "prompts": False,  # Prompts will be added in later phases
            }
        }

    def get_health_status(self) -> Dict[str, Any]:
        """Get server health status.

        Returns:
            Dict with health status
        """
        if self.config.per_request_auth:
            # No server-wide connection to probe; liveness is what /health means
            # here. Touching the proxy outside a request would fail closed.
            return {
                "status": "healthy",
                "version": SERVER_VERSION,
                "connection": {"connected": True, "mode": "per-request-auth"},
            }

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
