"""Tests for field formatters registry and implementations."""

import datetime
import unittest
from unittest.mock import MagicMock

from mcp_server_odoo.field_formatters import (
    FieldFormatterRegistry,
    format_binary_field,
    format_boolean_field,
    format_char_field,
    format_date_field,
    format_datetime_field,
    format_default_field,
    format_float_field,
    format_integer_field,
    format_many2many_field,
    format_many2one_field,
    format_one2many_field,
    format_text_field,
    register_default_formatters,
)


class TestFieldFormatterRegistry(unittest.TestCase):
    """Test cases for the field formatter registry."""

    def setUp(self):
        """Set up test environment."""
        self.registry = FieldFormatterRegistry()

    def test_register_formatter(self):
        """Test registering a formatter."""

        def dummy_formatter(name, value, **kwargs):
            return f"Dummy: {name}={value}"

        self.registry.register("test_type", dummy_formatter)
        self.assertIn("test_type", self.registry.formatters)
        self.assertEqual(self.registry.formatters["test_type"], dummy_formatter)

    def test_get_formatter(self):
        """Test getting a formatter."""

        def dummy_formatter(name, value, **kwargs):
            return f"Dummy: {name}={value}"

        self.registry.register("test_type", dummy_formatter)
        formatter = self.registry.get_formatter("test_type")
        self.assertEqual(formatter, dummy_formatter)

    def test_get_formatter_unknown_type(self):
        """Test getting a formatter for an unknown type."""

        def default_formatter(name, value, **kwargs):
            return f"Default: {name}={value}"

        self.registry.register_default(default_formatter)
        formatter = self.registry.get_formatter("unknown_type")
        self.assertEqual(formatter, default_formatter)

    def test_format_value(self):
        """Test formatting a value using the registry."""

        def dummy_formatter(name, value, **kwargs):
            return f"Dummy: {name}={value}"

        self.registry.register("test_type", dummy_formatter)
        result = self.registry.format_value("test_type", "field_name", "field_value")
        self.assertEqual(result, "Dummy: field_name=field_value")


class TestBasicFormatters(unittest.TestCase):
    """Test cases for basic field formatters."""

    def test_char_formatter(self):
        """Test char field formatter."""
        # Normal case
        result = format_char_field("Name", "Test Value")
        self.assertEqual(result, "Name: Test Value")

        # Empty string
        result = format_char_field("Name", "")
        self.assertEqual(result, "Name: ")

        # None value
        result = format_char_field("Name", None)
        self.assertEqual(result, "Name: Not set")

    def test_text_formatter(self):
        """Test text field formatter."""
        # Normal case
        result = format_text_field("Description", "This is a\nmultiline text")
        self.assertEqual(result, "Description: This is a\nmultiline text")

        # None value
        result = format_text_field("Description", None)
        self.assertEqual(result, "Description: Not set")

    def test_integer_formatter(self):
        """Test integer field formatter."""
        # Normal case
        result = format_integer_field("Count", 42)
        self.assertEqual(result, "Count: 42")

        # Zero value
        result = format_integer_field("Count", 0)
        self.assertEqual(result, "Count: 0")

        # None value
        result = format_integer_field("Count", None)
        self.assertEqual(result, "Count: Not set")

    def test_float_formatter(self):
        """Test float field formatter."""
        # Normal case
        result = format_float_field("Amount", 42.5)
        self.assertEqual(result, "Amount: 42.50")

        # With digits param
        result = format_float_field("Amount", 42.5, digits=(16, 3))
        self.assertEqual(result, "Amount: 42.500")

        # Zero value
        result = format_float_field("Amount", 0.0)
        self.assertEqual(result, "Amount: 0.00")

        # None value
        result = format_float_field("Amount", None)
        self.assertEqual(result, "Amount: Not set")

    def test_boolean_formatter(self):
        """Test boolean field formatter."""
        # True value
        result = format_boolean_field("Active", True)
        self.assertEqual(result, "Active: Yes")

        # False value
        result = format_boolean_field("Active", False)
        self.assertEqual(result, "Active: No")

        # None value
        result = format_boolean_field("Active", None)
        self.assertEqual(result, "Active: Not set")


