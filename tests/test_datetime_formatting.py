"""Test datetime formatting in tools."""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.tools import OdooToolHandler


class TestDateTimeFormatting:
    """Test datetime formatting functionality."""

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

    def test_format_datetime_compact_format(self, tool_handler):
        """Test formatting of Odoo compact datetime format."""
        # Test compact format (YYYYMMDDTHH:MM:SS)
        result = tool_handler._format_datetime("20250606T13:50:23")
        assert result == "2025-06-06T13:50:23+00:00"

    def test_format_datetime_standard_format(self, tool_handler):
        """Test formatting of standard Odoo datetime format."""
        # Test standard format (YYYY-MM-DD HH:MM:SS)
        result = tool_handler._format_datetime("2025-06-06 13:50:23")
        assert result == "2025-06-06T13:50:23+00:00"

    def test_format_datetime_already_formatted(self, tool_handler):
        """Test that already formatted datetime is not changed."""
        # Test already formatted
        result = tool_handler._format_datetime("2025-06-06T13:50:23+00:00")
        assert result == "2025-06-06T13:50:23+00:00"

    def test_format_datetime_invalid_format(self, tool_handler):
        """Test that invalid datetime formats are returned unchanged."""
        # Test invalid format
        result = tool_handler._format_datetime("invalid-date")
        assert result == "invalid-date"

    def test_format_datetime_empty_value(self, tool_handler):
        """Test that empty values are handled properly."""
        assert tool_handler._format_datetime("") == ""
        assert tool_handler._format_datetime(None) is None
        assert tool_handler._format_datetime(False) is False

    def test_process_record_dates_with_metadata(self, tool_handler):
        """Test processing dates in a record with field metadata."""
        # Mock fields_get to return field metadata
        tool_handler.connection.fields_get.return_value = {
            "create_date": {"type": "datetime"},
            "write_date": {"type": "datetime"},
            "date_field": {"type": "date"},
            "name": {"type": "char"},
        }

        record = {
            "id": 1,
            "name": "Test Record",
            "create_date": "20250606T13:50:23",
            "write_date": "2025-06-06 14:30:00",
            "date_field": "2025-06-06",
        }

        result = tool_handler._process_record_dates(record, "res.partner")

        assert result["create_date"] == "2025-06-06T13:50:23+00:00"
        assert result["write_date"] == "2025-06-06T14:30:00+00:00"
        assert result["date_field"] == "2025-06-06"  # Date fields unchanged
        assert result["name"] == "Test Record"  # Non-date fields unchanged

    def test_process_record_dates_without_metadata(self, tool_handler):
        """Test processing dates in a record without field metadata (fallback)."""
        # Mock fields_get to raise an exception
        tool_handler.connection.fields_get.side_effect = Exception("Cannot get fields")

        record = {
            "id": 1,
            "name": "Test Record",
            "some_datetime": "20250606T13:50:23",
            "another_datetime": "2025-06-06 14:30:00",
            "not_a_date": "some text",
        }

        result = tool_handler._process_record_dates(record, "res.partner")

        # Should detect datetime patterns and format them
        assert result["some_datetime"] == "2025-06-06T13:50:23+00:00"
        assert result["another_datetime"] == "2025-06-06T14:30:00+00:00"
        assert result["not_a_date"] == "some text"

    @pytest.mark.asyncio
    async def test_search_records_formats_dates(self, tool_handler):
        """Test that search_records formats datetime fields."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test Partner",
                "create_date": "20250606T13:50:23",
            }
        ]
        tool_handler.connection.fields_get.return_value = {
            "create_date": {"type": "datetime"},
            "name": {"type": "char"},
        }

        # Register the tool handler
        handler = tool_handler._handle_search_tool

        result = await handler("res.partner", [], None, 10, 0, None)

        assert result["records"][0]["create_date"] == "2025-06-06T13:50:23+00:00"

    @pytest.mark.asyncio
    async def test_get_record_formats_dates(self, tool_handler):
        """Test that get_record formats datetime fields."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test Partner",
                "create_date": "20250606T13:50:23",
                "write_date": "2025-06-06 14:30:00",
            }
        ]
        tool_handler.connection.fields_get.return_value = {
            "create_date": {"type": "datetime"},
            "write_date": {"type": "datetime"},
            "name": {"type": "char"},
        }

        # Call the handler
        handler = tool_handler._handle_get_record_tool

        result = await handler("res.partner", 1, None)

        assert result.record["create_date"] == "2025-06-06T13:50:23+00:00"
        assert result.record["write_date"] == "2025-06-06T14:30:00+00:00"
