"""End-to-end tests for YOLO mode functionality.

This module tests complete YOLO mode workflows with real Odoo instances.
Tests are marked with @pytest.mark.e2e and require a running Odoo instance.
"""

import os
import socket
import time
from unittest.mock import MagicMock

import pytest

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.tools import OdooToolHandler


def is_odoo_server_running(host="localhost", port=8069):
    """Check if Odoo server is running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        result = sock.connect_ex((host, port))
        return result == 0
    except Exception:
        return False
    finally:
        sock.close()


@pytest.mark.skipif(
    not is_odoo_server_running(), reason="Odoo server not running at localhost:8069"
)
@pytest.mark.e2e
class TestYoloModeE2E:
    """End-to-end tests for YOLO mode with real Odoo."""

    @pytest.fixture
    def config_read_only(self):
        """Create configuration for read-only YOLO mode."""
        return OdooConfig(
            url="http://localhost:8069",
            database=os.getenv("ODOO_DB", "mcp-18"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            yolo_mode="read",
        )

    @pytest.fixture
    def config_full_access(self):
        """Create configuration for full access YOLO mode."""
        return OdooConfig(
            url="http://localhost:8069",
            database=os.getenv("ODOO_DB", "mcp-18"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            yolo_mode="true",
        )

    @pytest.fixture
    def config_standard(self):
        """Create configuration for standard mode."""
        return OdooConfig(
            url="http://localhost:8069",
            database=os.getenv("ODOO_DB", "mcp-18"),
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
        assert "total" in search_result
        assert search_result["total"] >= 0

        # 4. Get a specific record - should work
        if search_result["records"]:
            first_record = search_result["records"][0]
            get_result = await handler._handle_get_record_tool(
                model="res.partner",
                record_id=first_record["id"],
                fields=None,
            )
            assert "id" in get_result
            assert get_result["id"] == first_record["id"]

        # 5. Attempt to create record - should fail
        with pytest.raises(Exception) as exc_info:
            await handler._handle_create_record_tool(
                model="res.partner",
                values={"name": "YOLO Test Partner - Should Fail"},
            )
        assert "not allowed in read-only" in str(exc_info.value).lower()

        # 6. Attempt to update record - should fail
        if search_result["records"]:
            with pytest.raises(Exception) as exc_info:
                await handler._handle_update_record_tool(
                    model="res.partner",
                    record_id=first_record["id"],
                    values={"email": "test@fail.com"},
                )
            assert "not allowed in read-only" in str(exc_info.value).lower()

        # 7. Attempt to delete record - should fail
        if search_result["records"]:
            with pytest.raises(Exception) as exc_info:
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

        # The result has a different structure
        assert create_result["success"] is True
        assert "record" in create_result
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

        assert get_result["email"] == "updated.yolo@test.com"
        assert get_result["phone"] == "+1234567890"

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
        standard_models = ["res.partner", "res.users", "res.company"]
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

        # Test accounting models if available
        try:
            account_result = await handler._handle_search_tool(
                model="account.account",
                domain=[],
                fields=["id", "name"],
                limit=1,
                offset=0,
                order=None,
            )
            assert "records" in account_result
        except Exception:
            # Accounting module might not be installed
            pass

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
        with pytest.raises(Exception) as exc_info:
            await handler._handle_search_tool(
                model="invalid.model.name",
                domain=[],
                fields=["id"],
                limit=1,
                offset=0,
                order=None,
            )
        # Should get Odoo error, not MCP error
        assert (
            "invalid.model.name" in str(exc_info.value)
            or "not found" in str(exc_info.value).lower()
        )

        # Test invalid field name
        with pytest.raises(Exception) as exc_info:
            await handler._handle_search_tool(
                model="res.partner",
                domain=[],
                fields=["id", "invalid_field_xyz"],
                limit=1,
                offset=0,
                order=None,
            )
        assert "invalid_field_xyz" in str(exc_info.value) or "field" in str(exc_info.value).lower()

        # Test invalid record ID
        with pytest.raises(Exception) as exc_info:
            await handler._handle_get_record_tool(
                model="res.partner",
                record_id=999999999,  # Very unlikely to exist
                fields=["id", "name"],
            )
        # Should indicate record not found

        # Test creating record with missing required fields
        with pytest.raises(Exception) as exc_info:
            await handler._handle_create_record_tool(
                model="res.users",  # Requires login field
                values={"name": "Test User Without Login"},
            )
        # Should get validation error from Odoo

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
    async def test_explicit_opt_in_requirement(self):
        """Test that only 'true' enables full access, not other truthy values."""
        # Test valid YOLO modes
        valid_cases = [
            ("true", True),  # Should enable full access
            ("read", False),  # Should be read-only
            ("off", False),  # Should be disabled (standard mode)
        ]

        for value, should_allow_write in valid_cases:
            config = OdooConfig(
                url="http://localhost:8069",
                database=os.getenv("ODOO_DB", "mcp-18"),
                username=os.getenv("ODOO_USER", "admin"),
                password=os.getenv("ODOO_PASSWORD", "admin"),
                yolo_mode=value,
            )

            if value in ["true", "read"]:
                # These enable YOLO mode
                assert config.is_yolo_enabled

                if value == "true":
                    assert config.yolo_mode == "true"
                    # Check permissions allow write
                    access_controller = AccessController(config)
                    allowed, _ = access_controller.check_operation_allowed("res.partner", "write")
                    assert (
                        allowed == should_allow_write
                    ), f"Value '{value}' should {'allow' if should_allow_write else 'not allow'} write"
                else:
                    assert config.yolo_mode == "read"
                    # Check permissions block write
                    access_controller = AccessController(config)
                    allowed, _ = access_controller.check_operation_allowed("res.partner", "write")
                    assert not allowed, "Read mode should not allow write"
            else:
                # "off" mode
                assert not config.is_yolo_enabled
                assert config.yolo_mode == "off"

        # Test invalid YOLO mode values - should raise ValueError
        invalid_cases = [
            "True",  # Wrong case
            "1",  # Not accepted
            "yes",  # Not accepted
            "on",  # Not accepted
            "false",  # Not accepted
            "full",  # Not accepted
            "",  # Empty string
        ]

        for value in invalid_cases:
            with pytest.raises(ValueError, match="Invalid YOLO mode"):
                OdooConfig(
                    url="http://localhost:8069",
                    database=os.getenv("ODOO_DB", "mcp-18"),
                    username=os.getenv("ODOO_USER", "admin"),
                    password=os.getenv("ODOO_PASSWORD", "admin"),
                    yolo_mode=value,
                )

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
