"""Tests for the utils module - additional coverage."""

import unittest

from mcp_server_odoo.utils import (
    build_browse_uri,
    build_count_uri,
    build_fields_uri,
    build_record_uri,
    build_resource_uri,
    build_search_uri,
    get_model_display_name,
    parse_domain,
    sanitize_string,
)


class TestParseDomainsFunction(unittest.TestCase):
    """Test parse_domain function."""

    def test_empty_domain(self):
        """Test parsing an empty domain."""
        result = parse_domain("")
        self.assertEqual(result, [])

    def test_simple_domain(self):
        """Test parsing a simple domain."""
        domain_str = '[["name", "=", "Test"]]'
        result = parse_domain(domain_str)
        self.assertEqual(result, [["name", "=", "Test"]])

    def test_complex_domain(self):
        """Test parsing a complex domain with logical operators."""
        domain_str = '["&", ["active", "=", true], ["name", "like", "Test%"]]'
        result = parse_domain(domain_str)
        self.assertEqual(
            result, ["&", ["active", "=", True], ["name", "like", "Test%"]]
        )

    def test_url_encoded_domain(self):
        """Test parsing a URL-encoded domain."""
        # URL encoded version of [["name", "=", "Test & Co."]]
        domain_str = "%5B%5B%22name%22%2C%20%22%3D%22%2C%20%22Test%20%26%20Co.%22%5D%5D"
        result = parse_domain(domain_str)
        self.assertEqual(result, [["name", "=", "Test & Co."]])

    def test_invalid_domain_format(self):
        """Test handling invalid domain format."""
        # Not a valid JSON
        with self.assertRaises(ValueError) as context:
            parse_domain("not-a-valid-domain")
        self.assertIn("Invalid domain format", str(context.exception))

    def test_non_list_domain(self):
        """Test handling domain that is not a list."""
        with self.assertRaises(ValueError) as context:
            parse_domain('{"domain": "not-a-list"}')
        self.assertIn("Invalid domain format", str(context.exception))

    def test_invalid_condition_length(self):
        """Test handling invalid condition length."""
        with self.assertRaises(ValueError) as context:
            parse_domain('[["name", "="]]')  # Missing third element
        self.assertIn("Invalid domain format", str(context.exception))

    def test_invalid_operator(self):
        """Test handling invalid operator."""
        with self.assertRaises(ValueError) as context:
            parse_domain('[["name", "invalid_op", "Test"]]')
        self.assertIn("Invalid operator in domain condition", str(context.exception))

    def test_invalid_logical_operator(self):
        """Test handling invalid logical operator."""
        with self.assertRaises(ValueError) as context:
            parse_domain('[["invalid", ["name", "=", "Test"]]]')
        self.assertIn("Invalid domain format", str(context.exception))

    def test_invalid_domain_item_type(self):
        """Test handling invalid domain item type."""
        with self.assertRaises(ValueError) as context:
            parse_domain('[[123, ["name", "=", "Test"]]]')  # 123 is not string or list
        self.assertIn("Invalid domain format", str(context.exception))


class TestSanitizeStringFunction(unittest.TestCase):
    """Test sanitize_string function."""

    def test_normal_string(self):
        """Test sanitizing a normal string."""
        result = sanitize_string("Normal text")
        self.assertEqual(result, "Normal text")

    def test_string_with_control_chars(self):
        """Test sanitizing a string with control characters."""
        # Include some control characters (ASCII < 32)
        result = sanitize_string("Text with\x01\x02control\x03chars")
        self.assertEqual(result, "Text withcontrolchars")

        # But keep newlines, tabs, etc.
        result = sanitize_string("Text with\nnewlines\tand\rtabs")
        self.assertEqual(result, "Text with\nnewlines\tand\rtabs")

    def test_non_string_input(self):
        """Test sanitizing a non-string input."""
        result = sanitize_string(123)
        self.assertEqual(result, "123")

        result = sanitize_string(None)
        self.assertEqual(result, "None")

    def test_very_long_string(self):
        """Test sanitizing a very long string gets truncated."""
        long_string = "a" * 2000  # 2000 characters
        result = sanitize_string(long_string)
        self.assertTrue(len(result) > 1000)  # At least 1000 chars
        self.assertTrue(result.endswith("... (truncated)"))


