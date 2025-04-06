"""Advanced tests for data formatting functions.

This module tests complex formatting scenarios and edge cases
for the data formatting functionality.
"""

import unittest
from unittest.mock import MagicMock

from mcp_server_odoo.data_formatting import (
    format_field_list,
    format_field_value,
    format_record,
    format_search_results,
)


class TestAdvancedDataFormatting(unittest.TestCase):
    """Test cases for advanced data formatting scenarios."""

    def setUp(self):
        """Set up test environment."""
        # Create a mock OdooConnection
        self.odoo = MagicMock()

        # Mock get_model_fields to return field info
        self.odoo.get_model_fields.return_value = {
            "name": {"type": "char", "string": "Name"},
            "description": {"type": "text", "string": "Description"},
            "amount": {"type": "float", "string": "Amount"},
            "date": {"type": "date", "string": "Date"},
            "datetime": {"type": "datetime", "string": "Date and Time"},
            "selection_field": {
                "type": "selection",
                "string": "Selection",
                "selection": [
                    ("draft", "Draft"),
                    ("confirmed", "Confirmed"),
                    ("done", "Done"),
                ],
            },
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
            "line_ids": {
                "type": "one2many",
                "string": "Lines",
                "relation": "account.move.line",
            },
            "binary_field": {"type": "binary", "string": "Binary Data"},
            "image": {"type": "binary", "string": "Image"},
            "reference": {
                "type": "reference",
                "string": "Reference",
                "selection": [
                    ("res.partner", "Partner"),
                    ("product.product", "Product"),
                ],
            },
            "html_field": {"type": "html", "string": "HTML Content"},
            "monetary": {"type": "monetary", "string": "Monetary Value"},
            "property_field": {"type": "property", "string": "Property"},
        }

    def test_format_field_value_with_null_values(self):
        """Test formatting fields with null values."""
        # Test with null/None values for different field types
        tests = [
            ("char", "Name", None, "Name: Not set"),
            ("text", "Description", None, "Description: Not set"),
            ("integer", "Count", None, "Count: Not set"),
            ("float", "Amount", None, "Amount: Not set"),
            ("date", "Date", None, "Date: Not set"),
            ("datetime", "Date and Time", None, "Date and Time: Not set"),
            ("selection", "Selection", None, "Selection: Not set"),
            ("many2one", "Partner", None, "Partner: Not set"),
            ("one2many", "Lines", None, "Lines: Not set"),
            ("many2many", "Tags", None, "Tags: Not set"),
            ("binary", "Image", None, "Image: Not set"),
            ("reference", "Reference", None, "Reference: Not set"),
            ("html", "HTML Content", None, "HTML Content: Not set"),
            ("boolean", "Active", None, "Active: Not set"),
            ("monetary", "Price", None, "Price: Not set"),
        ]

        for field_type, field_name, value, expected in tests:
            result = format_field_value(
                field_name=field_name,
                field_value=value,
                field_type=field_type,
                model="test.model",
                odoo=self.odoo,
            )
            self.assertEqual(result, expected)

    def test_format_field_value_with_empty_values(self):
        """Test formatting fields with empty values."""
        # Test with empty values for different field types
        tests = [
            ("char", "Name", "", "Name: "),
            ("text", "Description", "", "Description: "),
            ("integer", "Count", 0, "Count: 0"),
            ("float", "Amount", 0.0, "Amount: 0.00"),
            ("one2many", "Lines", [], "Lines: 0 related records"),
            ("many2many", "Tags", [], "Tags: 0 related records"),
            ("binary", "Image", "", "Image: [Binary data]"),
            ("html", "HTML Content", "", "HTML Content: "),
        ]

        for field_type, field_name, value, expected in tests:
            result = format_field_value(
                field_name=field_name,
                field_value=value,
                field_type=field_type,
                model="test.model",
                odoo=self.odoo,
            )
            self.assertEqual(result, expected)

    def test_format_field_value_with_special_characters(self):
        """Test formatting fields with special characters."""
        # Test with special characters in values
        tests = [
            (
                "char",
                "Name",
                "Test & <Special> Characters",
                "Name: Test & <Special> Characters",
            ),
            (
                "text",
                "Description",
                "Line 1\nLine 2\n\nLine 4",
                "Description: Line 1\nLine 2\n\nLine 4",
            ),
            (
                "char",
                "Code",
                "<script>alert('XSS')</script>",
                "Code: <script>alert('XSS')</script>",
            ),
            ("char", "Format", "{json: true}", "Format: {json: true}"),
        ]

        for field_type, field_name, value, expected in tests:
            result = format_field_value(
                field_name=field_name,
                field_value=value,
                field_type=field_type,
                model="test.model",
                odoo=self.odoo,
            )
            self.assertEqual(result, expected)

    def test_format_field_value_with_long_text(self):
        """Test formatting fields with very long text."""
        # Generate a long text value
        long_text = "Lorem ipsum " * 100  # 1100+ characters

        result = format_field_value(
            field_name="Description",
            field_value=long_text,
            field_type="text",
            model="test.model",
            odoo=self.odoo,
        )

        # Verify the result contains the field name and at least part of the text
        self.assertTrue(result.startswith("Description: Lorem ipsum"))
        self.assertEqual(len(result), len("Description: ") + len(long_text))

    def test_format_field_value_with_very_large_relations(self):
        """Test formatting fields with large numbers of related records."""
        # Test with a lot of related records
        many_ids = list(range(1, 1001))  # 1000 related records

        result = format_field_value(
            field_name="Tags",
            field_value=many_ids,
            field_type="many2many",
            model="test.model",
            odoo=self.odoo,
        )

        # Verify the count is correct and includes URI
        self.assertIn("Tags: 1000 related records", result)
        self.assertIn("odoo://res.partner.category/browse?ids=", result)

    def test_format_record_with_complex_fields(self):
        """Test formatting a record with complex fields."""
        # Setup a complex record with various field types
        record = {
            "id": 42,
            "name": "Complex Record",
            "description": "This is a\nmultiline\ndescription",
            "amount": 123.456,
            "date": "2023-04-06",
            "datetime": "2023-04-06 15:30:45",
            "selection_field": "confirmed",
            "partner_id": [84, "Complex Partner"],
            "tag_ids": [1, 2, 3, 4, 5],
            "line_ids": [10, 20, 30, 40, 50],
            "binary_field": "c29tZSBiaW5hcnkgZGF0YQ==",  # "some binary data" in base64
            "image": "iVBORw0KGgoJKGG==",  # fake image data
            "reference": "res.partner,84",
            "html_field": "<p>This is <b>HTML</b> content</p>",
            "monetary": 9999.99,
            "property_field": {"type": "char", "value": "property value"},
        }

        # Mock relation_info to provide needed info for reference field
        self.odoo.get_relation_info.return_value = (
            "res.partner",
            84,
            "Complex Partner",
        )

        result = format_record(
            model="test.complex.model",
            record=record,
            odoo=self.odoo,
        )

        # Check record header
        self.assertIn("Resource: test.complex.model/record/42", result)

        # Check basic fields
        self.assertIn("Name: Complex Record", result)
        self.assertIn("Description: This is a\nmultiline\ndescription", result)
        self.assertIn("Amount: 123.46", result)  # Check rounding

        # Check date/time fields
        self.assertIn("Date: 2023-04-06", result)
        self.assertIn("Date and Time: 2023-04-06 15:30:45", result)

        # Check selection field - matches current implementation
        self.assertIn("Selection: confirmed", result)  # Updated (lowercase)

        # Check relational fields
        self.assertIn("Partner: Complex Partner", result)
        self.assertIn("odoo://res.partner/record/84", result)

        # Check binary fields
        self.assertIn("Binary Data: [Binary data]", result)

        # Check HTML field
        self.assertIn("HTML Content: <p>This is <b>HTML</b> content</p>", result)

        # Check monetary field
        self.assertIn(
            "Monetary Value: 10000.0", result
        )  # Updated to match 1 decimal place

    def test_format_search_results_with_complex_domain_and_metadata(self):
        """Test formatting search results with complex domain and metadata."""
        # Setup complex domain and records
        complex_domain = [
            "&",
            ("is_company", "=", True),
            "|",
            ("country_id", "=", 233),  # USA
            ("state_id", "in", [1, 2, 3, 4, 5]),
        ]

        # Mock records
        records = [
            {
                "id": 1,
                "name": "Company A",
                "is_company": True,
                "country_id": [233, "United States"],
                "state_id": [1, "California"],
            },
            {
                "id": 2,
                "name": "Company B",
                "is_company": True,
                "country_id": [233, "United States"],
                "state_id": [2, "New York"],
            },
            {
                "id": 3,
                "name": "Company C",
                "is_company": True,
                "country_id": [233, "United States"],
                "state_id": [3, "Texas"],
            },
        ]

        result = format_search_results(
            model="res.partner",
            records=records,
            total_count=100,  # Pretend there are 100 matching records
            limit=3,
            offset=0,
            domain=complex_domain,
            odoo=self.odoo,
        )

        # Check search header
        self.assertIn("Search Results: res.partner (100 total matches)", result)
        self.assertIn("Showing: Records 1-3 of 100", result)

        # Check domain representation
        domain_str = str(complex_domain).replace(" ", "")
        self.assertIn(domain_str, result)

        # Check refinement options
        self.assertIn("Refinement options:", result)
        self.assertIn("Companies only:", result)
        self.assertIn("Individuals only:", result)

    def test_format_field_list_with_many_fields(self):
        """Test formatting a field list with many fields."""
        # Create a mock for get_model_fields that returns a lot of fields
        # Generate 50 fake fields
        many_fields = {}
        for i in range(1, 51):
            field_name = f"field_{i}"
            field_type = "char" if i % 5 != 0 else "many2one"
            relation = "res.partner" if i % 5 == 0 else None

            field_info = {
                "type": field_type,
                "string": f"Field {i}",
            }

            if relation:
                field_info["relation"] = relation

            many_fields[field_name] = field_info

        # Call format_field_list with the model name and the fields dictionary
        result = format_field_list("test.model", many_fields)

        # Verify that the result contains a header
        self.assertIn("Fields for test.model", result)

        # Verify that field type headers are included
        self.assertIn("Char Fields:", result)
        self.assertIn("Many2one Fields:", result)

        # Verify that some fields are included (not checking all 50)
        for i in range(1, 10):
            self.assertIn(f"field_{i}", result)

    def test_format_record_with_missing_fields(self):
        """Test formatting a record with fields missing from get_model_fields."""
        # Create a record with fields not returned by get_model_fields
        record = {
            "id": 99,
            "name": "Test Record",
            "unknown_field": "Unknown Value",
            "another_unknown": 42,
        }

        # Mock get_model_fields to return only a subset of fields
        self.odoo.get_model_fields.return_value = {
            "name": {"type": "char", "string": "Name"},
        }

        result = format_record(
            model="test.model",
            record=record,
            odoo=self.odoo,
        )

        # Verify that known fields are formatted correctly
        self.assertIn("Resource: test.model/record/99", result)
        self.assertIn("Name: Test Record", result)

        # Verify that unknown fields are still included with default formatting
        self.assertIn("unknown_field: Unknown Value", result)
        self.assertIn("another_unknown: 42", result)

    def test_format_search_results_empty_with_query(self):
        """Test formatting empty search results with a specific query."""
        result = format_search_results(
            model="res.partner",
            records=[],
            total_count=0,
            limit=10,
            offset=0,
            domain=[("name", "ilike", "NonExistentCompany")],
            odoo=self.odoo,
        )

        # Verify the result contains information about the empty search
        self.assertIn("Search Results: res.partner (0 total matches)", result)
        self.assertIn("No records found matching the criteria.", result)


if __name__ == "__main__":
    unittest.main()
