import asyncio

import mcp_server_odoo.config
from mcp_server_odoo.server import OdooMCPServer


async def verify_schema():
    print("Initializing Odoo MCP Server...")
    config = mcp_server_odoo.config.OdooConfig(
        url="http://mock", database="mock", username="mock", password="mock"
    )
    server = OdooMCPServer(config=config)

    # Mock connection to avoid needing actual Odoo credentials
    server.connection = type("MockConnection", (), {"is_authenticated": True})()
    server.access_controller = type("MockAccessController", (), {})()

    print("Registering tools...")
    server._register_tools()

    print("Extracting tool schemas...")
    tools = server.app._tool_manager.list_tools()

    for tool in tools:
        if tool.name == "search_records":
            # domain should be a list or a string. Its schema might have anyOf or array type
            domain_param = tool.parameters.get("properties", {}).get("domain", {})
            print("Schema for search_records domain:")
            import json

            print(json.dumps(domain_param, indent=2))

            # verify type is not a list
            if isinstance(domain_param.get("type"), list):
                print("ERROR: type is still a list")
                return False

            if "anyOf" in domain_param or "allOf" in domain_param:
                print("ERROR: anyOf or allOf is still present")
                return False

    print("SUCCESS: Schemas are sanitized for n8n!")
    return True


if __name__ == "__main__":
    asyncio.run(verify_schema())
