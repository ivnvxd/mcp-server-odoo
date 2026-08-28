"""Tests for the URI schema module."""

import pytest

from mcp_server_odoo.uri_schema import (
    OdooOperation,
    URIParseError,
    URIValidationError,
    build_pagination_uri,
    build_record_uri,
    build_uri,
    extract_model_from_uri,
    parse_uri,
)


class TestURIParsing:
    """Test URI parsing functionality."""

    def test_parse_record_uri(self):
        """Test parsing record URIs."""
        uri = "odoo://res.partner/record/42"
        parsed = parse_uri(uri)

        assert parsed.model == "res.partner"
        assert parsed.operation == OdooOperation.RECORD
        assert parsed.record_id == 42
        assert parsed.domain is None
        assert parsed.fields is None
        assert parsed.limit is None
        assert parsed.offset is None
        assert parsed.order is None
        assert parsed.ids is None

    def test_parse_search_uri_simple(self):
        """Test parsing simple search URIs."""
        uri = "odoo://product.product/search"
        parsed = parse_uri(uri)

        assert parsed.model == "product.product"
        assert parsed.operation == OdooOperation.SEARCH
        assert parsed.record_id is None
        assert parsed.domain is None

    def test_parse_search_uri_with_domain(self):
        """Test parsing search URIs with domain parameter."""
        uri = "odoo://res.partner/search?domain=[('is_company','=',True)]"
        parsed = parse_uri(uri)

        assert parsed.model == "res.partner"
        assert parsed.operation == OdooOperation.SEARCH
        assert parsed.domain == "[('is_company','=',True)]"

    def test_parse_search_uri_with_all_params(self):
        """Test parsing search URIs with all parameters."""
        uri = "odoo://sale.order/search?domain=[('state','=','sale')]&fields=name,partner_id&limit=10&offset=20&order=date_order desc"
        parsed = parse_uri(uri)

        assert parsed.model == "sale.order"
        assert parsed.operation == OdooOperation.SEARCH
        assert parsed.domain == "[('state','=','sale')]"
        assert parsed.fields == ["name", "partner_id"]
        assert parsed.limit == 10
        assert parsed.offset == 20
        assert parsed.order == "date_order desc"

    def test_parse_count_uri(self):
        """Test parsing count URIs."""
        uri = "odoo://res.partner/count?domain=[('country_id.code','=','US')]"
        parsed = parse_uri(uri)

        assert parsed.model == "res.partner"
        assert parsed.operation == OdooOperation.COUNT
        assert parsed.domain == "[('country_id.code','=','US')]"

    def test_parse_fields_uri(self):
        """Test parsing fields URIs."""
        uri = "odoo://product.template/fields"
        parsed = parse_uri(uri)

        assert parsed.model == "product.template"
        assert parsed.operation == OdooOperation.FIELDS

    def test_parse_uri_with_url_encoded_domain(self):
        """Test parsing URIs with URL-encoded domain."""
        uri = "odoo://res.partner/search?domain=%5B%28%27is_company%27%2C%27%3D%27%2CTrue%29%5D"
        parsed = parse_uri(uri)

        assert parsed.domain == "[('is_company','=',True)]"

    def test_parse_uri_with_empty_fields(self):
        """Test parsing URIs with empty fields parameter."""
        uri = "odoo://res.partner/search?fields="
        parsed = parse_uri(uri)

        assert parsed.fields is None

    def test_parse_uri_with_complex_model_name(self):
        """Test parsing URIs with complex model names."""
        uri = "odoo://account.invoice.line/search"
        parsed = parse_uri(uri)

        assert parsed.model == "account.invoice.line"

    def test_parse_uri_invalid_scheme(self):
        """Test parsing URIs with invalid scheme."""
        with pytest.raises(URIParseError, match="URI must start with 'odoo://'"):
            parse_uri("http://res.partner/search")

    def test_parse_uri_invalid_format(self):
        """Test parsing URIs with invalid format."""
        with pytest.raises(URIParseError, match="Invalid URI format"):
            parse_uri("odoo://")

    def test_parse_uri_invalid_model_name(self):
        """Test parsing URIs with invalid model names."""
        with pytest.raises(URIValidationError, match="Invalid model name"):
            parse_uri("odoo://123invalid/search")

        # Empty model name results in invalid URI format
        with pytest.raises(URIParseError, match="Invalid URI format"):
            parse_uri("odoo:///search")

    def test_parse_uri_invalid_operation(self):
        """Test parsing URIs with invalid operations."""
        with pytest.raises(URIValidationError, match="Invalid operation"):
            parse_uri("odoo://res.partner/invalid_op")

    def test_parse_uri_record_without_id(self):
        """Test parsing record URIs without ID."""
        with pytest.raises(URIValidationError, match="Record operation requires an ID"):
            parse_uri("odoo://res.partner/record")

    def test_parse_uri_invalid_limit(self):
        """Test parsing URIs with invalid limit parameter."""
        with pytest.raises(URIValidationError, match="Invalid limit value"):
            parse_uri("odoo://res.partner/search?limit=abc")

        with pytest.raises(URIValidationError, match="limit must be non-negative"):
            parse_uri("odoo://res.partner/search?limit=-5")

    def test_parse_uri_invalid_offset(self):
        """Test parsing URIs with invalid offset parameter."""
        with pytest.raises(URIValidationError, match="Invalid offset value"):
            parse_uri("odoo://res.partner/search?offset=xyz")

    def test_odoo_uri_to_uri(self):
        """Test converting OdooURI back to string."""
        original = (
            "odoo://res.partner/search?domain=[('is_company','=',True)]&fields=name,email&limit=10"
        )
        parsed = parse_uri(original)
        rebuilt = parsed.to_uri()

        # Parse again to compare
        reparsed = parse_uri(rebuilt)
        assert reparsed.model == parsed.model
        assert reparsed.operation == parsed.operation
        assert reparsed.domain == parsed.domain
        assert reparsed.fields == parsed.fields
        assert reparsed.limit == parsed.limit


