"""MCP test client for protocol validation."""

import asyncio
import logging
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import (
    CallToolResult,
    Resource,
    TextContent,
    Tool,
)

logger = logging.getLogger(__name__)


class MCPTestClient:
    """Test client for validating MCP server functionality."""

    def __init__(self, server_command: Optional[List[str]] = None):
        """Initialize MCP test client.

        Args:
            server_command: Command to start the server.
                           Defaults to ["uvx", "mcp-server-odoo"]
        """
        # Use python module directly since package isn't published
        self.server_command = server_command or [sys.executable, "-m", "mcp_server_odoo"]
        self.session: Optional[ClientSession] = None
        self._server_process: Optional[subprocess.Popen] = None

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["MCPTestClient"]:
        """Connect to MCP server using stdio transport."""
        try:
            # Create server parameters
            server_params = StdioServerParameters(
                command=self.server_command[0],
                args=self.server_command[1:] if len(self.server_command) > 1 else [],
                env=dict(os.environ),  # Pass full environment (MCP default strips ODOO_* vars)
            )

            # Connect to server
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    self.session = session

                    # Initialize the connection
                    await session.initialize()
                    logger.info("Connected to MCP server")

                    yield self

        except Exception as e:
            logger.error(f"Failed to connect to MCP server: {e}")
            raise
        finally:
            self.session = None

    async def list_resources(self) -> List[Resource]:
        """List available resources from the server."""
        if not self.session:
            raise RuntimeError("Not connected to server")

        result = await self.session.list_resources()
        return result.resources

    async def read_resource(self, uri: str) -> str:
        """Read a resource by URI.

        Args:
            uri: Resource URI (e.g., "odoo://res.partner/record/1")

        Returns:
            Resource content as text
        """
        if not self.session:
            raise RuntimeError("Not connected to server")

        result = await self.session.read_resource(uri)

        # Extract text content
        if result.contents:
            for content in result.contents:
                if isinstance(content, TextContent):
                    return content.text

        return ""

    async def list_tools(self) -> List[Tool]:
        """List available tools from the server."""
        if not self.session:
            raise RuntimeError("Not connected to server")

        result = await self.session.list_tools()
        return result.tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> CallToolResult:
        """Call a tool with arguments.

        Args:
            name: Tool name
            arguments: Tool arguments

        Returns:
            Tool execution result
        """
        if not self.session:
            raise RuntimeError("Not connected to server")

        result = await self.session.call_tool(name, arguments)
        return result

    async def get_server_info(self) -> Dict[str, Any]:
        """Get server information from initialization."""
        if not self.session:
            raise RuntimeError("Not connected to server")

        # Access server info from session
        return {
            "name": getattr(self.session, "_server_name", "Unknown"),
            "version": getattr(self.session, "_server_version", "Unknown"),
            "capabilities": getattr(self.session, "_server_capabilities", {}),
        }

    @staticmethod
    def start_test_server(env: Optional[Dict[str, str]] = None) -> subprocess.Popen:
        """Start a test server instance.

        Args:
            env: Additional environment variables

        Returns:
            Server process
        """
        # Prepare environment
        test_env = dict(os.environ)
        if env:
            test_env.update(env)

        # Start server
        process = subprocess.Popen(
            ["uvx", "mcp-server-odoo"],
            env=test_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Give server time to start
        time.sleep(2)

        # Check if process is still running
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"Server failed to start:\nstdout: {stdout}\nstderr: {stderr}")

        return process

    @staticmethod
    def stop_test_server(process: subprocess.Popen) -> None:
        """Stop a test server instance."""
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def validate_mcp_response(response: Any, expected_type: type) -> bool:
    """Validate MCP response format.

    Args:
        response: Response to validate
        expected_type: Expected response type

    Returns:
        True if valid
    """
    if not isinstance(response, expected_type):
        logger.error(f"Invalid response type: expected {expected_type}, got {type(response)}")
        return False

    # Additional validation based on type
    if hasattr(response, "contents"):
        # Validate resource contents
        for content in response.contents:
            if not isinstance(content, (TextContent,)):
                logger.error(f"Invalid content type: {type(content)}")
                return False

    return True


async def check_server_capabilities(client: MCPTestClient) -> Dict[str, bool]:
    """Check server capabilities.

    Args:
        client: Connected MCP client

    Returns:
        Dictionary of capability test results
    """
    results = {}

    # Test resource listing
    try:
        resources = await client.list_resources()
        results["list_resources"] = len(resources) > 0
        logger.info(f"Found {len(resources)} resources")
    except Exception as e:
        logger.error(f"Failed to list resources: {e}")
        results["list_resources"] = False

    # Test tool listing
    try:
        tools = await client.list_tools()
        results["list_tools"] = len(tools) > 0
        logger.info(f"Found {len(tools)} tools")
    except Exception as e:
        logger.error(f"Failed to list tools: {e}")
        results["list_tools"] = False

    # Test server info
    try:
        info = await client.get_server_info()
        results["server_info"] = bool(info.get("name"))
        logger.info(f"Server: {info.get('name')} v{info.get('version')}")
    except Exception as e:
        logger.error(f"Failed to get server info: {e}")
        results["server_info"] = False

    return results


# For direct testing
if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO)

    async def main():
        # Check that required environment variables are set
        required_vars = ["ODOO_URL"]
        missing_vars = [var for var in required_vars if not os.getenv(var)]

        if missing_vars:
            print("ERROR: Missing required environment variables:")
            for var in missing_vars:
                print(f"  - {var}")
            print("\nPlease configure these in your .env file")
            print("Copy .env.example to .env and update with your values")
            return

        # Connect to server
        client = MCPTestClient()
        async with client.connect() as connected_client:
            # Test capabilities
            results = await check_server_capabilities(connected_client)

            print("\nCapability Test Results:")
            for capability, passed in results.items():
                status = "✓" if passed else "✗"
                print(f"  {status} {capability}")

            # Try to list resources
            if results.get("list_resources"):
                resources = await connected_client.list_resources()
                print(f"\nAvailable resources: {len(resources)}")
                for resource in resources[:5]:  # Show first 5
                    print(f"  - {resource.uri}: {resource.name}")
                if len(resources) > 5:
                    print(f"  ... and {len(resources) - 5} more")

    asyncio.run(main())
