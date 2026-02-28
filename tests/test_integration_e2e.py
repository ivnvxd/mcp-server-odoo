"""End-to-end integration tests for Odoo MCP Server.

These tests validate real MCP server functionality against a running Odoo server.
They exercise the full stack: config -> connection -> access control -> resource/tool handlers -> formatters.

All tests require a running Odoo server with the MCP module installed.
"""

import os
import subprocess
import sys
import time
import uuid

import pytest
import requests
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import NotFoundError, ValidationError
from mcp_server_odoo.error_handling import PermissionError as MCPPermissionError
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.resources import OdooResourceHandler
from mcp_server_odoo.tools import OdooToolHandler
from tests.helpers.server_testing import (
    MCPTestServer,
    OdooTestData,
    PerformanceTimer,
    assert_performance,
    check_odoo_health,
    create_test_env_file,
    mcp_test_server,
)

# Mark all tests in this module as requiring Odoo with MCP module
pytestmark = [pytest.mark.mcp]


def _resolve_db_header(config: OdooConfig) -> dict[str, str]:
    """Resolve target database and return X-Odoo-Database header dict."""
    conn = OdooConnection(config)
    conn.connect()
    try:
        db = conn.auto_select_database()
    finally:
        conn.disconnect()
    return {"X-Odoo-Database": db} if db else {}


@pytest.fixture
def config():
    """Load configuration from environment."""
    return OdooConfig.from_env()


@pytest.fixture
def connected_env(config):
    """Create a fully connected environment with real handlers."""
    conn = OdooConnection(config)
    conn.connect()
    conn.authenticate()

    access_controller = AccessController(config)
    app = FastMCP("test-e2e")

    resource_handler = OdooResourceHandler(app, conn, access_controller, config)
    tool_handler = OdooToolHandler(app, conn, access_controller, config)

    yield {
        "config": config,
        "connection": conn,
        "access_controller": access_controller,
        "resource_handler": resource_handler,
        "tool_handler": tool_handler,
        "app": app,
    }

    conn.disconnect()


@pytest.fixture
def test_data(config):
    """Create and manage test data with automatic cleanup."""
    conn = OdooConnection(config)
    conn.connect()
    conn.authenticate()

    data = OdooTestData(conn)
    yield data
    data.cleanup()

    conn.disconnect()