class TestURIBuilding:
    """Test URI building functionality."""

    def test_build_record_uri(self):
        """Test building record URIs."""
        uri = build_uri("res.partner", "record", record_id=42)
        assert uri == "odoo://res.partner/record/42"

    def test_build_uri_search_simple(self):
        """Test building simple search URIs."""
        uri = build_uri("product.product", "search")
        assert uri == "odoo://product.product/search"

    def test_build_uri_search_with_domain(self):
        """Test building search URIs with domain."""
        uri = build_uri("res.partner", "search", domain="[('is_company','=',True)]")
        assert (
            uri
            == "odoo://res.partner/search?domain=%5B%28%27is_company%27%2C%27%3D%27%2CTrue%29%5D"
        )

    def test_build_uri_search_with_all_params(self):
        """Test building search URIs with all parameters."""
        uri = build_uri(
            "sale.order",
            "search",
            domain="[('state','=','sale')]",
            fields=["name", "partner_id", "amount_total"],
            limit=25,
            offset=50,
            order="date_order desc",
        )

        parsed = parse_uri(uri)
        assert parsed.model == "sale.order"
        assert parsed.operation == OdooOperation.SEARCH
        assert parsed.domain == "[('state','=','sale')]"
        assert parsed.fields == ["name", "partner_id", "amount_total"]
        assert parsed.limit == 25
        assert parsed.offset == 50
        assert parsed.order == "date_order desc"

    def test_build_count_uri(self):
        """Test building count URIs."""
        uri = build_uri("res.partner", "count", domain="[('country_id.code','=','US')]")
        parsed = parse_uri(uri)
        assert parsed.operation == OdooOperation.COUNT
        assert parsed.domain == "[('country_id.code','=','US')]"

    def test_build_fields_uri(self):
        """Test building fields URIs."""
        uri = build_uri("product.template", "fields")
        assert uri == "odoo://product.template/fields"

    def test_build_uri_invalid_model(self):
        """Test building URIs with invalid model names."""
        with pytest.raises(URIValidationError, match="Invalid model name"):
            build_uri("", "search")

    def test_build_uri_invalid_operation(self):
        """Test building URIs with invalid operations."""
        with pytest.raises(URIValidationError, match="Invalid operation"):
            build_uri("res.partner", "invalid_op")

    def test_build_uri_record_without_id(self):
        """Test building record URIs without ID."""
        with pytest.raises(URIValidationError, match="Record operation requires an ID"):
            build_uri("res.partner", "record")


