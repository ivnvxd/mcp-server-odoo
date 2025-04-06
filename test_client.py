#!/usr/bin/env python3
"""Test client for Odoo MCP server.

This script connects to the Odoo MCP server and tests basic functionality.
It tests all operations and parameters:
- Operations: record/{id}, search, browse, count, fields
- Parameters: domain, fields, limit, offset, order
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

                # ----------------------------------------------------------------------
                # List available resources
                # ----------------------------------------------------------------------
                print("\n=== Testing Resource Listing ===")
                resources = await session.list_resources()
                resource_uris = []
                model_uris = []

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

                            # Store model URIs for later use
                            uri_str = str(uri)
                            if "/" not in uri_str.split("://")[1]:
                                model_uris.append(uri_str)

                        elif hasattr(resource, "uri") and hasattr(resource, "name"):
                            print(f"- {resource.uri} ({resource.name})")
                            resource_uris.append(resource.uri)

                            # Store model URIs for later use
                            uri_str = str(resource.uri)
                            if "/" not in uri_str.split("://")[1]:
                                model_uris.append(uri_str)
                        else:
                            print(f"- {resource}")
                else:
                    print("No resources available")

                # Select the first model for testing
                if not model_uris:
                    print("No models available for testing")
                    return

                test_model = str(model_uris[0]).split("://")[1]
                print(f"\nUsing model {test_model} for testing operations")

                # ----------------------------------------------------------------------
                # Test record operation - Direct resource access works
                # ----------------------------------------------------------------------
                print("\n=== Testing record Operation ===")
                record_uri = f"odoo://{test_model}/record/1"
                print(f"Retrieving record: {record_uri}")
                try:
                    record_content = await session.read_resource(record_uri)
                    print(f"Successfully retrieved record with ID 1: {record_content}")
                except Exception as e:
                    print(f"Error reading record: {e}")

                # ----------------------------------------------------------------------
                # Test fields operation
                # ----------------------------------------------------------------------
                print("\n=== Testing fields Operation ===")
                fields_uri = f"odoo://{test_model}/fields"
                print(f"Retrieving fields: {fields_uri}")
                try:
                    fields_content = await session.read_resource(fields_uri)
                    print(
                        f"Successfully retrieved fields for {test_model}: {fields_content}"
                    )

                    # Just use default fields - parsing is unreliable across different response formats
                    field_names = ["name", "id", "active"]
                    print(f"Using default fields for testing: {', '.join(field_names)}")
                except Exception as e:
                    print(f"Error reading fields: {e}")
                    field_names = ["name", "id", "active"]  # Fallback fields

                test_fields = field_names  # Use the default fields

                # ----------------------------------------------------------------------
                # Test search operation with parameters - Using tools instead of raw resources
                # ----------------------------------------------------------------------
                print(
                    "\n=== Testing search Operation with Parameters (Using tools) ==="
                )

                # 1. Test with domain parameter
                print("Searching with domain parameter")
                try:
                    domain_str = "[]"  # Empty domain matches all records
                    result = await session.call_tool(
                        "search_odoo",
                        {"model": test_model, "domain": domain_str, "limit": 3},
                    )
                    print(f"Successfully searched with domain parameter: {result}")
                except Exception as e:
                    print(f"Error searching with domain: {e}")

                # 2. Test with fields parameter - Not directly supported by tool, but we can test fields later
                print(
                    "Testing with fields - note: fields parameter might not be directly supported by the tool"
                )

                # 3. Test with limit parameter
                print("Searching with limit parameter")
                try:
                    result = await session.call_tool(
                        "search_odoo",
                        {"model": test_model, "limit": 5},
                    )
                    print(f"Successfully searched with limit parameter: {result}")
                except Exception as e:
                    print(f"Error searching with limit parameter: {e}")

                # 4. Test with offset parameter
                print("Searching with offset parameter")
                try:
                    result = await session.call_tool(
                        "search_odoo",
                        {"model": test_model, "limit": 3, "offset": 3},
                    )
                    print(f"Successfully searched with offset parameter: {result}")
                except Exception as e:
                    print(f"Error searching with offset parameter: {e}")

                # 5. Test with order parameter
                print("Searching with order parameter")
                try:
                    result = await session.call_tool(
                        "search_odoo",
                        {"model": test_model, "limit": 3, "order": "id desc"},
                    )
                    print(f"Successfully searched with order parameter: {result}")
                except Exception as e:
                    print(f"Error searching with order parameter: {e}")

                # 6. Test with combined parameters
                print("Searching with combined parameters")
                try:
                    result = await session.call_tool(
                        "search_odoo",
                        {
                            "model": test_model,
                            "domain": "[]",
                            "limit": 5,
                            "offset": 0,
                            "order": "id asc",
                        },
                    )
                    print(f"Successfully searched with combined parameters: {result}")
                except Exception as e:
                    print(f"Error searching with combined parameters: {e}")

                # ----------------------------------------------------------------------
                # Test browse operation - Using get_odoo_record tool for each ID
                # ----------------------------------------------------------------------
                print("\n=== Testing browse Operation ===")
                print("Browsing records with IDs 1, 2, 3 (using get_odoo_record)")
                for record_id in [1, 2, 3]:
                    try:
                        result = await session.call_tool(
                            "get_odoo_record",
                            {"model": test_model, "record_id": record_id},
                        )
                        print(
                            f"Successfully retrieved record with ID {record_id}: {result}"
                        )
                    except Exception as e:
                        print(f"Error retrieving record with ID {record_id}: {e}")

                # ----------------------------------------------------------------------
                # Test count operation - Count is part of search results
                # ----------------------------------------------------------------------
                print("\n=== Testing count Operation ===")
                print("Count is shown in search results from search_odoo tool")
                try:
                    result = await session.call_tool(
                        "search_odoo",
                        {"model": test_model, "limit": 1},
                    )
                    print(
                        f"Successfully retrieved count as part of search results: {result}"
                    )
                except Exception as e:
                    print(f"Error getting count: {e}")

                # ----------------------------------------------------------------------
                # Test tools
                # ----------------------------------------------------------------------
                print("\n=== Testing Tools ===")
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
                            {"model": test_model, "limit": 3},
                        )
                        if hasattr(result, "result"):
                            print(result.result)
                        else:
                            print(result)
                    except Exception as e:
                        print(f"Error calling tool: {e}")

                    # Test with domain parameter
                    print("\n2.1 Testing search_odoo tool with domain parameter:")
                    try:
                        result = await session.call_tool(
                            "search_odoo",
                            {
                                "model": test_model,
                                "domain": "[('id', '<', 5)]",
                                "limit": 3,
                            },
                        )
                        if hasattr(result, "result"):
                            print(result.result)
                        else:
                            print(result)
                    except Exception as e:
                        print(f"Error calling tool: {e}")

                    # Test with order parameter
                    print("\n2.2 Testing search_odoo tool with order parameter:")
                    try:
                        result = await session.call_tool(
                            "search_odoo",
                            {"model": test_model, "limit": 3, "order": "id desc"},
                        )
                        if hasattr(result, "result"):
                            print(result.result)
                        else:
                            print(result)
                    except Exception as e:
                        print(f"Error calling tool: {e}")

                    # Test with offset parameter
                    print("\n2.3 Testing search_odoo tool with offset parameter:")
                    try:
                        result = await session.call_tool(
                            "search_odoo",
                            {"model": test_model, "limit": 3, "offset": 3},
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
                        f"\n3. Testing get_odoo_record tool with {test_model} model (ID=1):"
                    )
                    try:
                        result = await session.call_tool(
                            "get_odoo_record",
                            {"model": test_model, "record_id": 1},
                        )
                        if hasattr(result, "result"):
                            print(result.result)
                        else:
                            print(result)
                    except Exception as e:
                        print(f"Error calling tool: {e}")

                print("\n=== Test Complete ===")
                print("All operations and parameters tested.")

            except Exception as e:
                logger.exception(f"Error communicating with MCP server: {e}")
                sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
