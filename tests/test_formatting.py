"""Tests for data formatting functions."""

import unittest
from unittest.mock import MagicMock

from mcp_server_odoo.data_formatting import (
    format_field_value,
    format_record,
)


class TestDataFormatting(unittest.TestCase):
    """Test cases for data formatting functions."""

    def setUp(self):
        """Set up test environment."""
        # Create a mock OdooConnection
        self.odoo = MagicMock()

        # Mock get_model_fields to return field info
        self.odoo.get_model_fields.return_value = {
            "name": {"type": "char", "string": "Name"},
            "age": {"type": "integer", "string": "Age"},
            "is_active": {"type": "boolean", "string": "Active"},
            "partner_id": {
                "type": "many2one",
                "string": "Partner",
                "relation": "res.partner",
            },
            "tag_ids": {
                "type": "many2many",
                "string": "Tags",
                "relation": "res.partner.category",
            },
            "note": {"type": "text", "string": "Notes"},
        }

    def test_format_field_value_char(self):
        """Test formatting char field."""
        result = format_field_value(
            field_name="Name",
            field_value="Test Name",
            field_type="char",
            model="res.partner",
            odoo=self.odoo,
        )
        self.assertEqual(result, "Name: Test Name")

    def test_format_field_value_integer(self):
        """Test formatting integer field."""
        result = format_field_value(
            field_name="Age",
            field_value=42,
            field_type="integer",
            model="res.partner",
            odoo=self.odoo,
        )
        self.assertEqual(result, "Age: 42")

    def test_format_field_value_boolean(self):
        """Test formatting boolean field."""
        result = format_field_value(
            field_name="Active",
            field_value=True,
            field_type="boolean",
            model="res.partner",
            odoo=self.odoo,
        )
        self.assertEqual(result, "Active: Yes")

        result = format_field_value(
            field_name="Active",
            field_value=False,
            field_type="boolean",
            model="res.partner",
            odoo=self.odoo,
        )
        self.assertEqual(result, "Active: No")

    def test_format_field_value_many2one(self):
        """Test formatting many2one field."""
        result = format_field_value(
            field_name="Partner",
            field_value=[1, "Test Partner"],
            field_type="many2one",
            model="res.partner",
            odoo=self.odoo,
        )
        self.assertEqual(result, "Partner: Test Partner [odoo://Partner/record/1]")

    def test_format_record(self):
        """Test formatting a complete record."""
        record = {
            "id": 1,
            "name": "Test Record",
            "age": 30,
            "is_active": True,
            "partner_id": [2, "Test Partner"],
            "tag_ids": [1, 2, 3],
        }

        result = format_record(
            model="test.model",
            record=record,
            odoo=self.odoo,
        )

        # Check that the result contains expected content
        self.assertIn("Resource: test.model/record/1", result)
        self.assertIn("Name: Test Record", result)
        self.assertIn("Age: 30", result)
        self.assertIn("Active: Yes", result)
        self.assertIn("Partner: Test Partner", result)
        self.assertIn("Tags: 3 related records", result)


if __name__ == "__main__":
    unittest.main()
