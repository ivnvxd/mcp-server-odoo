"""Integration test to verify all fixes work together."""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.tools import OdooToolHandler


class TestFixesIntegration:
    """Test that all fixes work together correctly."""

    @pytest.fixture
    def tool_handler(self):
        """Create a tool handler with mocked dependencies."""
        app = Mock()
        connection = Mock()
        access_controller = Mock()
        config = Mock()
        config.default_limit = 10
        config.max_limit = 100
        config.is_yolo_enabled = False  # Ensure standard mode for these tests
        config.yolo_mode = "off"

        return OdooToolHandler(app, connection, access_controller, config)

    @pytest.mark.asyncio
    async def test_all_fixes_work_together(self, tool_handler):
        """Test datetime formatting, list_models JSON, and smart fields all work."""
        # Setup connection mock
        tool_handler.connection.is_authenticated = True

        # Test 1: list_models returns proper JSON structure
        mock_models = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "res.company", "name": "Companies"},
        ]
        tool_handler.access_controller.get_enabled_models.return_value = mock_models

        # Mock permissions for each model
        mock_permissions = Mock()
        mock_permissions.can_read = True
        mock_permissions.can_write = True
        mock_permissions.can_create = False
        mock_permissions.can_unlink = False
        tool_handler.access_controller.get_model_permissions.return_value = mock_permissions

        models_result = await tool_handler._handle_list_models_tool()

        # Should return dict with models array
        assert isinstance(models_result, dict)
        assert "models" in models_result
        assert isinstance(models_result["models"], list)
        assert len(models_result["models"]) == 2

        # Test 2: get_record with smart defaults and datetime formatting
        tool_handler.connection.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char", "required": True},
            "email": {"type": "char", "store": True, "searchable": True},
            "create_date": {"type": "datetime", "store": True},
            "write_date": {"type": "datetime"},  # Should be excluded
            "image_1920": {"type": "binary"},  # Should be excluded
            "message_ids": {"type": "one2many"},  # Should be excluded
        }

        # Mock read to return a record with Odoo's compact datetime format
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test Partner",
                "email": "test@example.com",
                "create_date": "20250606T13:50:23",  # Compact format
            }
        ]

        record_result = await tool_handler._handle_get_record_tool("res.partner", 1, None)

        # Should have smart field selection
        assert "id" in record_result
        assert "name" in record_result
        assert "email" in record_result
        assert "create_date" in record_result

        # Should NOT have excluded fields
        assert "write_date" not in record_result
        assert "image_1920" not in record_result
        assert "message_ids" not in record_result

        # Should have metadata
        assert "_metadata" in record_result
        assert record_result["_metadata"]["field_selection_method"] == "smart_defaults"

        # Should have formatted datetime
        assert record_result["create_date"] == "2025-06-06T13:50:23+00:00"

        # Test 3: search_records with datetime formatting
        tool_handler.connection.search_count.return_value = 2
        tool_handler.connection.search.return_value = [1, 2]
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Partner 1",
                "create_date": "20250606T13:50:23",  # Compact format
                "write_date": "2025-06-07 14:30:00",  # Standard format
            },
            {
                "id": 2,
                "name": "Partner 2",
                "create_date": "20250607T10:20:30",
                "write_date": "2025-06-08 11:45:00",
            },
        ]

        search_result = await tool_handler._handle_search_tool(
            "res.partner",
            [["is_company", "=", True]],
            ["name", "create_date", "write_date"],
            10,
            0,
            None,
        )

        # Should have properly formatted datetimes
        assert search_result["records"][0]["create_date"] == "2025-06-06T13:50:23+00:00"
        assert search_result["records"][0]["write_date"] == "2025-06-07T14:30:00+00:00"
        assert search_result["records"][1]["create_date"] == "2025-06-07T10:20:30+00:00"
        assert search_result["records"][1]["write_date"] == "2025-06-08T11:45:00+00:00"

    @pytest.mark.asyncio
    async def test_get_record_with_all_fields_option(self, tool_handler):
        """Test get_record with __all__ option returns all fields without metadata."""
        tool_handler.connection.is_authenticated = True

        # Mock a large response with many fields
        large_record = {
            "id": 1,
            "name": "Test",
            "email": "test@example.com",
            "image_1920": "base64data...",
            "message_ids": [1, 2, 3],
            "write_date": "20250606T13:50:23",
            # ... imagine 100+ more fields
        }
        tool_handler.connection.read.return_value = [large_record]

        result = await tool_handler._handle_get_record_tool("res.partner", 1, ["__all__"])

        # Should return all fields
        assert "image_1920" in result
        assert "message_ids" in result
        assert "write_date" in result

        # Should NOT have metadata
        assert "_metadata" not in result

        # Should still format datetime
        assert result["write_date"] == "2025-06-06T13:50:23+00:00"

    @pytest.mark.asyncio
    async def test_get_record_with_specific_fields(self, tool_handler):
        """Test get_record with specific fields returns only those fields."""
        tool_handler.connection.is_authenticated = True

        tool_handler.connection.read.return_value = [
            {"name": "Test Partner", "vat": "US123456", "create_date": "20250606T13:50:23"}
        ]

        result = await tool_handler._handle_get_record_tool(
            "res.partner", 1, ["name", "vat", "create_date"]
        )

        # Should have only requested fields
        assert "name" in result
        assert "vat" in result
        assert "create_date" in result

        # Should NOT have metadata
        assert "_metadata" not in result

        # Should still format datetime
        assert result["create_date"] == "2025-06-06T13:50:23+00:00"