class TestServerLifecycle:
    """Test MCP server lifecycle management."""

    @pytest.mark.asyncio
    async def test_server_startup_and_shutdown(self, config):
        """Test that server can start up and shut down cleanly."""
        server = MCPTestServer(config)

        await server.start()
        assert server.server is not None
        assert server.odoo_connection is not None
        assert server.odoo_connection.is_connected

        await server.stop()
        assert server.server is None
        assert server.odoo_connection is None

    def test_server_subprocess_lifecycle(self, config):
        """Test server can be started as a subprocess."""
        with mcp_test_server(config) as server:
            process = server.start_subprocess()
            assert process is not None
            assert process.poll() is None

            time.sleep(0.5)
            assert process.poll() is None

        assert server.server_process is None

    def test_server_with_env_file(self, tmp_path, config):
        """Test server can load configuration from .env file."""
        create_test_env_file(tmp_path)

        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            loaded = OdooConfig.from_env()
            assert loaded.url == os.getenv("ODOO_URL", "http://localhost:8069")
            assert loaded.api_key == (os.getenv("ODOO_API_KEY") or None)
        finally:
            os.chdir(original_cwd)

    def test_uvx_server_startup(self):
        """Test that server module is executable."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server_odoo", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0 or "MCP" in result.stdout or "MCP" in result.stderr


class TestAuthenticationFlows:
    """Test authentication flows with different configurations."""

    def test_api_key_authentication_from_env(self, config):
        """Test API key authentication using .env configuration."""
        if not config.api_key:
            pytest.skip("ODOO_API_KEY not configured")

        conn = OdooConnection(config)
        conn.connect()
        conn.authenticate()

        assert conn.is_connected
        assert conn.uid is not None

        version = conn.get_server_version()
        assert version is not None
        conn.close()

    def test_username_password_fallback(self, config):
        """Test fallback to username/password when API key fails."""
        fallback_config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="invalid_key",
            database=os.getenv("ODOO_DB"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
        )

        conn = OdooConnection(fallback_config)
        conn.connect()
        conn.authenticate()
        assert conn.is_connected
        conn.close()

    def test_rest_api_authentication(self, config):
        """Test REST API authentication with API key."""
        db_header = _resolve_db_header(config)

        response = requests.get(f"{config.url}/mcp/health", headers=db_header)
        assert response.status_code == 200

        headers = {"X-API-Key": config.api_key, **db_header}
        response = requests.get(f"{config.url}/mcp/system/info", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True
        assert "db_name" in data.get("data", {})

    def test_authentication_error_handling(self, config):
        """Test proper error handling for authentication failures."""
        db_header = _resolve_db_header(config)

        headers = {"X-API-Key": "invalid_key", **db_header}
        response = requests.get(f"{config.url}/mcp/system/info", headers=headers)

        if config.api_key:
            assert response.status_code == 401
        else:
            assert response.status_code == 200


class TestResourceOperations:
    """Test all resource operations with real Odoo data."""

    @pytest.mark.asyncio
    async def test_record_retrieval(self, connected_env):
        """Test retrieving a real record via resource handler."""
        handler = connected_env["resource_handler"]
        conn = connected_env["connection"]

        partner_ids = conn.search("res.partner", [], limit=1)
        assert partner_ids, "No partners found in Odoo"

        result = await handler._handle_record_retrieval("res.partner", str(partner_ids[0]))

        assert f"Record: res.partner/{partner_ids[0]}" in result
        assert "Name:" in result
        assert "=" * 50 in result

    @pytest.mark.asyncio
    async def test_search_operation(self, connected_env):
        """Test search with real data returns properly formatted results."""
        handler = connected_env["resource_handler"]

        result = await handler._handle_search("res.partner", None, None, 5, 0, None)

        assert "res.partner" in result
        assert "Total:" in result or "record" in result.lower()
        # Must contain actual record data, not fake mock strings
        assert "Mock data" not in result

    @pytest.mark.asyncio
    async def test_search_with_domain(self, connected_env):
        """Test search with domain filter returns filtered results."""
        handler = connected_env["resource_handler"]
        import json
        from urllib.parse import quote

        domain = json.dumps([["is_company", "=", True]])
        result = await handler._handle_search("res.partner", quote(domain), None, 5, 0, None)

        assert "res.partner" in result

    @pytest.mark.asyncio
    async def test_count_operation(self, connected_env):
        """Test count returns real record count."""
        handler = connected_env["resource_handler"]
        conn = connected_env["connection"]

        result = await handler._handle_count("res.partner", None)

        assert "Count Result: res.partner" in result
        assert "Total count:" in result

        # Verify the count matches a direct search_count
        real_count = conn.search_count("res.partner", [])
        assert f"{real_count:,}" in result

    @pytest.mark.asyncio
    async def test_fields_operation(self, connected_env):
        """Test fields returns real model field definitions."""
        handler = connected_env["resource_handler"]

        result = await handler._handle_fields("res.partner")

        assert "Field Definitions: res.partner" in result
        assert "Total fields:" in result
        # Real res.partner must have these
        assert "name:" in result
        assert "CHAR Fields" in result
        assert "MANY2ONE Fields" in result

    @pytest.mark.asyncio
    async def test_record_safe_field_filtering(self, connected_env):
        """Test that binary/html/serialized fields are excluded from record retrieval."""
        handler = connected_env["resource_handler"]
        conn = connected_env["connection"]

        partner_ids = conn.search("res.partner", [], limit=1)
        assert partner_ids

        result = await handler._handle_record_retrieval("res.partner", str(partner_ids[0]))

        # Binary fields like image_1920 should NOT appear in the output
        assert "image_1920:" not in result
        assert "image_128:" not in result


class TestToolOperations:
    """Test tool handlers with real Odoo data."""

    @pytest.mark.asyncio
    async def test_search_records_tool(self, connected_env):
        """Test search_records tool returns real SearchResult data."""
        handler = connected_env["tool_handler"]

        result = await handler._handle_search_tool("res.partner", None, None, 5, 0, None, None)

        assert result["total"] > 0
        assert len(result["records"]) <= 5
        assert result["model"] == "res.partner"
        for record in result["records"]:
            assert "id" in record
            assert isinstance(record["id"], int)

    @pytest.mark.asyncio
    async def test_get_record_tool_smart_defaults(self, connected_env):
        """Test get_record tool with smart field selection."""
        handler = connected_env["tool_handler"]
        conn = connected_env["connection"]

        partner_ids = conn.search("res.partner", [], limit=1)
        assert partner_ids

        result = await handler._handle_get_record_tool("res.partner", partner_ids[0], None, None)

        assert result.record["id"] == partner_ids[0]
        # Smart defaults should include essential fields
        assert "name" in result.record or "display_name" in result.record
        # Should include field selection metadata
        assert result.metadata is not None
        assert result.metadata.field_selection_method == "smart_defaults"

    @pytest.mark.asyncio
    async def test_get_record_tool_specific_fields(self, connected_env):
        """Test get_record with explicit field list."""
        handler = connected_env["tool_handler"]
        conn = connected_env["connection"]

        partner_ids = conn.search("res.partner", [], limit=1)
        assert partner_ids

        result = await handler._handle_get_record_tool(
            "res.partner", partner_ids[0], ["name", "email"], None
        )

        assert "name" in result.record
        assert "email" in result.record
        # Should NOT have fields we did not ask for (except id)
        non_requested = set(result.record.keys()) - {"id", "name", "email"}
        assert len(non_requested) == 0, f"Got unexpected fields: {non_requested}"

    @pytest.mark.asyncio
    async def test_list_models_tool(self, connected_env):
        """Test list_models returns real model list."""
        handler = connected_env["tool_handler"]

        result = await handler._handle_list_models_tool(None)

        assert "models" in result
        assert len(result["models"]) > 0
        for model in result["models"]:
            assert "model" in model

    @pytest.mark.asyncio
    async def test_list_resource_templates_tool(self, connected_env):
        """Test list_resource_templates returns template information."""
        handler = connected_env["tool_handler"]

        result = await handler._handle_list_resource_templates_tool(None)

        assert "templates" in result
        assert len(result["templates"]) > 0

    @pytest.mark.asyncio
    async def test_create_update_delete_cycle(self, connected_env):
        """Test full CRUD lifecycle using res.company (has full CRUD permissions in MCP config)."""
        handler = connected_env["tool_handler"]
        ac = connected_env["access_controller"]

        try:
            ac.validate_model_access("res.company", "create")
        except Exception:
            pytest.skip("No create permission on res.company in current MCP config")

        # Use unique name to avoid constraint violations from leftover test data
        unique = uuid.uuid4().hex[:8]
        company_name = f"E2E Test Company {unique}"

        # Create
        create_result = await handler._handle_create_record_tool(
            "res.company",
            {"name": company_name},
            None,
        )
        assert create_result["success"] is True
        record_id = create_result["record"]["id"]
        assert isinstance(record_id, int)
        assert record_id > 0

        try:
            # Update
            updated_name = f"E2E Test Company Updated {unique}"
            update_result = await handler._handle_update_record_tool(
                "res.company",
                record_id,
                {"name": updated_name},
                None,
            )
            assert update_result["success"] is True

            # Verify update via get_record
            get_result = await handler._handle_get_record_tool(
                "res.company", record_id, ["name"], None
            )
            assert get_result.record["name"] == updated_name

            # Delete
            delete_result = await handler._handle_delete_record_tool("res.company", record_id, None)
            assert delete_result["success"] is True

            # Verify deletion
            with pytest.raises((NotFoundError, ValidationError)):
                await handler._handle_get_record_tool("res.company", record_id, ["name"], None)
        except Exception:
            # Cleanup on failure
            try:
                connected_env["connection"].unlink("res.company", [record_id])
            except Exception:
                pass
            raise


class TestErrorHandling:
    """Test error handling with real server."""

    @pytest.mark.asyncio
    async def test_record_not_found(self, connected_env):
        """Test retrieving a non-existent record."""
        handler = connected_env["resource_handler"]

        with pytest.raises(NotFoundError) as exc_info:
            await handler._handle_record_retrieval("res.partner", "999999999")

        assert (
            "not found" in str(exc_info.value).lower()
            or "does not exist" in str(exc_info.value).lower()
        )

    @pytest.mark.asyncio
    async def test_invalid_model_error(self, connected_env):
        """Test error handling for invalid/inaccessible model."""
        handler = connected_env["resource_handler"]

        with pytest.raises((MCPPermissionError, ValidationError)):
            await handler._handle_record_retrieval("nonexistent.model.xyz", "1")

    @pytest.mark.asyncio
    async def test_invalid_record_id(self, connected_env):
        """Test error handling for invalid record IDs."""
        handler = connected_env["resource_handler"]

        with pytest.raises(ValidationError):
            await handler._handle_record_retrieval("res.partner", "abc")

        with pytest.raises(ValidationError):
            await handler._handle_record_retrieval("res.partner", "-5")

    def test_connection_failure_recovery(self, config):
        """Test recovery from connection failures."""
        conn = OdooConnection(config)

        conn.connect()
        conn.authenticate()
        assert conn.is_connected

        conn.close()
        assert not conn.is_connected

        # Reconnect
        conn.connect()
        conn.authenticate()
        version = conn.get_server_version()
        assert version is not None
        assert conn.is_connected
        conn.close()


class TestPerformanceAndReliability:
    """Test performance and reliability aspects."""

    @pytest.mark.asyncio
    async def test_connection_reuse(self, config):
        """Test that connections are properly reused."""
        async with MCPTestServer(config) as server:
            await server.start()
            conn = server.odoo_connection

            for _ in range(5):
                version = conn.get_server_version()
                assert version is not None

            assert conn.is_connected

    @pytest.mark.asyncio
    async def test_operation_performance(self, connected_env):
        """Test that operations complete within acceptable time."""
        handler = connected_env["resource_handler"]
        conn = connected_env["connection"]

        partner_ids = conn.search("res.partner", [], limit=1)
        assert partner_ids

        operations = [
            (
                "Record fetch",
                lambda: handler._handle_record_retrieval("res.partner", str(partner_ids[0])),
                3.0,
            ),
            ("Search", lambda: handler._handle_search("res.partner", None, None, 10, 0, None), 3.0),
            ("Field metadata", lambda: handler._handle_fields("res.partner"), 3.0),
            ("Count", lambda: handler._handle_count("res.partner", None), 2.0),
        ]

        for op_name, op_func, max_time in operations:
            with PerformanceTimer(op_name) as timer:
                await op_func()
            assert_performance(op_name, timer.elapsed, max_time)

    def test_server_health_monitoring(self, config):
        """Test server health check functionality."""
        db_header = _resolve_db_header(config)
        db_name = db_header.get("X-Odoo-Database")

        if config.api_key:
            is_healthy = check_odoo_health(config.url, config.api_key, database=db_name)
            assert is_healthy

            is_healthy = check_odoo_health(config.url, "invalid_key", database=db_name)
            assert not is_healthy
        else:
            response = requests.get(f"{config.url}/mcp/health", headers=db_header, timeout=5)
            assert response.status_code == 200
