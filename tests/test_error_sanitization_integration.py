"""Integration tests for error sanitization in tools and resources."""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.access_control import AccessControlError
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnectionError
from mcp_server_odoo.resources import OdooResourceHandler
from mcp_server_odoo.tools import OdooToolHandler


class TestErrorSanitizationIntegration:
    """Test that error messages are properly sanitized in real usage."""

    @pytest.fixture
    def tool_handler(self):
        """Create a tool handler with mocked dependencies."""
        app = Mock()
        connection = Mock()
        access_controller = Mock()
        config = Mock()
        config.default_limit = 10
        config.max_limit = 100

        return OdooToolHandler(app, connection, access_controller, config)

    @pytest.fixture
    def resource_handler(self):
        """Create a resource handler with mocked dependencies."""
        app = Mock()
        connection = Mock()
        access_controller = Mock()
        config = Mock()
        config.default_limit = 10
        config.max_limit = 100

        return OdooResourceHandler(app, connection, access_controller, config)

    @pytest.mark.asyncio
    async def test_tool_wraps_connection_error(self, tool_handler):
        """Test that the tool layer wraps OdooConnectionError into ValidationError.

        Note: actual XML-RPC fault sanitization happens in OdooConnection.execute_kw,
        not in the tool layer. This test verifies the tool's error wrapping.
        """
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.side_effect = OdooConnectionError(
            "Operation failed: Invalid field 'bogus_field' in search criteria"
        )

        with pytest.raises(ValidationError) as exc_info:
            await tool_handler._handle_search_tool(
                "res.partner", [["bogus_field", "=", True]], None, 10, 0, None
            )

        # Error message should be sanitized
        error_msg = str(exc_info.value)
        assert "Invalid field" in error_msg
        assert "bogus_field" in error_msg

    @pytest.mark.asyncio
    async def test_tool_access_error_sanitization(self, tool_handler):
        """Test that access control errors are sanitized."""
        tool_handler.access_controller.validate_model_access.side_effect = AccessControlError(
            "Model 'sale.order' is not enabled for MCP access"
        )

        with pytest.raises(ValidationError) as exc_info:
            await tool_handler._handle_get_record_tool("sale.order", 1, None)

        error_msg = str(exc_info.value)
        assert "Access denied" in error_msg
        assert "sale.order" in error_msg

    @pytest.mark.asyncio
    async def test_tool_connection_error_sanitization(self, tool_handler):
        """Test that connection errors are sanitized."""
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.read.side_effect = OdooConnectionError(
            "Operation failed: Cannot connect to Odoo server"
        )

        with pytest.raises(ValidationError) as exc_info:
            await tool_handler._handle_get_record_tool("res.partner", 1, None)

        error_msg = str(exc_info.value)
        assert "Connection error" in error_msg
        assert "Cannot connect" in error_msg

    @pytest.mark.asyncio
    async def test_tool_generic_error_sanitization(self, tool_handler):
        """Test that generic errors are sanitized."""
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search.side_effect = Exception(
            "Traceback (most recent call last):\n"
            '  File "/opt/odoo/models.py", line 123, in execute\n'
            "    raise ValueError('Test error')\n"
            "ValueError: Test error"
        )

        with pytest.raises(ValidationError) as exc_info:
            await tool_handler._handle_search_tool("res.partner", [], None, 10, 0, None)

        error_msg = str(exc_info.value)
        assert "Traceback" not in error_msg
        assert "/opt/odoo" not in error_msg
        assert "line 123" not in error_msg
        assert "Search failed" in error_msg

    @pytest.mark.asyncio
    async def test_resource_error_sanitization(self, resource_handler):
        """Test that resource errors are sanitized."""
        resource_handler.connection.is_authenticated = True
        resource_handler.connection.search.return_value = []

        from mcp_server_odoo.error_handling import NotFoundError

        with pytest.raises(NotFoundError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "999999")

        error_msg = str(exc_info.value)
        assert "Record not found" in error_msg
        assert "res.partner" in error_msg
        assert "999999" in error_msg

    @pytest.mark.asyncio
    async def test_mcp_error_response_sanitization(self, tool_handler):
        """Test that MCP error responses are sanitized."""
        from mcp_server_odoo.error_handling import ValidationError

        # Create an error with internal details
        error = ValidationError(
            "Failed to execute search_count on res.partner: Invalid field res.partner.test_field",
            details={
                "error_type": "XMLRPCFault",
                "traceback": "Full traceback here...",
                "field": "test_field",
                "model": "res.partner",
            },
        )

        mcp_error = error.to_mcp_error()

        # Check sanitized message
        assert "Failed to execute search_count" not in mcp_error.message
        assert "XMLRPCFault" not in str(mcp_error.data)
        assert "traceback" not in mcp_error.data["details"]
        assert mcp_error.data["details"]["field"] == "test_field"
        assert mcp_error.data["details"]["model"] == "res.partner"

    def test_complex_error_chain_sanitization(self):
        """Test sanitization of complex error chains."""
        from mcp_server_odoo.error_sanitizer import ErrorSanitizer

        # Simulate a complex error message from multiple layers
        complex_error = """
        Error executing tool search_records: Connection error: Failed to execute search_count on res.partner:
        Internal Server Error in MCPObjectController: Invalid field res.partner.invalid_field in leaf ('invalid_field', '=', True)
        File "/opt/odoo/addons/mcp_server/controllers/xmlrpc.py", line 123, in execute_kw
        File "/usr/lib/python3.10/xmlrpc/client.py", line 1122, in __call__
        xmlrpc.client.Fault: <Fault 1: 'Traceback (most recent call last):
          File "/opt/odoo/odoo/http.py", line 1589, in _serve_db
        odoo.exceptions.ValidationError: Invalid field res.partner.invalid_field'>
        """

        sanitized = ErrorSanitizer.sanitize_message(complex_error)

        # Should not contain any internal details
        assert "MCPObjectController" not in sanitized
        assert "/opt/odoo" not in sanitized
        assert "/usr/lib/python" not in sanitized
        assert "xmlrpc.client.Fault" not in sanitized
        assert "line 123" not in sanitized
        assert "Traceback" not in sanitized
        assert "_serve_db" not in sanitized

        # Should contain useful information
        assert "Invalid field" in sanitized
