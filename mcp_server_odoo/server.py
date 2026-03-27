"""MCP Server implementation for Odoo.

This module provides the FastMCP server that exposes Odoo data
and functionality through the Model Context Protocol.
"""

import contextlib
from typing import Any, Dict, Optional

from mcp.server import FastMCP

from .access_control import AccessController
from .audit import AuditLogger
from .auth import ANONYMOUS_AUTH, AuthMiddleware, create_auth_provider, set_current_auth_info
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
from .security import ConfirmationManager, MutationPolicy, SecurityPolicy
from .tools import register_tools

# Set up logging
logger = get_logger(__name__)

# Server version
SERVER_VERSION = "0.6.0"


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

        # Set up structured logging
        logging_config.setup()

        # Initialize connection and access controller (will be created on startup)
        self.connection: Optional[OdooConnection] = None
        self.access_controller: Optional[AccessController] = None
        self.performance_manager: Optional[PerformanceManager] = None
        self.resource_handler = None
        self.tool_handler = None

        # Initialize security components
        self.security_policy = SecurityPolicy(self.config)
        self.mutation_policy = MutationPolicy(self.config)
        self.confirmation_manager = ConfirmationManager(self.config)
        self.audit_logger = AuditLogger(self.config)
        self.auth_provider = create_auth_provider(self.config)

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

        @self.app.custom_route("/ready", methods=["GET"])
        async def readiness_check(request):
            from starlette.responses import JSONResponse

            is_ready = bool(self.connection and self.connection.is_authenticated)
            status_code = 200 if is_ready else 503
            return JSONResponse(
                {
                    "ready": is_ready,
                    "version": SERVER_VERSION,
                    "auth_mode": self.config.auth_mode,
                },
                status_code=status_code,
            )

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

        self._log_startup_posture()
        logger.info(f"Initialized Odoo MCP Server v{SERVER_VERSION}")

    def _log_startup_posture(self):
        """Log the security posture at startup."""
        c = self.config
        logger.info("=== Security Posture ===")
        logger.info("  Auth mode: %s", c.auth_mode)
        logger.info("  Transport: %s", c.transport)
        if c.allowed_models:
            logger.info("  Allowed models: %s", ", ".join(c.allowed_models))
        else:
            logger.info("  Allowed models: ALL (no restriction)")
        logger.info("  Mutations enabled: %s", c.enable_mutations)
        logger.info("  Deletes enabled: %s", c.enable_deletes)
        logger.info("  Confirmation required: %s", c.require_confirmation_for_mutations)
        logger.info("  Audit logging: %s", c.audit_log_enabled)
        if c.admin_mode:
            logger.warning("  ADMIN MODE: ENABLED (all safety checks bypassed)")
        if c.cors_origins:
            logger.info("  CORS origins: %s", ", ".join(c.cors_origins))
        logger.info("========================")

    @contextlib.asynccontextmanager
    async def _odoo_lifespan(self, app: FastMCP):
        """Manage Odoo connection lifecycle for FastMCP.

        Sets up connection, registers resources/tools before server starts.
        Cleans up connection when server stops.
        """
        try:
            with perf_logger.track_operation("server_startup"):
                self._ensure_connection()
                self._register_resources()
                self._register_tools()
            yield {}
        finally:
            self._cleanup_connection()

    def _ensure_connection(self):
        """Ensure connection to Odoo is established.

        Raises:
            ConnectionError: If connection fails
            ConfigurationError: If configuration is invalid
        """
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
        """Clean up Odoo connection."""
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
        """Register resource handlers after connection is established."""
        if self.connection and self.access_controller:
            self.resource_handler = register_resources(
                self.app, self.connection, self.access_controller, self.config
            )
            logger.info("Registered MCP resources")

    def _register_tools(self):
        """Register tool handlers after connection is established."""
        if self.connection and self.access_controller:
            self.tool_handler = register_tools(
                self.app,
                self.connection,
                self.access_controller,
                self.config,
                security_policy=self.security_policy,
                mutation_policy=self.mutation_policy,
                confirmation_manager=self.confirmation_manager,
                audit_logger=self.audit_logger,
            )
            logger.info("Registered MCP tools")

    async def run_stdio(self):
        """Run the server using stdio transport."""
        try:
            # Set anonymous auth for local stdio
            set_current_auth_info(ANONYMOUS_AUTH)
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
        """Synchronous wrapper for run_stdio."""
        import asyncio

        asyncio.run(self.run_stdio())

    # SSE transport has been deprecated in MCP protocol version 2025-03-26
    # Use streamable-http transport instead

    async def run_http(self, host: str = "localhost", port: int = 8000):
        """Run the server using streamable HTTP transport.

        Wraps the FastMCP Starlette app with auth and CORS middleware,
        then starts uvicorn directly.
        """
        try:
            logger.info(f"Starting MCP server with HTTP transport on {host}:{port}...")
            self.app.settings.host = host
            self.app.settings.port = port

            # Get the Starlette ASGI app from FastMCP
            starlette_app = self.app.streamable_http_app()

            # Add CORS middleware if configured
            if self.config.cors_origins:
                from starlette.middleware.cors import CORSMiddleware

                starlette_app.add_middleware(
                    CORSMiddleware,
                    allow_origins=self.config.cors_origins,
                    allow_credentials=self.config.cors_allow_credentials,
                    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                    allow_headers=["*", "Mcp-Session-Id"],
                )
                logger.info("CORS enabled for origins: %s", self.config.cors_origins)

            # Wrap with auth middleware if auth is enabled
            if self.config.auth_mode != "none":
                asgi_app = AuthMiddleware(starlette_app, self.auth_provider)
                logger.info("Auth middleware enabled (mode: %s)", self.config.auth_mode)
            else:
                asgi_app = starlette_app

            # Run with uvicorn
            import uvicorn

            config = uvicorn.Config(
                asgi_app,
                host=host,
                port=port,
                log_level=self.config.log_level.lower(),
            )
            server = uvicorn.Server(config)
            await server.serve()
        except KeyboardInterrupt:
            logger.info("Server interrupted by user")
        except (OdooConnectionError, ConfigurationError):
            raise
        except Exception as e:
            context = ErrorContext(operation="server_run_http")
            error_handler.handle_error(e, context=context)

    def get_capabilities(self) -> Dict[str, Dict[str, bool]]:
        """Get server capabilities."""
        return {
            "capabilities": {
                "resources": True,
                "tools": True,
                "prompts": False,
            }
        }

    def get_health_status(self) -> Dict[str, Any]:
        """Get server health status."""
        is_connected = bool(self.connection is not None and self.connection.is_authenticated)

        return {
            "status": "healthy" if is_connected else "unhealthy",
            "version": SERVER_VERSION,
            "auth_mode": self.config.auth_mode,
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
                names = [m["model"] for m in models]
            elif self.connection and self.connection.is_authenticated:
                # YOLO mode returns [] meaning "all allowed"
                records = self.connection.search_read("ir.model", [], ["model"], limit=200)
                names = [r["model"] for r in records]
            else:
                return []

            # Filter by security policy if allowlist is configured
            allowed = self.security_policy.get_allowed_models_list()
            if allowed:
                names = [n for n in names if n in set(allowed)]
            return names
        except Exception as e:
            logger.debug(f"Failed to get model names for autocomplete: {e}")
            return []
