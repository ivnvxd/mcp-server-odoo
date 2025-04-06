#!/usr/bin/env python3
"""Test client for Odoo MCP server.

This script connects to the Odoo MCP server and tests basic functionality.
"""

import asyncio
import logging
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mcp_test_client")


async def main():
    """Connect to the MCP server and test functionality."""
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Get environment file path
    env_file = os.path.join(script_dir, ".env")

    # Create server parameters for stdio connection
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "mcp_server_odoo", "--env-file", env_file],
        cwd=script_dir,
        env=None,
    )

    print("Connecting to Odoo MCP server...")

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            try:
                # Initialize the connection
                print("Initializing connection...")
                await session.initialize()
                print("Connection initialized!")

                # List available resources
                print("\nListing available resources:")
                resources = await session.list_resources()
                resource_uris = []
                if resources:
                    # Check if resources is an object with a 'resources' attribute
                    if hasattr(resources, "resources"):
                        resource_list = resources.resources
                    else:
                        resource_list = resources

                    for resource in resource_list:
                        # Check if resource is a tuple or has uri/name attributes
                        if isinstance(resource, tuple) and len(resource) >= 2:
                            uri, name = resource[0], resource[1]
                            print(f"- {uri} ({name})")
                            resource_uris.append(uri)
                        elif hasattr(resource, "uri") and hasattr(resource, "name"):
                            print(f"- {resource.uri} ({resource.name})")
                            resource_uris.append(resource.uri)
                        else:
                            print(f"- {resource}")
                else:
                    print("No resources available")

                # Test retrieving a resource if available
                if resource_uris:
                    test_uri = resource_uris[0]
                    print(f"\nTesting resource retrieval for: {test_uri}")
                    try:
                        resource_content = await session.read_resource(test_uri)
                        print(resource_content)
                    except Exception as e:
                        print(f"Error reading resource: {e}")

                # List available tools
                print("\nListing available tools:")
                tools = await session.list_tools()
                found_tools = []
                if tools:
                    # Check if tools is an object with a 'tools' attribute
                    if hasattr(tools, "tools"):
                        tool_list = tools.tools
                    else:
                        tool_list = tools

                    for tool in tool_list:
                        # Check if tool is a tuple or has name/description attributes
                        if isinstance(tool, tuple) and len(tool) >= 2:
                            name, description = tool[0], tool[1]
                            print(f"- {name}: {description}")
                            found_tools.append(name)
                        elif hasattr(tool, "name") and hasattr(tool, "description"):
                            print(f"- {tool.name}: {tool.description}")
                            found_tools.append(tool.name)
                            if hasattr(tool, "parameters") and tool.parameters:
                                print("  Parameters:")
                                for param in tool.parameters:
                                    if (
                                        hasattr(param, "name")
                                        and hasattr(param, "description")
                                        and hasattr(param, "required")
                                    ):
                                        print(
                                            f"    - {param.name}: {param.description} (required: {param.required})"
                                        )
                                    else:
                                        print(f"    - {param}")
                        else:
                            print(f"- {tool}")
                else:
                    print("No tools available")

                # Test found tools
                print("\nTesting available tools:")

                # Test list_odoo_models tool if available
                if "list_odoo_models" in found_tools:
                    print("\n1. Testing list_odoo_models tool:")
                    try:
                        result = await session.call_tool(
                            "list_odoo_models",
                            {},
                        )
                        if hasattr(result, "result"):
                            print(result.result)
                        else:
                            print(result)
                    except Exception as e:
                        print(f"Error calling tool: {e}")

                # Test search_odoo tool if available
                if "search_odoo" in found_tools:
                    print("\n2. Testing search_odoo tool with res.partner model:")
                    try:
                        result = await session.call_tool(
                            "search_odoo",
                            {"model": "res.partner", "limit": 3},
                        )
                        if hasattr(result, "result"):
                            print(result.result)
                        else:
                            print(result)
                    except Exception as e:
                        print(f"Error calling tool: {e}")

                # Test get_odoo_record tool if available
                if "get_odoo_record" in found_tools:
                    print(
                        "\n3. Testing get_odoo_record tool with res.partner model (ID=1):"
                    )
                    try:
                        result = await session.call_tool(
                            "get_odoo_record",
                            {"model": "res.partner", "record_id": 1},
                        )
                        if hasattr(result, "result"):
                            print(result.result)
                        else:
                            print(result)
                    except Exception as e:
                        print(f"Error calling tool: {e}")

            except Exception as e:
                logger.exception(f"Error communicating with MCP server: {e}")
                sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
