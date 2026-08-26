"""End-to-end tests for YOLO mode functionality.

This module tests complete YOLO mode workflows with real Odoo instances.
Tests are marked with @pytest.mark.yolo and require a running Odoo instance.
"""

import base64
import os
import time
from unittest.mock import MagicMock

import pytest

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.tools import OdooToolHandler


@pytest.mark.yolo
class TestYoloModeE2E:
    """End-to-end tests for YOLO mode with real Odoo."""

    @pytest.fixture
    def config_read_only(self):
        """Create configuration for read-only YOLO mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            database=os.getenv("ODOO_DB"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            yolo_mode="read",
        )

    @pytest.fixture
    def config_full_access(self):
        """Create configuration for full access YOLO mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            database=os.getenv("ODOO_DB"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            yolo_mode="true",
        )

    @pytest.fixture
    def config_standard(self):
        """Create configuration for standard mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            database=os.getenv("ODOO_DB"),
            api_key=os.getenv("ODOO_API_KEY"),
        )

    @pytest.mark.asyncio
    async def test_yolo_complete_workflow_read_only(self, config_read_only):
        """Test complete workflow in read-only YOLO mode."""
        # 1. Connect and authenticate
        connection = OdooConnection(config_read_only)
        connection.connect()
        connection.authenticate()

        assert connection.is_authenticated, "Failed to authenticate in read-only mode"

        # Setup tool handler
        app = MagicMock()
        access_controller = AccessController(config_read_only)
        handler = OdooToolHandler(app, connection, access_controller, config_read_only)

        # 2. List models - should work and show indicator
        # YOLO mode returns raw dict, not ModelsResult
        models_result = await handler._handle_list_models_tool()
        assert "models" in models_result
        assert "yolo_mode" in models_result
        assert len(models_result["models"]) > 0

        # Check for YOLO metadata
        yolo_meta = models_result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "read"
        assert "READ-ONLY" in yolo_meta["description"]

        # 3. Search records - should work
        search_result = await handler._handle_search_tool(
            model="res.partner",
            domain=[],
            fields=["id", "name", "email"],
            limit=5,
            offset=0,
            order=None,
        )

        assert "records" in search_result
        assert search_result["total"] > 0  # res.partner always has records

        # 4. Get a specific record - should work
        if search_result["records"]:
            first_record = search_result["records"][0]
            get_result = await handler._handle_get_record_tool(
                model="res.partner",
                record_id=first_record["id"],
                fields=None,
            )
            assert "id" in get_result.record
            assert get_result.record["id"] == first_record["id"]

        # 5. Attempt to create record - should fail
        with pytest.raises(ValidationError) as exc_info:
            await handler._handle_create_record_tool(
                model="res.partner",
                values={"name": "YOLO Test Partner - Should Fail"},
            )
        assert "not allowed in read-only" in str(exc_info.value).lower()

        # 6. Attempt to update record - should fail
        if search_result["records"]:
            with pytest.raises(ValidationError) as exc_info:
                await handler._handle_update_record_tool(
                    model="res.partner",
                    record_id=first_record["id"],
                    values={"email": "test@fail.com"},
                )
            assert "not allowed in read-only" in str(exc_info.value).lower()

        # 7. Attempt to delete record - should fail
        if search_result["records"]:
            with pytest.raises(ValidationError) as exc_info:
                await handler._handle_delete_record_tool(
                    model="res.partner",
                    record_id=first_record["id"],
                )
            assert "not allowed in read-only" in str(exc_info.value).lower()

        connection.disconnect()

    @pytest.mark.asyncio
    async def test_yolo_complete_workflow_full_access(self, config_full_access):
        """Test complete workflow in full access YOLO mode."""
        # 1. Connect and authenticate
        connection = OdooConnection(config_full_access)
        connection.connect()
        connection.authenticate()

        assert connection.is_authenticated, "Failed to authenticate in full access mode"

        # Setup tool handler
        app = MagicMock()
        access_controller = AccessController(config_full_access)
        handler = OdooToolHandler(app, connection, access_controller, config_full_access)

        # 2. List models - should work and show warning
        # YOLO mode returns raw dict, not ModelsResult
        models_result = await handler._handle_list_models_tool()
        assert "models" in models_result
        assert "yolo_mode" in models_result

        # Check for YOLO metadata
        yolo_meta = models_result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "true"
        assert "FULL ACCESS" in yolo_meta["description"]

        # 3. Create a test record - should work
        create_result = await handler._handle_create_record_tool(
            model="res.partner",
            values={
                "name": "YOLO E2E Test Partner",
                "email": "yolo.e2e@test.com",
                "is_company": True,
            },
        )

        assert create_result["success"] is True
        created_id = create_result["record"]["id"]
        assert created_id > 0

        # 4. Search for the created record - should work
        search_result = await handler._handle_search_tool(
            model="res.partner",
            domain=[["id", "=", created_id]],
            fields=["id", "name", "email"],
            limit=1,
            offset=0,
            order=None,
        )

        assert search_result["total"] == 1
        assert search_result["records"][0]["name"] == "YOLO E2E Test Partner"

        # 5. Update the record - should work
        update_result = await handler._handle_update_record_tool(
            model="res.partner",
            record_id=created_id,
            values={"email": "updated.yolo@test.com", "phone": "+1234567890"},
        )

        assert update_result["success"] is True

        # 6. Verify update
        get_result = await handler._handle_get_record_tool(
            model="res.partner",
            record_id=created_id,
            fields=["id", "name", "email", "phone"],
        )

        assert get_result.record["email"] == "updated.yolo@test.com"
        assert get_result.record["phone"] == "+1234567890"

        # 7. Delete the record - should work
        delete_result = await handler._handle_delete_record_tool(
            model="res.partner",
            record_id=created_id,
        )

        assert delete_result["success"] is True

        # 8. Verify deletion
        search_after_delete = await handler._handle_search_tool(
            model="res.partner",
            domain=[["id", "=", created_id]],
            fields=["id"],
            limit=1,
            offset=0,
            order=None,
        )

        assert search_after_delete["total"] == 0

        connection.disconnect()

    @pytest.mark.asyncio
    async def test_model_access_different_types(self, config_full_access):
        """Test accessing different types of models in YOLO mode."""
        connection = OdooConnection(config_full_access)
        connection.connect()
        connection.authenticate()

        app = MagicMock()
        access_controller = AccessController(config_full_access)
        handler = OdooToolHandler(app, connection, access_controller, config_full_access)

        # Test standard models
        standard_models = ["res.partner", "res.users", "res.company", "res.country"]
        for model in standard_models:
            result = await handler._handle_search_tool(
                model=model,
                domain=[],
                fields=["id"],
                limit=1,
                offset=0,
                order=None,
            )
            assert "records" in result, f"Failed to access standard model: {model}"
            assert result["total"] > 0, f"Expected records in {model}"

        # Test system models (usually restricted in standard mode)
        system_models = ["ir.model", "ir.model.fields", "ir.config_parameter"]
        for model in system_models:
            result = await handler._handle_search_tool(
                model=model,
                domain=[],
                fields=["id"],
                limit=1,
                offset=0,
                order=None,
            )
            assert "records" in result, f"Failed to access system model: {model}"

        connection.disconnect()

    @pytest.mark.asyncio
    async def test_error_handling(self, config_full_access):
        """Test error handling in YOLO mode."""
        connection = OdooConnection(config_full_access)
        connection.connect()
        connection.authenticate()

        app = MagicMock()
        access_controller = AccessController(config_full_access)
        handler = OdooToolHandler(app, connection, access_controller, config_full_access)

        # Test invalid model name
        with pytest.raises(ValidationError) as exc_info:
            await handler._handle_search_tool(
                model="invalid.model.name",
                domain=[],
                fields=["id"],
                limit=1,
                offset=0,
                order=None,
            )
        assert "invalid.model.name" in str(exc_info.value)

        # Test invalid field name
        with pytest.raises(ValidationError) as exc_info:
            await handler._handle_search_tool(
                model="res.partner",
                domain=[],
                fields=["id", "invalid_field_xyz"],
                limit=1,
                offset=0,
                order=None,
            )
        assert "invalid_field_xyz" in str(exc_info.value)

        # Test invalid record ID
        with pytest.raises(ValidationError) as exc_info:
            await handler._handle_get_record_tool(
                model="res.partner",
                record_id=999999999,  # Very unlikely to exist
                fields=["id", "name"],
            )
        assert "not found" in str(exc_info.value).lower()

        # Test creating record with missing required fields
        with pytest.raises(ValidationError) as exc_info:
            await handler._handle_create_record_tool(
                model="res.users",  # Requires login field
                values={"name": "Test User Without Login"},
            )
        err_msg = str(exc_info.value).lower()
        assert "required" in err_msg or "login" in err_msg, (
            f"Expected error about missing required fields, got: {exc_info.value}"
        )

        connection.disconnect()

    @pytest.mark.asyncio
    async def test_mode_indicators_in_responses(self, config_read_only, config_full_access):
        """Test that mode indicators appear correctly in responses."""
        # Test read-only mode indicators
        connection = OdooConnection(config_read_only)
        connection.connect()
        connection.authenticate()

        app = MagicMock()
        access_controller = AccessController(config_read_only)
        handler = OdooToolHandler(app, connection, access_controller, config_read_only)

        # Check list_models indicator
        models_result = await handler._handle_list_models_tool()
        yolo_meta = models_result["yolo_mode"]
        assert "READ-ONLY" in yolo_meta["description"]
        assert yolo_meta["operations"]["read"] is True
        assert yolo_meta["operations"]["write"] is False

        connection.disconnect()

        # Test full access mode indicators
        connection = OdooConnection(config_full_access)
        connection.connect()
        connection.authenticate()

        access_controller = AccessController(config_full_access)
        handler = OdooToolHandler(app, connection, access_controller, config_full_access)

        # Check list_models indicator
        models_result = await handler._handle_list_models_tool()
        yolo_meta = models_result["yolo_mode"]
        assert "FULL ACCESS" in yolo_meta["description"]
        assert all(
            [
                yolo_meta["operations"]["read"],
                yolo_meta["operations"]["write"],
                yolo_meta["operations"]["create"],
                yolo_meta["operations"]["unlink"],
            ]
        )

        connection.disconnect()

    @pytest.mark.asyncio
    async def test_performance_comparison(self, config_full_access):
        """Test performance of YOLO mode operations."""
        connection = OdooConnection(config_full_access)
        connection.connect()
        connection.authenticate()

        app = MagicMock()
        access_controller = AccessController(config_full_access)
        handler = OdooToolHandler(app, connection, access_controller, config_full_access)

        # Measure list_models performance
        start_time = time.time()
        models_result = await handler._handle_list_models_tool()
        list_models_time = time.time() - start_time

        assert list_models_time < 2.0, f"list_models took too long: {list_models_time:.2f}s"
        assert len(models_result["models"]) > 50, "Should list many models in YOLO mode"

        # Measure search performance
        start_time = time.time()
        search_result = await handler._handle_search_tool(
            model="res.partner",
            domain=[],
            fields=["id", "name"],
            limit=100,
            offset=0,
            order="id ASC",
        )
        search_time = time.time() - start_time

        assert search_time < 1.0, f"Search took too long: {search_time:.2f}s"

        # Measure bulk operations performance
        if search_result["total"] >= 10:
            # Read 10 records individually
            start_time = time.time()
            for record in search_result["records"][:10]:
                await handler._handle_get_record_tool(
                    model="res.partner",
                    record_id=record["id"],
                    fields=["id", "name", "email"],
                )
            bulk_read_time = time.time() - start_time

            assert bulk_read_time < 2.0, f"Bulk read took too long: {bulk_read_time:.2f}s"

        connection.disconnect()

    @pytest.mark.asyncio
    async def test_no_mcp_module_required(self, config_full_access):
        """Test that YOLO mode works without MCP module installed in Odoo."""
        # This test verifies YOLO mode connects to standard endpoints
        connection = OdooConnection(config_full_access)

        # Check that we're using standard endpoints
        assert connection.COMMON_ENDPOINT == "/xmlrpc/2/common"
        assert connection.OBJECT_ENDPOINT == "/xmlrpc/2/object"
        assert connection.DB_ENDPOINT == "/xmlrpc/db"

        # Should be able to connect and work
        connection.connect()
        connection.authenticate()
        assert connection.is_authenticated

        # Should be able to perform operations
        result = connection.search_read(
            "res.partner",
            [["is_company", "=", True]],
            ["id", "name"],
            limit=5,
        )
        assert isinstance(result, list)

        connection.disconnect()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])


class TestYoloOptInValidation:
    """Unit tests for YOLO mode opt-in validation (no Odoo needed)."""

    def test_explicit_opt_in_requirement(self):
        """Test that only 'true' enables full access, not other truthy values."""
        valid_cases = [
            ("true", True),
            ("read", False),
            ("off", False),
        ]

        for value, should_allow_write in valid_cases:
            config = OdooConfig(
                url=os.getenv("ODOO_URL", "http://localhost:8069"),
                database=os.getenv("ODOO_DB"),
                username=os.getenv("ODOO_USER", "admin"),
                password=os.getenv("ODOO_PASSWORD", "admin"),
                yolo_mode=value,
            )

            if value in ["true", "read"]:
                assert config.is_yolo_enabled

                if value == "true":
                    assert config.yolo_mode == "true"
                    access_controller = AccessController(config)
                    allowed, _ = access_controller.check_operation_allowed("res.partner", "write")
                    assert allowed == should_allow_write, (
                        f"Value '{value}' should"
                        f" {'allow' if should_allow_write else 'not allow'} write"
                    )
                else:
                    assert config.yolo_mode == "read"
                    access_controller = AccessController(config)
                    allowed, _ = access_controller.check_operation_allowed("res.partner", "write")
                    assert not allowed, "Read mode should not allow write"
            else:
                assert not config.is_yolo_enabled
                assert config.yolo_mode == "off"

        invalid_cases = ["True", "1", "yes", "on", "false", "full", ""]

        for value in invalid_cases:
            with pytest.raises(ValueError, match="Invalid YOLO mode"):
                OdooConfig(
                    url="http://localhost:8069",
                    database=os.getenv("ODOO_DB", "mcp-18"),
                    username=os.getenv("ODOO_USER", "admin"),
                    password=os.getenv("ODOO_PASSWORD", "admin"),
                    yolo_mode=value,
                )


@pytest.mark.yolo
class TestDynamicInstructionsE2E:
    """initialize carries personalized instructions; get_current_context matches."""

    @pytest.mark.asyncio
    async def test_initialize_instructions_and_tool_match(self, monkeypatch):
        from tests.helpers.mcp_test_client import MCPTestClient

        # Spawn the real server (stdio) in YOLO read mode
        monkeypatch.setenv("ODOO_URL", os.getenv("ODOO_URL", "http://localhost:8069"))
        monkeypatch.setenv("ODOO_USER", os.getenv("ODOO_USER", "admin"))
        monkeypatch.setenv("ODOO_PASSWORD", os.getenv("ODOO_PASSWORD", "admin"))
        monkeypatch.setenv("ODOO_YOLO", "read")
        if os.getenv("ODOO_DB"):
            monkeypatch.setenv("ODOO_DB", os.getenv("ODOO_DB"))

        client = MCPTestClient()
        async with client.connect():
            instructions = client.initialize_result.instructions
            assert instructions, "initialize must carry instructions"
            # Static description retained as the first line
            assert instructions.startswith("MCP server for accessing and managing Odoo ERP data")
            assert "You are connected to Odoo via MCP as:" in instructions
            assert "(login: admin)" in instructions
            assert "Datetime handling:" in instructions

            result = await client.call_tool("get_current_context", {})
            structured = result.structuredContent
            assert structured is not None
            assert structured["login"] == "admin"
            # The tool text is exactly the dynamic block inside the instructions
            assert structured["text"] in instructions


@pytest.mark.yolo
class TestBinaryResourcesE2E:
    """Binary field & attachment resources round-trip against live Odoo."""

    # 1x1 transparent PNG — starts with the PNG magic bytes
    PNG_BYTES = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    PDF_BYTES = b"%PDF-1.4 binary e2e test payload"

    @pytest.fixture
    def config_full_access(self):
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            database=os.getenv("ODOO_DB"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            yolo_mode="true",
        )

    async def _read_resource(self, app, uri: str):
        """Invoke the registered low-level resources/read handler."""
        from mcp import types

        handler = app._mcp_server.request_handlers[types.ReadResourceRequest]
        request = types.ReadResourceRequest(
            method="resources/read",
            params=types.ReadResourceRequestParams(uri=uri),
        )
        result = await handler(request)
        return result.root.contents[0]

    @pytest.mark.asyncio
    async def test_binary_field_and_attachment_round_trip(self, config_full_access):
        from mcp import types
        from mcp.server.fastmcp import FastMCP

        from mcp_server_odoo.resources import register_resources

        connection = OdooConnection(config_full_access)
        connection.connect()
        connection.authenticate()
        access_controller = AccessController(config_full_access)

        # Create inside try so a partial setup failure still cleans up
        partner_id = None
        attachment_id = None
        try:
            partner_id = connection.create(
                "res.partner",
                {
                    "name": "Binary E2E Test Partner",
                    "image_1920": base64.b64encode(self.PNG_BYTES).decode("ascii"),
                },
            )
            attachment_id = connection.create(
                "ir.attachment",
                {
                    "name": "binary-e2e.pdf",
                    "datas": base64.b64encode(self.PDF_BYTES).decode("ascii"),
                    "mimetype": "application/pdf",
                },
            )

            app = FastMCP("test-binary-e2e")
            register_resources(app, connection, access_controller, config_full_access)
            tool_handler = OdooToolHandler(
                MagicMock(), connection, access_controller, config_full_access
            )

            # Binary field: correct mimeType, blob byte-identical to the field
            content = await self._read_resource(
                app, f"odoo://res.partner/record/{partner_id}/image_1920"
            )
            assert isinstance(content, types.BlobResourceContents)
            assert content.mimeType == "image/png"
            stored = connection.read("res.partner", [partner_id], ["image_1920"])[0]["image_1920"]
            assert base64.b64decode(content.blob) == base64.b64decode(stored)

            # Attachment: correct mimeType, blob byte-identical to the upload
            content = await self._read_resource(app, f"odoo://attachment/{attachment_id}")
            assert isinstance(content, types.BlobResourceContents)
            assert content.mimeType == "application/pdf"
            assert base64.b64decode(content.blob) == self.PDF_BYTES

            # get_record never inlines base64: binary value arrives as a URI
            record_result = await tool_handler._handle_get_record_tool(
                "res.partner", partner_id, ["name", "image_1920"]
            )
            assert (
                record_result.record["image_1920"]
                == f"odoo://res.partner/record/{partner_id}/image_1920"
            )

        finally:
            if attachment_id:
                connection.unlink("ir.attachment", [attachment_id])
            if partner_id:
                connection.unlink("res.partner", [partner_id])
            connection.disconnect()


@pytest.mark.yolo
class TestYoloAggregateRecordsE2E:
    """Live YOLO mode tests for the aggregate_records tool (Odoo 17+)."""

    @pytest.fixture
    def config_read_only(self):
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            database=os.getenv("ODOO_DB"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            yolo_mode="read",
        )

    @pytest.fixture
    def handler(self, config_read_only):
        connection = OdooConnection(config_read_only)
        connection.connect()
        connection.authenticate()
        access_controller = AccessController(config_read_only)
        app = MagicMock()
        yield OdooToolHandler(app, connection, access_controller, config_read_only)
        connection.disconnect()

    @pytest.mark.asyncio
    async def test_count_partners_by_country(self, handler):
        """Default aggregate (caller passes None) → tool injects ['__count'].

        Works on every supported Odoo version: v19+ via formatted_read_group,
        v16/v17/v18 via the read_group fallback path with normalization.
        """
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
        assert result["aggregates"] == ["__count"]
        assert isinstance(result["groups"], list)
        assert result["groups"], "Expected at least one group"
        for bucket in result["groups"]:
            assert "__count" in bucket
            assert "country_id" in bucket

    @pytest.mark.asyncio
    async def test_aggregate_with_explicit_aggregate(self, handler):
        """Explicit aggregate expression appears in each bucket.

        Uses ``partner_share:count_distinct`` rather than ``id:count`` because
        v16's read_group silently elides ``id:count`` when ``__count`` is
        already implicit. Non-id aggregates work on every version.
        """
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
    async def test_empty_groupby_returns_overall_count(self, handler):
        """groupby=[] collapses to one overall row — the filtered-count path."""
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
        assert result["groups"][0]["__count"] >= 0
        assert result["has_more"] is False