class TestGetModelDisplayNameFunction(unittest.TestCase):
    """Test get_model_display_name function."""

    def test_model_with_dots(self):
        """Test getting display name for model with dots."""
        result = get_model_display_name("res.partner")
        self.assertEqual(result, "Partner")

        result = get_model_display_name("product.product")
        self.assertEqual(result, "Product")

    def test_model_without_dots(self):
        """Test getting display name for model without dots."""
        result = get_model_display_name("customer")
        self.assertEqual(result, "Customer")

    def test_model_with_underscores(self):
        """Test getting display name for model with underscores."""
        result = get_model_display_name("sale_order")
        self.assertEqual(result, "Sale Order")

        result = get_model_display_name("res.partner_category")
        self.assertEqual(result, "Partner Category")


class TestUriBuilderFunctions(unittest.TestCase):
    """Test URI builder functions."""

    def test_build_resource_uri_basic(self):
        """Test building a basic resource URI."""
        result = build_resource_uri("res.partner", "record/1")
        self.assertEqual(result, "odoo://res.partner/record/1")

    def test_build_resource_uri_with_params(self):
        """Test building a resource URI with parameters."""
        result = build_resource_uri(
            "res.partner",
            "search",
            {"limit": 10, "offset": 0, "domain": '[["active", "=", true]]'},
        )
        # Just check that the result contains all necessary components
        self.assertTrue(result.startswith("odoo://res.partner/search?"))
        self.assertIn("limit=10", result)
        self.assertIn("offset=0", result)
        self.assertIn("domain=", result)
        self.assertIn("active", result)

    def test_build_resource_uri_invalid_model(self):
        """Test building a resource URI with invalid model."""
        with self.assertRaises(ValueError):
            build_resource_uri("Invalid Model", "record/1")

    def test_build_resource_uri_invalid_operation(self):
        """Test building a resource URI with invalid operation."""
        with self.assertRaises(ValueError):
            build_resource_uri("res.partner", "invalid_operation")

    def test_build_record_uri(self):
        """Test building a record URI."""
        result = build_record_uri("res.partner", 42)
        self.assertEqual(result, "odoo://res.partner/record/42")

        # Should work with string IDs too
        result = build_record_uri("res.partner", "42")
        self.assertEqual(result, "odoo://res.partner/record/42")

    def test_build_search_uri_minimal(self):
        """Test building a minimal search URI."""
        result = build_search_uri("res.partner")
        self.assertEqual(result, "odoo://res.partner/search")

    def test_build_search_uri_complete(self):
        """Test building a complete search URI with all parameters."""
        result = build_search_uri(
            model="res.partner",
            domain=[["active", "=", True]],
            fields=["name", "email"],
            limit=10,
            offset=20,
            order="name desc",
        )

        # Check model and operation
        self.assertTrue(result.startswith("odoo://res.partner/search?"))

        # Check parameters
        self.assertIn("domain=", result)
        self.assertIn("fields=name%2Cemail", result)
        self.assertIn("limit=10", result)
        self.assertIn("offset=20", result)
        self.assertIn("order=name", result)
        self.assertIn("desc", result)

    def test_build_browse_uri(self):
        """Test building a browse URI."""
        result = build_browse_uri("res.partner", [1, 2, 3])
        self.assertEqual(result, "odoo://res.partner/browse?ids=1%2C2%2C3")

        # Should work with string IDs too
        result = build_browse_uri("res.partner", ["1", "2", "3"])
        self.assertEqual(result, "odoo://res.partner/browse?ids=1%2C2%2C3")

    def test_build_count_uri_minimal(self):
        """Test building a minimal count URI."""
        result = build_count_uri("res.partner")
        self.assertEqual(result, "odoo://res.partner/count")

    def test_build_count_uri_with_domain(self):
        """Test building a count URI with domain."""
        result = build_count_uri("res.partner", [["active", "=", True]])
        self.assertTrue(result.startswith("odoo://res.partner/count?domain="))
        self.assertIn("active", result)
        self.assertIn("true", result)

    def test_build_fields_uri(self):
        """Test building a fields URI."""
        result = build_fields_uri("res.partner")
        self.assertEqual(result, "odoo://res.partner/fields")


if __name__ == "__main__":
    unittest.main()
