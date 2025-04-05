"""MCP Server implementation for Odoo.

This module implements the core MCP Server functionality for Odoo.
It handles resource management, client communication, and server lifecycle.
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp import Resource
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import ServerCapabilities

from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError
from mcp_server_odoo.resource_handlers import ResourceHandlerRegistry

logger = logging.getLogger(__name__)


class OdooContext:
    """Context object for Odoo MCP server."""

    def __init__(
        self,
        odoo: OdooConnection,
        registry: ResourceHandlerRegistry,
    ):
        """Initialize the Odoo context.

        Args:
            odoo: Odoo connection instance
            registry: Resource handler registry
        """
        self.odoo = odoo
        self.registry = registry


@asynccontextmanager
async def odoo_lifespan(
    server: Server,
    odoo_url: str,
    odoo_db: str,
    odoo_token: str,
    default_limit: int = 20,
    max_limit: int = 100,
) -> AsyncIterator[OdooContext]:
    """Manage the Odoo connection lifecycle.

    Args:
        server: MCP server instance
        odoo_url: URL of the Odoo instance
        odoo_db: Odoo database name
        odoo_token: Authentication token for Odoo MCP
        default_limit: Default record limit for search operations
        max_limit: Maximum allowed record limit

    Yields:
        OdooContext: Context with initialized Odoo connection
    """
    # Create Odoo connection
    odoo = OdooConnection(
        url=odoo_url,
        db=odoo_db,
        token=odoo_token,
    )

    try:
        # Test connection before proceeding
        odoo.test_connection()
        logger.info(f"Connected to Odoo at {odoo_url}")

        # Create resource handler registry
        registry = ResourceHandlerRegistry(
            odoo=odoo,
            default_limit=default_limit,
            max_limit=max_limit,
        )

        # Yield context to server
        yield OdooContext(odoo=odoo, registry=registry)

    except Exception as e:
        logger.exception(f"Failed to initialize Odoo connection: {e}")
        raise
    finally:
        # Clean up (if needed)
        logger.info("Shutting down Odoo connection")


class MCPOdooServer:
    """MCP Server implementation for Odoo.

    This class manages the MCP server lifecycle and connects to Odoo
    using provided configuration. It handles client communication and
    resource management.
    """

    def __init__(
        self,
        odoo_url: str,
        odoo_db: str,
        odoo_token: str,
        default_limit: int = 20,
        max_limit: int = 100,
    ):
        """Initialize the MCP Odoo Server.

        Args:
            odoo_url: URL of the Odoo instance
            odoo_db: Odoo database name
            odoo_token: Authentication token for Odoo MCP
            default_limit: Default record limit for search operations
            max_limit: Maximum allowed record limit
        """
        self.odoo_url = odoo_url
        self.odoo_db = odoo_db
        self.odoo_token = odoo_token
        self.default_limit = default_limit
        self.max_limit = max_limit

        # Create Odoo connection
        self.odoo = OdooConnection(
            url=odoo_url,
            db=odoo_db,
            token=odoo_token,
        )

        # Create resource handler registry
        self.registry = ResourceHandlerRegistry(
            odoo=self.odoo,
            default_limit=default_limit,
            max_limit=max_limit,
        )

        # Initialize MCP server with lifespan
        self.server = Server(
            name="odoo",
            version="0.1.0",
            lifespan=lambda server: odoo_lifespan(
                server, odoo_url, odoo_db, odoo_token, default_limit, max_limit
            ),
        )

        # Set resource handler for handling resource requests
        self.server.on_resource = self._handle_resource

    def get_capabilities(self) -> ServerCapabilities:
        """Get server capabilities.

        Returns:
            ServerCapabilities: MCP server capabilities
        """
        return ServerCapabilities(
            resources={"listResources": True},
            # No tools or prompts in initial implementation
            tools=None,
            prompts=None,
            # Support for logging
            logging={"verbosity": "info"},
        )

    async def run_async(self) -> None:
        """Run the MCP server asynchronously with stdio transport.

        This method asynchronously starts the server and handles
        client communication using the stdio transport.
        """
        try:
            # Create initialization options
            init_options = InitializationOptions(
                server_name="odoo",
                server_version="0.1.0",
                capabilities=self.get_capabilities(),
            )

            logger.info("Starting MCP Server for Odoo")

            # Use the stdio_server context manager to get read/write streams
            async with stdio_server() as (read_stream, write_stream):
                # Run the server with standard stdio transport
                await self.server.run(
                    read_stream,
                    write_stream,
                    init_options,
                )

            logger.info("MCP Server for Odoo shutdown successfully")

        except OdooConnectionError as e:
            logger.error(f"Odoo connection error: {e}")
            raise
        except Exception as e:
            logger.exception(f"Error running MCP server: {e}")
            raise

    def start(self) -> None:
        """Start the MCP server with stdio transport.

        This method initializes the server and begins listening for
        client messages using the stdio transport.
        """
        try:
            # Run the async server in the asyncio event loop
            asyncio.run(self.run_async())

        except OdooConnectionError as e:
            logger.error(f"Odoo connection error: {e}")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"Failed to start MCP server: {e}")
            sys.exit(1)

    def _handle_resource(self, resource: Resource) -> dict:
        """Handle resource requests from MCP clients.

        Args:
            resource: The resource request from the client

        Returns:
            dict: The resource content to return to the client

        Raises:
            ValueError: If the resource URI is invalid
        """
        logger.debug(f"Handling resource: {resource.uri}")

        try:
            # Get the registry to handle the resource
            # We don't need to access lifespan_context as we're using the registry directly
            return self.registry.handle_resource(resource)

        except Exception as e:
            logger.exception(f"Error handling resource {resource.uri}: {e}")
            error_message = str(e)

            # Return error response
            return {
                "is_error": True,
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: {error_message}",
                    },
                ],
            }
