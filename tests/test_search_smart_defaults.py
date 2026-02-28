"""Test smart field selection for search_records."""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.tools import OdooToolHandler


class TestSearchSmartDefaults:
    """Test smart field selection for search_records when fields not specified."""

    @pytest.fixture
    def tool_handler(self):
        """Create a tool handler with mocked dependencies."""
        app = Mock()
        connection = Mock()
        access_controller = Mock()
        config = Mock()
        config.default_limit = 10
        config.max_limit = 100
        config.max_smart_fields = 15

        return OdooToolHandler(app, connection, access_controller, config)

    @pytest.mark.asyncio
    async def test_search_with_no_fields_uses_smart_defaults(self, tool_handler):
        """Test that search_records uses smart defaults when fields is None."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 2
        tool_handler.connection.search.return_value = [1, 2]

        # Mock fields_get to return field metadata
        tool_handler.connection.fields_get.return_value = {
            "id": {"type": "integer", "required": True},
            "name": {"type": "char", "required": True, "searchable": True},
            "email": {"type": "char", "searchable": True},
            "phone": {"type": "char", "searchable": True},
            "create_date": {"type": "datetime"},
            "message_ids": {"type": "one2many"},  # Should be excluded
            "_barcode_scan": {"type": "char"},  # Should be excluded (technical)
            "image_1920": {"type": "binary"},  # Should be excluded (binary)
        }

        # Mock read to return records with only smart default fields
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test 1",
                "email": "test1@example.com",
                "create_date": "20250607T10:00:00",
            },
            {
                "id": 2,
                "name": "Test 2",
                "email": "test2@example.com",
                "create_date": "20250607T11:00:00",
            },
        ]

        # Call the handler with fields=None
        handler = tool_handler._handle_search_tool
        await handler("res.partner", [], None, 10, 0, None)

        # Verify smart defaults were used
        # The read call should have been made with specific fields, not None
        tool_handler.connection.read.assert_called_once()
        call_args = tool_handler.connection.read.call_args
        fields_arg = call_args[0][2]  # Third positional argument

        # Should have selected smart default fields
        assert fields_arg is not None
        assert isinstance(fields_arg, list)
        assert "id" in fields_arg
        assert "name" in fields_arg
        assert "email" in fields_arg

        # Should exclude technical/binary/relation fields
        assert "message_ids" not in fields_arg
        assert "_barcode_scan" not in fields_arg
        assert "image_1920" not in fields_arg

    @pytest.mark.asyncio
    async def test_search_with_specific_fields(self, tool_handler):
        """Test that search_records uses specified fields when provided."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]
        tool_handler.connection.read.return_value = [
            {"id": 1, "name": "Test", "phone": "+1234567890"}
        ]

        # Call with specific fields
        handler = tool_handler._handle_search_tool
        fields = ["name", "phone"]
        await handler("res.partner", [], fields, 10, 0, None)

        # Verify specified fields were used
        tool_handler.connection.read.assert_called_once_with("res.partner", [1], fields)

    @pytest.mark.asyncio
    async def test_search_with_all_fields(self, tool_handler):
        """Test that search_records can fetch all fields when explicitly requested."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test",
                "email": "test@example.com",
                "phone": "+1234567890",
                "create_date": "20250607T10:00:00",
                "message_ids": [1, 2, 3],
                "_barcode_scan": "12345",
                "image_1920": "base64data...",
                # ... many more fields
            }
        ]

        # Call with __all__ to get all fields
        handler = tool_handler._handle_search_tool
        await handler("res.partner", [], ["__all__"], 10, 0, None)

        # Verify None was passed to read (which means all fields)
        tool_handler.connection.read.assert_called_once_with("res.partner", [1], None)

    @pytest.mark.asyncio
    async def test_search_smart_defaults_with_datetime_formatting(self, tool_handler):
        """Test that datetime fields are formatted even with smart defaults."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]

        # Mock fields_get — use date_order (a business datetime field that smart
        # selection includes) instead of create_date (which is in the exclude list
        # and would get score 0, making the test internally inconsistent).
        tool_handler.connection.fields_get.return_value = {
            "id": {"type": "integer", "required": True},
            "name": {"type": "char", "required": True},
            "date_order": {"type": "datetime", "store": True},
        }

        # Mock read with datetime that needs formatting
        tool_handler.connection.read.return_value = [
            {"id": 1, "name": "Test", "date_order": "20250607T10:00:00"}
        ]

        # Call the handler with fields=None to trigger smart selection
        handler = tool_handler._handle_search_tool
        result = await handler("res.partner", [], None, 10, 0, None)

        # Verify date_order was included by smart selection
        call_args = tool_handler.connection.read.call_args
        fields_arg = call_args[0][2]
        assert "date_order" in fields_arg

        # Verify datetime was formatted
        assert result["records"][0]["date_order"] == "2025-06-07T10:00:00+00:00"
