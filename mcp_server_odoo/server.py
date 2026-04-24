"""MCP Server implementation for Odoo.

This module provides the FastMCP server that exposes Odoo data
and functionality through the Model Context Protocol.
"""

import atexit
import contextlib
from typing import Any, Dict, Optional

from mcp.server import FastMCP

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

# Set up logging
logger = get_logger(__name__)

# Server version
SERVER_VERSION = "0.5.0"


class OdooMCPServer:
    """Main MCP server class for Odoo integration.

    This class manages the FastMCP server instance and maintains
    the connection to Odoo. The connection is **process-scoped**:
    it is established once per process and reused across MCP
    (streamable-http) sessions. Tool/resource registration is also
    performed only once per instance to avoid duplicate-registration
    warnings when the lifespan context manager is re-entered per
    session by some transports.
    """

    def __init__(self, config: Optional[OdooConfig] = None):
        """Initialize the Odoo MCP server.

        Args:
            config: Optional OdooConfig instance. If not provided,
                   will load from environment variables.
        """
        # Load configuration
        self.config = config or get_config()

        # Set up structured logging
        logging_config.setup()

        # Initialize connection and access controller (will be created on startup)
        self.connection: Optional[OdooConnection] = None
        self.access_controller: Optional[AccessController] = None
        self.performance_manager: Optional[PerformanceManager] = None
        self.resource_handler = None
        self.tool_handler = None

        # One-shot guards so the lifespan being re-entered per MCP session
        # (as happens with streamable-http transport) does not tear down
        # the connection or re-register tools/resources.
        self._startup_done: bool = False
        self._tools_registered: bool = False
        self._resources_registered: bool = False
        self._atexit_registered: bool = False

        # Create FastMCP instance with server metadata
        self.app = FastMCP(
            name="odoo-mcp-server",
            instructions="MCP server for accessing and managing Odoo ERP data through the Model Context Protocol",
            lifespan=self._odoo_lifespan,
        )

        @self.app.custom_route("/health", methods=["GET"])
        async def health_check(request):
            from starlette.responses import JSONResponse

            return JSONResponse(self.get_health_status())

        @self.app.completion()
        async def handle_completion(ref, argument, context):
            from mcp.types import Completion

            if argument.name == "model":
                model_names = self._get_model_names()
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

        The connection and tool/resource registration are process-scoped
        one-shots. This coroutine may be re-entered once per MCP session
        by some transports (notably streamable-http); re-entries do not
        re-authenticate or re-register handlers, and crucially they do
        NOT tear the connection down on exit. Teardown happens only at
        true process shutdown via :mod:`atexit`.
        """
        if not self._startup_done:
            with perf_logger.track_operation("server_startup"):
                self._ensure_connection()
                self._register_resources()
                self._register_tools()
                self._register_shutdown_hook()
            self._startup_done = True
        else:
            # Subsequent session: verify the long-lived connection is
            # still alive and re-authenticate if not.
            self._ensure_connection()
        try:
            yield {}
        finally:
            # Intentionally do NOT call _cleanup_connection here. The
            # Odoo connection is process-scoped and must survive
            # individual MCP session teardowns so subsequent sessions
            # can reuse it.
            pass

    def _register_shutdown_hook(self) -> None:
        """Register a process-level shutdown hook to release the connection."""
        if self._atexit_registered:
            return
        try:
            atexit.register(self._cleanup_connection)
            self._atexit_registered = True
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Could not register atexit cleanup: {e}")

    def _connection_is_live(self) -> bool:
        """Cheap liveness probe for the cached Odoo connection.

        Returns True only if the connection object exists, reports
        itself as authenticated, and responds to a lightweight RPC
        (``common.version()``). Any failure is logged at debug level
        and returns False so the caller can re-authenticate.
        """
        conn = self.connection
        if conn is None or not getattr(conn, "is_authenticated", False):
            return False
        try:
            is_healthy, _ = conn.check_health()
            return bool(is_healthy)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Liveness probe raised: {e}")
            return False

    def _ensure_connection(self):
        """Ensure a live, authenticated connection to Odoo exists.

        Idempotent: if a cached connection is already alive and
        authenticated, this is a no-op. If the cached connection is
        stale/dead, it is discarded and a fresh one is created.

        Raises:
            ConnectionError: If connection fails
            ConfigurationError: If configuration is invalid
        """
        if self.connection is not None:
            if self._connection_is_live():
                return
            # Stale / dead — drop it and reconnect below.
            logger.warning("Cached Odoo connection is stale; reconnecting...")
            try:
                self.connection.disconnect(suppress_logging=True)
            except Exception:
                pass
            self.connection = None
            self.access_controller = None

        try:
            logger.info("Establishing connection to Odoo...")
            with perf_logger.track_operation("connection_setup"):
                # Create performance manager (shared across components)
                if self.performance_manager is None:
                    self.performance_manager = PerformanceManager(self.config)

                # Create connection with performance manager
                self.connection = OdooConnection(
                    self.config, performance_manager=self.performance_manager
                )

                # Connect and authenticate
                self.connection.connect()
                self.connection.authenticate()

            logger.info(f"Successfully connected to Odoo at {self.config.url}")

            # Initialize access controller (pass resolved DB for session auth)
            self.access_controller = AccessController(
                self.config, database=self.connection.database
            )
        except Exception as e:
            context = ErrorContext(operation="connection_setup")
            # Let specific errors propagate as-is
            if isinstance(e, (OdooConnectionError, ConfigurationError)):
                raise
            # Handle other unexpected errors
            error_handler.handle_error(e, context=context)

    def _cleanup_connection(self):
        """Clean up Odoo connection. Called at process shutdown only."""
        if self.connection:
            try:
                try:
                    logger.info("Closing Odoo connection...")
                except (ValueError, RuntimeError):
                    # Logging may already be torn down during atexit.
                    pass
                self.connection.disconnect()
            except Exception as e:
                try:
                    logger.error(f"Error closing connection: {e}")
                except (ValueError, RuntimeError):
                    pass
            finally:
                # Always clear connection reference
                self.connection = None
                self.access_controller = None
                self.resource_handler = None
                self.tool_handler = None

    def _register_resources(self):
        """Register resource handlers after connection is established.

        Guarded so the lifespan being re-entered per MCP session does
        not attempt to re-register resources with FastMCP.
        """
        if self._resources_registered:
            return
        if self.connection and self.access_controller:
            self.resource_handler = register_resources(
                self.app, self.connection, self.access_controller, self.config
            )
            self._resources_registered = True
            logger.info("Registered MCP resources")

    def _register_tools(self):
        """Register tool handlers after connection is established.

        Guarded so the lifespan being re-entered per MCP session does
        not attempt to re-register tools (which would emit
        ``Tool already exists`` warnings).
        """
        if self._tools_registered:
            return
        if self.connection and self.access_controller:
            self.tool_handler = register_tools(
                self.app, self.connection, self.access_controller, self.config
            )
            self._tools_registered = True
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
            self.app.settings.host = host
            self.app.settings.port = port
            await self.app.run_streamable_http_async()
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
        except (OdooConnectionError, ConfigurationError):
            raise
        except Exception as e:
            context = ErrorContext(operation="server_run_http")
            error_handler.handle_error(e, context=context)

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
        """Get server health status with error metrics.

        Returns:
            Dict with health status and metrics
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
