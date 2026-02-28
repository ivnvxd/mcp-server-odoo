#!/usr/bin/env python
"""Simple MCP server test script.

This script tests the MCP server by starting it and verifying basic functionality.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add parent directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).parent))

from mcp_server_odoo import OdooConfig, OdooMCPServer


async def test_server():
    """Test the MCP server."""
    print("Testing Odoo MCP Server")
    print("======================")
    print()

    # Check environment
    print("Environment Configuration:")
    print(f"  ODOO_URL: {os.getenv('ODOO_URL', 'Not set')}")
    print(f"  ODOO_DB: {os.getenv('ODOO_DB', 'Not set')}")
    print(
        f"  ODOO_API_KEY: {os.getenv('ODOO_API_KEY', 'Not set')[:10]}..."
        if os.getenv("ODOO_API_KEY")
        else "  ODOO_API_KEY: Not set"
    )
    print()

    # Create server
    try:
        config = OdooConfig.from_env()
        print("✓ Configuration loaded successfully")
    except Exception as e:
        print(f"✗ Failed to load configuration: {e}")
        return 1

    # Create server instance
    server = OdooMCPServer(config)
    print(f"✓ Server created: {server.app.name}")

    # Test connection
    try:
        server._ensure_connection()
        print("✓ Connected to Odoo successfully")
        print(f"  Database: {server.connection.database}")
        print(f"  User ID: {server.connection.uid}")
    except Exception as e:
        print(f"✗ Failed to connect to Odoo: {e}")
        return 1

    # Test resource registration
    try:
        server._register_resources()
        print("✓ Resources registered successfully")

        # Get resource handler info
        if server.resource_handler and server.access_controller:
            models = server.access_controller.get_enabled_models()
            print(f"  Models available: {len(models)}")
            if models:
                # Models is a list of dicts with 'model' key
                model_names = [m["model"] for m in models[:3]]
                print(f"  Example models: {', '.join(model_names)}")
    except Exception as e:
        print(f"✗ Failed to register resources: {e}")
        return 1

    print()
    print("Server is ready to handle MCP requests!")
    print()
    print("To test with MCP Inspector:")
    print("  npx @modelcontextprotocol/inspector python -m mcp_server_odoo")

    return 0


if __name__ == "__main__":
    # Set up test environment
    if not os.getenv("ODOO_URL"):
        os.environ["ODOO_URL"] = "http://localhost:8069"
    # ODOO_DB and ODOO_API_KEY should be set in environment

    # Run test
    exit_code = asyncio.run(test_server())
    sys.exit(exit_code)