class TestURIConvenienceFunctions:
    """Test convenience functions for URI building."""

    def test_build_record_uri_convenience(self):
        """Test the build_record_uri convenience function."""
        uri = build_record_uri("res.partner", 123)
        assert uri == "odoo://res.partner/record/123"

    def test_build_pagination_uri(self):
        """Test building pagination URIs."""
        base = "odoo://res.partner/search?domain=[('is_company','=',True)]&limit=10"
        paginated = build_pagination_uri(base, offset=20, limit=10)

        parsed = parse_uri(paginated)
        assert parsed.offset == 20
        assert parsed.limit == 10
        assert parsed.domain == "[('is_company','=',True)]"

    def test_extract_model_from_uri(self):
        """Test extracting model name from URI."""
        assert extract_model_from_uri("odoo://res.partner/search") == "res.partner"
        assert extract_model_from_uri("odoo://sale.order/record/123") == "sale.order"
        assert (
            extract_model_from_uri("odoo://account.invoice.line/fields") == "account.invoice.line"
        )

    def test_extract_model_from_invalid_uri(self):
        """Test extracting model from invalid URI."""
        with pytest.raises(URIParseError):
            extract_model_from_uri("invalid://uri")


class TestURIRoundTrip:
    """Test round-trip conversion between URIs and parsed objects."""

    def test_roundtrip_simple(self):
        """Test simple URI round-trip."""
        original = "odoo://res.partner/search"
        parsed = parse_uri(original)
        rebuilt = parsed.to_uri()
        reparsed = parse_uri(rebuilt)

        assert reparsed.model == parsed.model
        assert reparsed.operation == parsed.operation

    def test_roundtrip_complex(self):
        """Test complex URI round-trip with all parameters."""
        original = build_uri(
            "sale.order",
            "search",
            domain="[('state','in',['sale','done'])]",
            fields=["name", "partner_id", "amount_total", "date_order"],
            limit=50,
            offset=100,
            order="date_order desc,name asc",
        )

        parsed = parse_uri(original)
        rebuilt = parsed.to_uri()
        reparsed = parse_uri(rebuilt)

        assert reparsed.model == parsed.model
        assert reparsed.operation == parsed.operation
        assert reparsed.domain == parsed.domain
        assert reparsed.fields == parsed.fields
        assert reparsed.limit == parsed.limit
        assert reparsed.offset == parsed.offset
        assert reparsed.order == parsed.order


class TestURIEdgeCases:
    """Test edge cases and special scenarios."""

    def test_model_with_underscores(self):
        """Test model names with underscores."""
        uri = "odoo://hr_employee/search"
        parsed = parse_uri(uri)
        assert parsed.model == "hr_employee"

    def test_model_with_multiple_dots(self):
        """Test model names with multiple dots."""
        uri = "odoo://account.bank.statement.line/search"
        parsed = parse_uri(uri)
        assert parsed.model == "account.bank.statement.line"

    def test_empty_domain(self):
        """Test URIs with empty domain parameter."""
        uri = "odoo://res.partner/search?domain="
        parsed = parse_uri(uri)
        assert parsed.domain == ""

    def test_fields_with_spaces(self):
        """Test fields parameter with spaces."""
        uri = "odoo://res.partner/search?fields=name, email , phone"
        parsed = parse_uri(uri)
        assert parsed.fields == ["name", "email", "phone"]

    def test_order_with_multiple_fields(self):
        """Test order parameter with multiple fields."""
        uri = "odoo://sale.order/search?order=date_order desc,partner_id asc,name"
        parsed = parse_uri(uri)
        assert parsed.order == "date_order desc,partner_id asc,name"

    def test_large_id_values(self):
        """Test URIs with large ID values."""
        uri = "odoo://res.partner/record/999999999"
        parsed = parse_uri(uri)
        assert parsed.record_id == 999999999
