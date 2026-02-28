"""Test smart field selection functionality."""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.tools import OdooToolHandler


class TestSmartFieldSelection:
    """Test smart field selection logic."""

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

    def test_score_field_importance_essential_fields(self, tool_handler):
        """Test that essential fields get highest scores."""
        # Essential fields should get 1000+ points
        assert tool_handler._score_field_importance("id", {}) >= 1000
        assert tool_handler._score_field_importance("name", {}) >= 1000
        assert tool_handler._score_field_importance("display_name", {}) >= 1000
        assert tool_handler._score_field_importance("active", {}) >= 1000

    def test_score_field_importance_technical_fields(self, tool_handler):
        """Test that technical fields get low scores."""
        # Fields with technical prefixes should get 0 points
        assert tool_handler._score_field_importance("_order", {}) == 0
        assert tool_handler._score_field_importance("message_follower_ids", {}) == 0
        assert tool_handler._score_field_importance("activity_ids", {}) == 0
        assert tool_handler._score_field_importance("website_message_ids", {}) == 0

        # Specific technical fields should get 0 points
        assert tool_handler._score_field_importance("write_date", {}) == 0
        assert tool_handler._score_field_importance("create_date", {}) == 0
        assert tool_handler._score_field_importance("__last_update", {}) == 0

    def test_score_field_importance_binary_fields(self, tool_handler):
        """Test that binary and large fields get low scores."""
        assert tool_handler._score_field_importance("image", {"type": "binary"}) == 0
        assert tool_handler._score_field_importance("photo", {"type": "image"}) == 0
        assert tool_handler._score_field_importance("description_html", {"type": "html"}) == 0

    def test_score_field_importance_computed_fields(self, tool_handler):
        """Test scoring of computed fields."""
        # Computed non-stored field should be capped at 30 points
        field_info = {"type": "char", "compute": "some_method", "store": False}
        score = tool_handler._score_field_importance("computed_field", field_info)
        assert score == 30  # Capped by line 313 in scoring logic

        # Computed stored field should get full score
        field_info = {"type": "char", "compute": "some_method", "store": True, "searchable": True}
        score = tool_handler._score_field_importance("computed_field", field_info)
        assert score > 30  # Should get base type score + storage + searchability bonuses

    def test_score_field_importance_relation_fields(self, tool_handler):
        """Test scoring of relation fields."""
        # One2many and many2many should get 0 points
        assert tool_handler._score_field_importance("child_ids", {"type": "one2many"}) == 0
        assert tool_handler._score_field_importance("tag_ids", {"type": "many2many"}) == 0

        # Many2one should get reasonable score
        score = tool_handler._score_field_importance(
            "partner_id", {"type": "many2one", "store": True, "searchable": True}
        )
        assert score > 0  # Should get base type score + bonuses

    def test_score_field_importance_required_fields(self, tool_handler):
        """Test that required fields get high scores."""
        field_info = {"type": "char", "required": True}
        score = tool_handler._score_field_importance("required_field", field_info)
        assert score >= 500  # Should get required field bonus (500 points)

    def test_score_field_importance_simple_stored_fields(self, tool_handler):
        """Test scoring of simple stored searchable fields."""
        simple_types = [
            "char",
            "text",
            "boolean",
            "integer",
            "float",
            "date",
            "datetime",
            "selection",
            "many2one",
        ]

        for field_type in simple_types:
            field_info = {"type": field_type, "store": True, "searchable": True}
            score = tool_handler._score_field_importance(f"test_{field_type}", field_info)
            assert score > 0  # Should get positive score

        # Non-searchable stored field should get lower score
        field_info = {"type": "char", "store": True, "searchable": False}
        score = tool_handler._score_field_importance("non_searchable", field_info)
        searchable_score = tool_handler._score_field_importance(
            "searchable", {"type": "char", "store": True, "searchable": True}
        )
        assert score < searchable_score  # Should be lower than searchable equivalent

    def test_get_smart_default_fields_success(self, tool_handler):
        """Test successful smart field selection."""
        # Mock fields_get response
        mock_fields = {
            "id": {"type": "integer"},
            "name": {"type": "char", "required": True},
            "display_name": {"type": "char"},
            "email": {"type": "char", "store": True, "searchable": True},
            "phone": {"type": "char", "store": True, "searchable": True},
            "is_company": {"type": "boolean", "store": True, "searchable": True},
            # Fields that should be excluded
            "message_ids": {"type": "one2many"},
            "_order": {"type": "char"},
            "image_1920": {"type": "binary"},
            "write_date": {"type": "datetime"},
            "access_token": {"type": "char"},
        }

        tool_handler.connection.fields_get.return_value = mock_fields

        result = tool_handler._get_smart_default_fields("res.partner")

        # Should include smart selection
        assert "id" in result
        assert "name" in result
        assert "display_name" in result
        assert "email" in result
        assert "phone" in result
        assert "is_company" in result

        # Should exclude technical/binary/relation fields
        assert "message_ids" not in result
        assert "_order" not in result
        assert "image_1920" not in result
        assert "write_date" not in result
        assert "access_token" not in result

    def test_get_smart_default_fields_error_handling(self, tool_handler):
        """Test error handling in smart field selection."""
        # Mock fields_get to raise an exception
        tool_handler.connection.fields_get.side_effect = Exception("Connection error")

        # Should return None to indicate fallback to all fields
        result = tool_handler._get_smart_default_fields("res.partner")
        assert result is None

    def test_get_smart_default_fields_empty_result(self, tool_handler):
        """Test handling of models with some essential fields but mostly excluded fields."""
        # Mock fields_get with essential fields + zero-score fields
        mock_fields = {
            "id": {"type": "integer"},
            "name": {"type": "char", "required": True},
            "display_name": {"type": "char"},
            "_order": {"type": "char"},
            "message_ids": {"type": "one2many"},
            "activity_ids": {"type": "one2many"},
        }

        tool_handler.connection.fields_get.return_value = mock_fields

        result = tool_handler._get_smart_default_fields("weird.model")

        # Should return essential fields only (since others score 0)
        # Expected order by score: name (1000+500+200+80+40=1820), display_name (1000+200+80+40=1320), id (1000+160+80+40=1280)
        assert set(result) == {"id", "name", "display_name"}
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_get_record_with_smart_defaults(self, tool_handler):
        """Test get_record using smart defaults."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.fields_get.return_value = {
            "id": {"type": "integer"},
            "name": {"type": "char", "required": True},
            "email": {"type": "char", "store": True, "searchable": True},
            "active": {"type": "boolean"},
            "display_name": {"type": "char"},
        }

        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test Partner",
                "email": "test@example.com",
                "active": True,
                "display_name": "Test Partner",
            }
        ]

        # Call without fields parameter
        result = await tool_handler._handle_get_record_tool("res.partner", 1, None)

        # Should have metadata
        assert result.metadata is not None
        assert result.metadata.field_selection_method == "smart_defaults"
        assert result.metadata.fields_returned == 5  # all 5 fields (less than limit)
        assert result.metadata.total_fields_available == 5
        assert "Limited fields returned" in result.metadata.note

        # Should have called read with smart fields
        tool_handler.connection.read.assert_called_once()
        call_args = tool_handler.connection.read.call_args
        assert call_args[0][0] == "res.partner"  # model
        assert call_args[0][1] == [1]  # record_id
        # With 5 fields, all should be returned (less than max_smart_fields=15)
        expected_fields = {"id", "name", "email", "active", "display_name"}
        assert set(call_args[0][2]) == expected_fields

    @pytest.mark.asyncio
    async def test_get_record_with_all_fields(self, tool_handler):
        """Test get_record with __all__ parameter."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test Partner",
                "email": "test@example.com",
                "phone": "123456",
                "message_ids": [1, 2, 3],
                "image_1920": "base64data...",
                # ... many more fields
            }
        ]

        # Call with __all__
        result = await tool_handler._handle_get_record_tool("res.partner", 1, ["__all__"])

        # Should not have metadata
        assert result.metadata is None

        # Should have called read with None (all fields)
        tool_handler.connection.read.assert_called_once_with("res.partner", [1], None)

    @pytest.mark.asyncio
    async def test_get_record_with_specific_fields(self, tool_handler):
        """Test get_record with specific fields."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.read.return_value = [
            {"id": 1, "name": "Test Partner", "phone": "123456"}
        ]

        # Call with specific fields
        fields = ["name", "phone"]
        result = await tool_handler._handle_get_record_tool("res.partner", 1, fields)

        # Should not have metadata
        assert result.metadata is None

        # Should have called read with specific fields
        tool_handler.connection.read.assert_called_once_with("res.partner", [1], fields)

    def test_field_selection(self, tool_handler):
        """Test that expected fields are selected by smart defaults."""
        # Mock fields_get response
        mock_fields = {
            "zip": {"type": "char", "store": True, "searchable": True},
            "email": {"type": "char", "store": True, "searchable": True},
            "active": {"type": "boolean"},
            "name": {"type": "char", "required": True},
            "display_name": {"type": "char"},
            "id": {"type": "integer"},
            "city": {"type": "char", "store": True, "searchable": True},
        }

        tool_handler.connection.fields_get.return_value = mock_fields

        result = tool_handler._get_smart_default_fields("res.partner")

        # All fields should be returned since we have only 7 fields (less than limit of 15)
        assert len(result) == 7

        # Verify that essential fields are included
        essential_fields = ["id", "name", "display_name", "active"]
        for field in essential_fields:
            assert field in result

        # Verify that business fields are included
        assert "email" in result  # Has business pattern bonus
        assert "city" in result
        assert "zip" in result

        # The exact order depends on the scoring algorithm and essential field processing
        # Just verify the expected fields are present in correct quantity
        assert set(result) == {"active", "name", "display_name", "id", "email", "city", "zip"}
