"""Test the URI parser for Odoo MCP Server.

This module tests the functionality of the URI parser for Odoo MCP resources.
"""

import urllib.parse

import pytest

from mcp_server_odoo.utils import build_resource_uri, parse_domain, parse_uri


class TestURIParser:
    """Test cases for URI parser functionality."""

    def test_parse_basic_uri(self):
        """Test parsing a basic URI without parameters."""
        model, operation, params = parse_uri("odoo://res.partner/record/42")
        assert model == "res.partner"
        assert operation == "record/42"
        assert params == {}

    def test_parse_uri_with_params(self):
        """Test parsing a URI with query parameters."""
        uri = "odoo://product.template/search?limit=10&offset=0&order=name"
        model, operation, params = parse_uri(uri)
        assert model == "product.template"
        assert operation == "search"
        assert params == {"limit": "10", "offset": "0", "order": "name"}

    def test_parse_uri_with_domain(self):
        """Test parsing a URI with a domain parameter."""
        domain = '[["name", "ilike", "Test"], ["active", "=", true]]'
        encoded_domain = urllib.parse.quote(domain)
        uri = f"odoo://res.partner/search?domain={encoded_domain}"

        model, operation, params = parse_uri(uri)
        assert model == "res.partner"
        assert operation == "search"
        assert "domain" in params

        parsed_domain = parse_domain(params["domain"])
        assert parsed_domain == [["name", "ilike", "Test"], ["active", "=", True]]

    def test_parse_uri_with_repeated_params(self):
        """Test parsing a URI with repeated parameters."""
        uri = "odoo://product.product/search?fields=name&fields=code&fields=price"
        model, operation, params = parse_uri(uri)
        assert model == "product.product"
        assert operation == "search"
        assert params["fields"] == ["name", "code", "price"]

    def test_invalid_uri_format(self):
        """Test parsing an invalid URI format."""
        invalid_uris = [
            "invalid://res.partner/record/42",  # Wrong scheme
            "odoo:/res.partner/record/42",  # Missing slash
            "odoo:res.partner/record/42",  # Missing slashes
            "odoo://record/42",  # Missing model
            "odoo://res.partner",  # Missing operation
        ]

        for uri in invalid_uris:
            with pytest.raises(ValueError):
                parse_uri(uri)

    def test_parse_operation_types(self):
        """Test parsing different operation types."""
        operations = {
            "record/42": ("record", "42"),
            "search": ("search", None),
            "browse": ("browse", None),
            "count": ("count", None),
            "fields": ("fields", None),
        }

        for op_str, expected in operations.items():
            uri = f"odoo://res.partner/{op_str}"
            model, operation, _ = parse_uri(uri)

            if expected[1] is not None:
                assert operation == op_str
            else:
                assert operation == expected[0]

    def test_validate_model_name(self):
        """Test validation of model names."""
        # Test with valid model name patterns
        valid_models = ["res.partner", "product_template", "sale.order.line"]
        for model in valid_models:
            uri = f"odoo://{model}/fields"
            parsed_model, _, _ = parse_uri(uri)
            assert parsed_model == model

        # Test with invalid model name patterns (if validation is implemented)
        invalid_models = ["res/partner", "product.template!", ""]
        for model in invalid_models:
            uri = f"odoo://{model}/fields"
            with pytest.raises(ValueError):
                parse_uri(uri)

    def test_parse_complex_domain(self):
        """Test parsing a complex domain with nested conditions."""
        domain = '["|", ["type", "=", "service"], "&", ["sale_ok", "=", true], ["purchase_ok", "=", false]]'
        encoded_domain = urllib.parse.quote(domain)
        uri = f"odoo://product.template/search?domain={encoded_domain}"

        _, _, params = parse_uri(uri)
        parsed_domain = parse_domain(params["domain"])

        expected = [
            "|",
            ["type", "=", "service"],
            "&",
            ["sale_ok", "=", True],
            ["purchase_ok", "=", False],
        ]
        assert parsed_domain == expected

    def test_build_resource_uri(self):
        """Test building resource URIs from components."""
        # Basic URI without parameters
        uri = build_resource_uri("res.partner", "record/42")
        assert uri == "odoo://res.partner/record/42"

        # URI with simple parameters
        uri = build_resource_uri(
            "product.template", "search", {"limit": 10, "offset": 0, "order": "name"}
        )
        assert uri == "odoo://product.template/search?limit=10&offset=0&order=name"

        # URI with domain parameter
        domain = [["name", "ilike", "Test"], ["active", "=", True]]
        uri = build_resource_uri("res.partner", "search", {"domain": domain})

        # Parse back to verify
        model, operation, params = parse_uri(uri)
        assert model == "res.partner"
        assert operation == "search"
        parsed_domain = parse_domain(params["domain"])
        assert parsed_domain == domain

    def test_build_related_resource_uris(self):
        """Test building URIs for related resources."""
        # URI for a related one2many field
        many_uri = build_resource_uri(
            "sale.order.line", "search", {"domain": [["order_id", "=", 42]]}
        )

        # URI for a related many2one field
        one_uri = build_resource_uri("res.partner", "record/42")

        # Verify
        assert many_uri.startswith("odoo://sale.order.line/search?domain=")
        assert one_uri == "odoo://res.partner/record/42"

        # Parse the many URI to verify the domain
        _, _, params = parse_uri(many_uri)
        parsed_domain = parse_domain(params["domain"])
        assert parsed_domain == [["order_id", "=", 42]]


