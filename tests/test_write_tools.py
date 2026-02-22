"""Tests for write operation tools."""

from unittest.mock import Mock, call

import pytest

from mcp_server_odoo.access_control import AccessControlError
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnectionError
from mcp_server_odoo.tools import OdooToolHandler, register_tools


class TestWriteTools:
    """Test write operation tools."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        app = Mock()
        app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
        return app

    @pytest.fixture
    def mock_connection(self):
        """Create mock OdooConnection."""
        conn = Mock()
        conn.is_authenticated = True
        return conn

    @pytest.fixture
    def mock_access_controller(self):
        """Create mock AccessController."""
        controller = Mock()
        controller.validate_model_access = Mock()
        return controller

    @pytest.fixture
    def mock_config(self):
        """Create mock OdooConfig."""
        config = Mock()
        config.default_limit = 10
        config.max_limit = 100
        config.url = "http://localhost:8069"
        return config

    @pytest.fixture
    def tool_handler(self, mock_app, mock_connection, mock_access_controller, mock_config):
        """Create OdooToolHandler instance."""
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, mock_config)

    @pytest.mark.asyncio
    async def test_create_record_success(self, tool_handler, mock_connection):
        """Test successful record creation."""
        # Setup
        model = "res.partner"
        values = {"name": "Test Partner", "email": "test@example.com"}
        created_id = 123
        essential_record = {
            "id": created_id,
            "name": "Test Partner",
            "display_name": "Test Partner",
        }

        mock_connection.create.return_value = created_id
        mock_connection.read.return_value = [essential_record]

        # Execute
        result = await tool_handler._handle_create_record_tool(model, values)

        # Verify
        assert result["success"] is True
        assert result["record"] == essential_record
        assert (
            result["url"]
            == f"http://localhost:8069/web#id={created_id}&model={model}&view_type=form"
        )
        assert "Successfully created" in result["message"]
        mock_connection.create.assert_called_once_with(model, values)
        mock_connection.read.assert_called_once_with(
            model, [created_id], ["id", "name", "display_name"]
        )

    @pytest.mark.asyncio
    async def test_create_record_no_values(self, tool_handler):
        """Test create record with no values."""
        with pytest.raises(ValidationError, match="No values provided"):
            await tool_handler._handle_create_record_tool("res.partner", {})

    @pytest.mark.asyncio
    async def test_create_record_access_denied(self, tool_handler, mock_access_controller):
        """Test create record with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        with pytest.raises(ValidationError, match="Access denied"):
            await tool_handler._handle_create_record_tool("res.partner", {"name": "Test"})

    @pytest.mark.asyncio
    async def test_update_record_success(self, tool_handler, mock_connection):
        """Test successful record update."""
        # Setup
        model = "res.partner"
        record_id = 123
        values = {"email": "updated@example.com"}
        # First read call (existence check) returns just ID
        existing_record = {"id": record_id}
        # Second read call returns essential fields
        updated_record = {"id": record_id, "name": "Test Partner", "display_name": "Test Partner"}

        mock_connection.read.side_effect = [[existing_record], [updated_record]]
        mock_connection.write.return_value = True

        # Execute
        result = await tool_handler._handle_update_record_tool(model, record_id, values)

        # Verify
        assert result["success"] is True
        assert result["record"] == updated_record
        assert (
            result["url"]
            == f"http://localhost:8069/web#id={record_id}&model={model}&view_type=form"
        )
        assert "Successfully updated" in result["message"]
        mock_connection.write.assert_called_once_with(model, [record_id], values)
        # Verify both read calls with correct parameters
        expected_calls = [
            call(model, [record_id], ["id"]),  # Existence check
            call(model, [record_id], ["id", "name", "display_name"]),  # Essential fields
        ]
        mock_connection.read.assert_has_calls(expected_calls)

    @pytest.mark.asyncio
    async def test_update_record_not_found(self, tool_handler, mock_connection):
        """Test update record that doesn't exist."""
        mock_connection.read.return_value = []

        with pytest.raises(ValidationError, match="Record not found"):
            await tool_handler._handle_update_record_tool("res.partner", 999, {"name": "Test"})

    @pytest.mark.asyncio
    async def test_update_record_no_values(self, tool_handler):
        """Test update record with no values."""
        with pytest.raises(ValidationError, match="No values provided"):
            await tool_handler._handle_update_record_tool("res.partner", 123, {})

    @pytest.mark.asyncio
    async def test_delete_record_success(self, tool_handler, mock_connection):
        """Test successful record deletion."""
        # Setup
        model = "res.partner"
        record_id = 123
        existing_record = {"id": record_id, "name": "Test Partner"}

        mock_connection.read.return_value = [existing_record]
        mock_connection.unlink.return_value = True

        # Execute
        result = await tool_handler._handle_delete_record_tool(model, record_id)

        # Verify
        assert result["success"] is True
        assert result["deleted_id"] == record_id
        assert result["deleted_name"] == "Test Partner"
        assert "Successfully deleted" in result["message"]
        mock_connection.unlink.assert_called_once_with(model, [record_id])

    @pytest.mark.asyncio
    async def test_delete_record_not_found(self, tool_handler, mock_connection):
        """Test delete record that doesn't exist."""
        mock_connection.read.return_value = []

        with pytest.raises(ValidationError, match="Record not found"):
            await tool_handler._handle_delete_record_tool("res.partner", 999)

    @pytest.mark.asyncio
    async def test_delete_record_access_denied(self, tool_handler, mock_access_controller):
        """Test delete record with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        with pytest.raises(ValidationError, match="Access denied"):
            await tool_handler._handle_delete_record_tool("res.partner", 123)

    @pytest.mark.asyncio
    async def test_create_record_not_authenticated(self, tool_handler, mock_connection):
        """Test create record when not authenticated."""
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError, match="Not authenticated"):
            await tool_handler._handle_create_record_tool("res.partner", {"name": "Test"})

    @pytest.mark.asyncio
    async def test_update_record_connection_error(self, tool_handler, mock_connection):
        """Test update record with connection error."""
        mock_connection.read.side_effect = OdooConnectionError("Connection failed")

        with pytest.raises(ValidationError, match="Connection error"):
            await tool_handler._handle_update_record_tool("res.partner", 123, {"name": "Test"})

    def test_tools_registered(self, mock_app, mock_connection, mock_access_controller, mock_config):
        """Test that write tools are registered."""
        # Track functions that were decorated
        decorated_functions = []

        def mock_tool_decorator(**kwargs):
            def decorator(func):
                decorated_functions.append(func.__name__)
                return func

            return decorator

        mock_app.tool = mock_tool_decorator

        register_tools(mock_app, mock_connection, mock_access_controller, mock_config)

        # Check that tool decorator was called for write operations
        assert "create_record" in decorated_functions
        assert "update_record" in decorated_functions
        assert "delete_record" in decorated_functions


class TestWriteToolsIntegration:
    """Integration tests for write tools with real connection."""

    @pytest.fixture
    def real_config(self):
        """Load real configuration."""
        from mcp_server_odoo.config import load_config

        return load_config()

    @pytest.fixture
    def real_connection(self, real_config):
        """Create real connection."""
        from mcp_server_odoo.odoo_connection import OdooConnection

        conn = OdooConnection(real_config)
        conn.connect()
        conn.authenticate()
        yield conn
        conn.disconnect()

    @pytest.fixture
    def real_access_controller(self, real_config):
        """Create real access controller."""
        from mcp_server_odoo.access_control import AccessController

        return AccessController(real_config)

    @pytest.fixture
    def real_app(self):
        """Create real FastMCP app."""
        from mcp.server.fastmcp import FastMCP

        return FastMCP("test-app")

    @pytest.fixture
    def real_tool_handler(self, real_app, real_connection, real_access_controller, real_config):
        """Create real tool handler."""
        return register_tools(real_app, real_connection, real_access_controller, real_config)

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_update_delete_cycle(self, real_config, real_tool_handler):
        """Test full create, update, delete cycle with real Odoo."""
        if real_config.yolo_mode != "true":
            pytest.skip("Write integration test requires ODOO_YOLO=true")
        handler = real_tool_handler

        # Create a test partner
        create_values = {
            "name": "MCP Test Partner",
            "email": "mcp.test@example.com",
            "is_company": False,
        }

        # Create
        create_result = await handler._handle_create_record_tool("res.partner", create_values)
        assert create_result["success"] is True
        record_id = create_result["record"]["id"]
        assert create_result["record"]["name"] == "MCP Test Partner"

        try:
            # Update
            update_values = {
                "email": "mcp.updated@example.com",
                "phone": "+1234567890",
            }
            update_result = await handler._handle_update_record_tool(
                "res.partner", record_id, update_values
            )
            assert update_result["success"] is True

            # Verify updated values via get_record (update result only has essential fields)
            get_result = await handler._handle_get_record_tool(
                "res.partner", record_id, fields=["email", "phone"]
            )
            assert get_result.record["email"] == "mcp.updated@example.com"
            assert get_result.record["phone"] == "+1234567890"

            # Delete
            delete_result = await handler._handle_delete_record_tool("res.partner", record_id)
            assert delete_result["success"] is True
            assert delete_result["deleted_id"] == record_id

            # Verify deletion
            from mcp_server_odoo.tools import ValidationError

            with pytest.raises(ValidationError, match="Record not found"):
                await handler._handle_get_record_tool("res.partner", record_id, fields=None)

        except Exception:
            # Clean up if test fails
            try:
                handler.connection.unlink("res.partner", [record_id])
            except Exception:
                pass
            raise
