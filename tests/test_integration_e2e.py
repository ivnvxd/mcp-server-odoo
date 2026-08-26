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
from mcp_server_odoo.error_handling import MCPPermissionError, NotFoundError, ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.resources import OdooResourceHandler
from mcp_server_odoo.tools import OdooToolHandler
from tests.helpers.server_testing import (
    MCPTestServer,
    OdooTestData,
    PerformanceTimer,
    assert_performance,
    check_odoo_health,
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

    def test_uvx_server_startup(self):
        """Test that server module is executable."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server_odoo", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0


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
            pytest.skip("API key not configured, cannot test auth rejection")


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
        assert "Showing records" in result
        # Must contain actual record data, not fake mock strings
        assert "Mock data" not in result

    @pytest.mark.asyncio
    async def test_search_with_domain(self, connected_env):
        """Test search with domain filter returns filtered results."""
        handler = connected_env["resource_handler"]
        conn = connected_env["connection"]
        import json
        from urllib.parse import quote

        domain = [["is_company", "=", True]]
        result = await handler._handle_search(
            "res.partner", quote(json.dumps(domain)), None, 5, 0, None
        )

        assert "res.partner" in result
        assert "Showing records" in result

        # The displayed total must match the domain-filtered count, proving
        # the domain was actually applied and not silently dropped
        expected = conn.search_count("res.partner", domain)
        assert expected > 0, "test database must contain at least one company partner"
        assert f"of {expected}" in result

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
        """Stored binaries render as fetchable URIs, never as inlined base64."""
        handler = connected_env["resource_handler"]
        conn = connected_env["connection"]

        partner_ids = conn.search("res.partner", [], limit=1)
        assert partner_ids
        partner_id = partner_ids[0]

        result = await handler._handle_record_retrieval("res.partner", str(partner_id))

        # A POPULATED binary is advertised as a resource URI plus its size, so
        # the client fetches it on demand instead of paying for base64 inline.
        # An empty one still renders its label ("image_1920: Not set"), so the
        # field being present says nothing about which form to expect — assert
        # per value, not per field name. (A demo database has partner images;
        # CI initializes with --without-demo, so both cases occur.)
        expected_uri = f"odoo://res.partner/record/{partner_id}/"
        checked = 0
        for line in result.splitlines():
            label, _, value = line.strip().partition(":")
            if label not in ("image_1920", "image_128"):
                continue
            checked += 1
            value = value.strip()
            assert value == "Not set" or value.startswith(f"{expected_uri}{label}"), (
                f"{label} rendered as {value!r}; expected 'Not set' or a resource URI"
            )
        assert checked, "no image field rendered — the record resource dropped binaries"
        # Whatever is rendered, no raw base64 payload leaks into the text
        assert "data:image" not in result
        assert "iVBORw0KGgo" not in result  # PNG base64 preamble


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
        # res.partner should always be available
        assert any(m["model"] == "res.partner" for m in result["models"])
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

    @pytest.mark.asyncio
    async def test_aggregate_records_count_only(self, connected_env):
        """aggregate_records: count partners by country via formatted_read_group.

        Requires the MCP module's whitelist to include
        ``"formatted_read_group": "read"`` (matches the Post-Completion step
        of the aggregate_records plan).
        """
        handler = connected_env["tool_handler"]

        result = await handler._handle_aggregate_records_tool(
            model="res.partner",
            groupby=["country_id"],
            aggregates=None,
            domain=None,
            order=None,
            limit=20,
            offset=0,
        )

        assert result["model"] == "res.partner"
        assert result["groupby"] == ["country_id"]
        # Tool defaults to ['__count'] when caller omits aggregates
        assert result["aggregates"] == ["__count"]
        assert isinstance(result["groups"], list)
        for bucket in result["groups"]:
            assert "__count" in bucket
            assert "country_id" in bucket

    @pytest.mark.asyncio
    async def test_aggregate_records_with_explicit_aggregate(self, connected_env):
        """aggregate_records with an explicit aggregate over a domain filter.

        Uses ``partner_share:count_distinct`` rather than ``id:count`` because
        v16's read_group silently elides ``id:count`` when ``__count`` is
        already implicit (lazy=False). Non-id aggregates are emitted on every
        supported Odoo version.
        """
        handler = connected_env["tool_handler"]
        ac = connected_env["access_controller"]

        try:
            ac.validate_model_access("res.partner", "read")
        except Exception:
            pytest.skip("No read permission on res.partner in current MCP config")

        result = await handler._handle_aggregate_records_tool(
            model="res.partner",
            groupby=["is_company"],
            aggregates=["partner_share:count_distinct"],
            domain=[["active", "=", True]],
            order=None,
            limit=10,
            offset=0,
        )

        assert result["aggregates"] == ["partner_share:count_distinct"]
        assert isinstance(result["groups"], list)
        for bucket in result["groups"]:
            assert "partner_share:count_distinct" in bucket

    @pytest.mark.asyncio
    async def test_aggregate_records_empty_groupby_overall_count(self, connected_env):
        """groupby=[] collapses to one overall row — the filtered-count path."""
        handler = connected_env["tool_handler"]
        ac = connected_env["access_controller"]

        try:
            ac.validate_model_access("res.partner", "read")
        except Exception:
            pytest.skip("No read permission on res.partner in current MCP config")

        result = await handler._handle_aggregate_records_tool(
            model="res.partner",
            groupby=[],
            aggregates=None,
            domain=[["active", "=", True]],
            order=None,
            limit=None,
            offset=0,
        )

        assert result["groupby"] == []
        assert result["aggregates"] == ["__count"]
        assert len(result["groups"]) == 1
        assert isinstance(result["groups"][0]["__count"], int)


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