class TestDomainParser:
    """Test cases for domain parser functionality."""

    def test_parse_empty_domain(self):
        """Test parsing an empty domain."""
        assert parse_domain("") == []
        assert parse_domain("[]") == []

    def test_parse_simple_domain(self):
        """Test parsing a simple domain."""
        domain_str = '[["name", "=", "Test"]]'
        parsed = parse_domain(domain_str)
        assert parsed == [["name", "=", "Test"]]

    def test_parse_complex_domain(self):
        """Test parsing a complex domain with logical operators."""
        domain_str = '["|", ["type", "=", "service"], "&", ["sale_ok", "=", true], ["purchase_ok", "=", false]]'
        parsed = parse_domain(domain_str)
        assert parsed == [
            "|",
            ["type", "=", "service"],
            "&",
            ["sale_ok", "=", True],
            ["purchase_ok", "=", False],
        ]

    def test_parse_nested_domain(self):
        """Test parsing a nested domain with multiple logical operations."""
        domain_str = '["|", ["name", "ilike", "Test"], "|", ["code", "=", "X123"], "&", ["active", "=", true], ["type", "!=", "service"]]'
        parsed = parse_domain(domain_str)
        expected = [
            "|",
            ["name", "ilike", "Test"],
            "|",
            ["code", "=", "X123"],
            "&",
            ["active", "=", True],
            ["type", "!=", "service"],
        ]
        assert parsed == expected

    def test_parse_invalid_domain(self):
        """Test parsing invalid domains."""
        invalid_domains = [
            '{"key": "value"}',  # Not a list
            '[["name"]]',  # Not enough elements
            '[["name", "=", "Test", "extra"]]',  # Too many elements
            "not json",  # Not JSON
            '[["operator", "invalid", "value"]]',  # Invalid operator (this only tests syntax, not semantics)
        ]

        for domain in invalid_domains:
            with pytest.raises(ValueError):
                parse_domain(domain)

    def test_url_encoded_domain(self):
        """Test parsing URL-encoded domains."""
        domain = '[["name", "=", "Test & Company"], ["code", "=", "A&B"]]'
        encoded = urllib.parse.quote(domain)
        parsed = parse_domain(encoded)
        assert parsed == [["name", "=", "Test & Company"], ["code", "=", "A&B"]]
