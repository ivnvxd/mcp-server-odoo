"""MCP client validation tests.

Tests the MCP server through actual MCP protocol communication,
validating protocol compliance and response formats.

All tests are marked @pytest.mark.mcp — they need Odoo with MCP module installed.
"""

import asyncio
import logging
import os

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import Resource, TextContent, Tool

from .helpers.mcp_test_client import (
    MCPTestClient,
    check_server_capabilities,
)

logger = logging.getLogger(__name__)

# Expected tools registered by the server
EXPECTED_TOOLS = {
    "search_records",
    "get_record",
    "list_models",
    "create_record",
    "update_record",
    "delete_record",
    "list_resource_templates",
}

# Test configuration
TEST_CONFIG = {
    "ODOO_URL": os.getenv("ODOO_URL", "http://localhost:8069"),
    "ODOO_DB": os.getenv("ODOO_DB"),
    "ODOO_API_KEY": os.getenv("ODOO_API_KEY"),
}


@pytest.fixture
def test_env(monkeypatch):
    """Set test environment variables."""
    for key, value in TEST_CONFIG.items():
        if value is not None:
            monkeypatch.setenv(key, value)
    yield


@pytest.mark.mcp
class TestMCPProtocolCompliance:
    """Test MCP protocol compliance against a live server."""

    @pytest.mark.asyncio
    async def test_server_connection(self, test_env):
        """Test that stdio MCP connection establishes a valid session."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            assert connected_client.session is not None

    @pytest.mark.asyncio
    async def test_tool_listing(self, test_env):
        """Test that all expected tools are registered and have valid schemas."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            tools = await connected_client.list_tools()

            assert len(tools) >= len(EXPECTED_TOOLS)
            tool_names = {t.name for t in tools}
            assert EXPECTED_TOOLS.issubset(tool_names), (
                f"Missing tools: {EXPECTED_TOOLS - tool_names}"
            )

            for tool in tools:
                assert isinstance(tool, Tool)
                assert tool.name
                assert tool.description
                assert tool.input_schema is not None
                assert tool.input_schema.get("type") == "object"

    @pytest.mark.asyncio
    async def test_resource_listing_format(self, test_env):
        """Test that the server advertises its odoo:// resource templates."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            # The server's resource surface is URI templates — they must
            # always be advertised
            result = await connected_client.session.list_resource_templates()
            templates = {t.uri_template for t in result.resource_templates}
            for expected in (
                "odoo://{model}/record/{record_id}",
                "odoo://{model}/search",
                "odoo://{model}/count",
                "odoo://{model}/fields",
            ):
                assert expected in templates, f"Missing template: {expected}"

            # Concrete (non-template) resources may legitimately be absent,
            # but any returned must be well-formed
            resources = await connected_client.list_resources()
            assert isinstance(resources, list)
            for resource in resources:
                assert isinstance(resource, Resource)
                assert str(resource.uri).startswith("odoo://"), f"Bad URI: {resource.uri}"
                assert resource.name, f"Resource {resource.uri} has no name"

    @pytest.mark.asyncio
    async def test_read_resource_success(self, test_env):
        """Test reading a record resource returns the formatted record."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            # Find a real record id via the search tool's structured output
            search_result = await connected_client.call_tool(
                "search_records", {"model": "res.partner", "domain": [], "limit": 1}
            )
            records = search_result.structured_content["records"]
            assert records, "test database must contain at least one res.partner"
            record_id = records[0]["id"]

            # Read the record resource and verify it is that record
            text = await connected_client.read_resource(f"odoo://res.partner/record/{record_id}")
            assert f"Record: res.partner/{record_id}" in text
            assert "Name:" in text

    @pytest.mark.asyncio
    async def test_read_resource_not_found(self, test_env):
        """Test that reading a non-existent record raises an error."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            with pytest.raises(Exception) as exc_info:
                await connected_client.read_resource("odoo://res.partner/record/999999999")

            error_msg = str(exc_info.value).lower()
            assert "not found" in error_msg or "does not exist" in error_msg

    @pytest.mark.asyncio
    async def test_concurrent_operations(self, test_env):
        """Test that concurrent list_resources and list_tools don't interfere."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            results = await asyncio.gather(
                connected_client.list_resources(),
                connected_client.list_tools(),
                return_exceptions=True,
            )

            for i, r in enumerate(results):
                assert not isinstance(r, Exception), f"Task {i} failed: {r}"

            resources, tools = results
            assert isinstance(resources, list)
            assert isinstance(tools, list)
            assert len(tools) >= len(EXPECTED_TOOLS)


@pytest.mark.mcp
class TestMCPIntegration:
    """Test end-to-end MCP workflows."""

    @pytest.mark.asyncio
    async def test_search_records_workflow(self, test_env):
        """Test calling search_records tool returns partner data."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            search_result = await connected_client.call_tool(
                "search_records", {"model": "res.partner", "domain": [], "limit": 1}
            )
            assert search_result.content, "search_records returned no content"
            first = search_result.content[0]
            assert isinstance(first, TextContent)
            assert len(first.text) > 0

    @pytest.mark.asyncio
    async def test_error_handling_invalid_uri(self, test_env):
        """Test that an invalid URI scheme raises MCPError."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            with pytest.raises(MCPError):
                await connected_client.read_resource("invalid://uri")

    @pytest.mark.asyncio
    async def test_error_handling_nonexistent_record(self, test_env):
        """Test that a non-existent record raises MCPError."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            with pytest.raises(MCPError):
                await connected_client.read_resource("odoo://res.partner/record/999999999")


@pytest.mark.mcp
class TestMCPInspectorCompatibility:
    """Test compatibility with MCP Inspector requirements."""

    @pytest.mark.asyncio
    async def test_inspector_requirements(self, test_env):
        """Test that server exposes tools with valid schemas (Inspector requirement)."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            tools = await connected_client.list_tools()
            assert len(tools) >= len(EXPECTED_TOOLS)

            for tool in tools:
                assert tool.input_schema is not None
                assert tool.input_schema.get("type") == "object"

    @pytest.mark.asyncio
    async def test_server_capabilities(self, test_env):
        """Test that check_server_capabilities reports all capabilities as available."""
        client = MCPTestClient()
        async with client.connect() as connected_client:
            results = await check_server_capabilities(connected_client)

            assert results["server_info"] is True, "Server info capability missing"
            assert results["list_tools"] is True, "list_tools capability missing"
            # list_resources may be False if no resources are exposed, but the key must exist
            assert "list_resources" in results


@pytest.mark.mcp
class TestRealOdooServer:
    """Test with real Odoo server health endpoint."""

    @pytest.mark.asyncio
    async def test_real_server_health(self):
        """Test connection to real Odoo server via /mcp/health endpoint."""
        import urllib.request

        odoo_url = os.getenv("ODOO_URL", "http://localhost:8069")
        database = os.getenv("ODOO_DB", "odoo")
        req = urllib.request.Request(
            f"{odoo_url}/mcp/health",
            headers={"X-Odoo-Database": database},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200

        client = MCPTestClient()
        async with client.connect() as connected_client:
            resources = await connected_client.list_resources()
            assert isinstance(resources, list)

            tools = await connected_client.list_tools()
            assert isinstance(tools, list)
            assert len(tools) >= len(EXPECTED_TOOLS)