class TestDateFormatters(unittest.TestCase):
    """Test cases for date and datetime formatters."""

    def test_date_formatter(self):
        """Test date field formatter."""
        # Normal case
        test_date = datetime.date(2023, 5, 15)
        result = format_date_field("Date", test_date)
        self.assertEqual(result, "Date: 2023-05-15")

        # String date
        result = format_date_field("Date", "2023-05-15")
        self.assertEqual(result, "Date: 2023-05-15")

        # None value
        result = format_date_field("Date", None)
        self.assertEqual(result, "Date: Not set")

    def test_datetime_formatter(self):
        """Test datetime field formatter."""
        # Normal case
        test_datetime = datetime.datetime(2023, 5, 15, 14, 30, 0)
        result = format_datetime_field("Timestamp", test_datetime)
        self.assertEqual(result, "Timestamp: 2023-05-15 14:30:00")

        # String datetime
        result = format_datetime_field("Timestamp", "2023-05-15 14:30:00")
        self.assertEqual(result, "Timestamp: 2023-05-15 14:30:00")

        # None value
        result = format_datetime_field("Timestamp", None)
        self.assertEqual(result, "Timestamp: Not set")


class TestRelationalFormatters(unittest.TestCase):
    """Test cases for relational field formatters."""

    def setUp(self):
        """Set up test environment."""
        # Create a mock OdooConnection
        self.odoo = MagicMock()

    def test_many2one_formatter(self):
        """Test many2one field formatter."""
        # Setup mock for the direct field name access
        self.odoo.get_model_fields.return_value = {
            "partner": {
                "type": "many2one",
                "string": "Partner",
                "relation": "res.partner",
            }
        }

        # Normal case with ID and name
        result = format_many2one_field(
            "Partner", [42, "Test Partner"], model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Partner: Test Partner [odoo://res.partner/record/42]")

        # Case with only ID
        result = format_many2one_field(
            "Partner", 42, model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Partner: Record #42 [odoo://res.partner/record/42]")

        # None value
        result = format_many2one_field(
            "Partner", None, model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Partner: Not set")

        # False value (Odoo uses False for empty relations)
        result = format_many2one_field(
            "Partner", False, model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Partner: Not set")

        # Setup mock for field_id lookup
        self.odoo.get_model_fields.return_value = {
            "partner_id": {
                "type": "many2one",
                "string": "Partner",
                "relation": "res.partner",
            }
        }

        # Test field lookup with _id suffix
        result = format_many2one_field(
            "Partner", [42, "Test Partner"], model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Partner: Test Partner [odoo://res.partner/record/42]")

        # Setup mock for fallback to field name
        self.odoo.get_model_fields.return_value = {}

        # Test fallback to field name
        result = format_many2one_field(
            "Partner", [42, "Test Partner"], model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Partner: Test Partner [odoo://Partner/record/42]")

    def test_one2many_formatter(self):
        """Test one2many field formatter."""
        # Setup mock for direct field name lookup
        self.odoo.get_model_fields.return_value = {
            "lines": {
                "type": "one2many",
                "string": "Lines",
                "relation": "sale.order.line",
            }
        }

        # Normal case with IDs
        result = format_one2many_field(
            "Lines", [1, 2, 3], model="sale.order", odoo=self.odoo
        )
        self.assertEqual(
            result, "Lines: 3 related records [odoo://sale.order.line/browse?ids=1,2,3]"
        )

        # Empty list
        result = format_one2many_field("Lines", [], model="sale.order", odoo=self.odoo)
        self.assertEqual(result, "Lines: 0 related records")

        # None value
        result = format_one2many_field(
            "Lines", None, model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Lines: Not set")

        # False value (Odoo sometimes uses False for empty lists)
        result = format_one2many_field(
            "Lines", False, model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Lines: Not set")

        # Setup mock for _ids lookup
        self.odoo.get_model_fields.return_value = {
            "line_ids": {
                "type": "one2many",
                "string": "Lines",
                "relation": "sale.order.line",
            }
        }

        # Test field lookup with _ids suffix
        result = format_one2many_field(
            "Lines", [1, 2, 3], model="sale.order", odoo=self.odoo
        )
        self.assertEqual(
            result, "Lines: 3 related records [odoo://sale.order.line/browse?ids=1,2,3]"
        )

        # Setup mock for no relation found
        self.odoo.get_model_fields.return_value = {}

        # Test fallback when no relation is found
        result = format_one2many_field(
            "Lines", [1, 2, 3], model="sale.order", odoo=self.odoo
        )
        self.assertEqual(result, "Lines: 3 related records")

    def test_many2many_formatter(self):
        """Test many2many field formatter."""
        # Setup mock for direct field name lookup
        self.odoo.get_model_fields.return_value = {
            "tags": {
                "type": "many2many",
                "string": "Tags",
                "relation": "res.partner.category",
            }
        }

        # Normal case with IDs
        result = format_many2many_field(
            "Tags", [1, 2, 3], model="res.partner", odoo=self.odoo
        )
        self.assertEqual(
            result,
            "Tags: 3 related records [odoo://res.partner.category/browse?ids=1,2,3]",
        )

        # Empty list
        result = format_many2many_field("Tags", [], model="res.partner", odoo=self.odoo)
        self.assertEqual(result, "Tags: 0 related records")

        # None value
        result = format_many2many_field(
            "Tags", None, model="res.partner", odoo=self.odoo
        )
        self.assertEqual(result, "Tags: Not set")

        # Setup mock for _ids lookup
        self.odoo.get_model_fields.return_value = {
            "tag_ids": {
                "type": "many2many",
                "string": "Tags",
                "relation": "res.partner.category",
            }
        }

        # Test field lookup with _ids suffix
        result = format_many2many_field(
            "Tags", [1, 2, 3], model="res.partner", odoo=self.odoo
        )
        self.assertEqual(
            result,
            "Tags: 3 related records [odoo://res.partner.category/browse?ids=1,2,3]",
        )

        # Setup mock for no relation found
        self.odoo.get_model_fields.return_value = {}

        # Test fallback when no relation is found
        result = format_many2many_field(
            "Tags", [1, 2, 3], model="res.partner", odoo=self.odoo
        )
        self.assertEqual(result, "Tags: 3 related records")


class TestBinaryFormatter(unittest.TestCase):
    """Test cases for binary field formatter."""

    def test_binary_formatter(self):
        """Test binary field formatter."""
        # Normal case
        result = format_binary_field("Image", "base64encodeddata")
        self.assertEqual(result, "Image: [Binary data]")

        # With human_size param
        result = format_binary_field("Image", "base64encodeddata", human_size="10 KB")
        self.assertEqual(result, "Image: [Binary data, 10 KB]")

        # None value
        result = format_binary_field("Image", None)
        self.assertEqual(result, "Image: Not set")


class TestDefaultFormatter(unittest.TestCase):
    """Test cases for default field formatter."""

    def test_default_formatter(self):
        """Test default field formatter."""
        # String value
        result = format_default_field("Unknown", "Test Value")
        self.assertEqual(result, "Unknown: Test Value")

        # Numeric value
        result = format_default_field("Unknown", 42)
        self.assertEqual(result, "Unknown: 42")

        # None value
        result = format_default_field("Unknown", None)
        self.assertEqual(result, "Unknown: Not set")


class TestRegisterDefaultFormatters(unittest.TestCase):
    """Test registering default formatters."""

    def test_register_default_formatters(self):
        """Test that all default formatters are registered."""
        registry = FieldFormatterRegistry()
        register_default_formatters(registry)

        # Check that all expected formatters are registered
        expected_types = [
            "char",
            "text",
            "integer",
            "float",
            "monetary",
            "boolean",
            "date",
            "datetime",
            "selection",
            "many2one",
            "one2many",
            "many2many",
            "binary",
        ]

        for field_type in expected_types:
            self.assertIn(field_type, registry.formatters)

        # Verify default formatter is registered
        self.assertIsNotNone(registry.default_formatter)


if __name__ == "__main__":
    unittest.main()
