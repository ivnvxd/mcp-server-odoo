"""Tests for data formatting system."""

from datetime import date, datetime

import pytest

from mcp_server_odoo.config import get_config
from mcp_server_odoo.formatters import DatasetFormatter, RecordFormatter
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


class TestRecordFormatter:
    """Test RecordFormatter functionality."""

    @pytest.fixture
    def formatter(self):
        """Create a RecordFormatter instance."""
        return RecordFormatter("res.partner")

    def test_format_simple_record(self, formatter):
        """Test formatting a simple record."""
        record = {
            "id": 1,
            "name": "Test Company",
            "display_name": "Test Company",
            "email": "test@example.com",
            "phone": "+1234567890",
            "is_company": True,
            "active": True,
        }

        result = formatter.format_record(record)

        assert "Record: res.partner/1" in result
        assert "Name: Test Company" in result
        assert "email: test@example.com" in result
        assert "phone: +1234567890" in result
        assert "is_company: True" in result
        assert "active: True" in result  # Without metadata, boolean shows as True

    def test_format_record_with_metadata(self, formatter):
        """Test formatting with field metadata."""
        record = {
            "id": 2,
            "name": "Test User",
            "credit_limit": 5000.0,
            "user_id": False,
            "date": "2024-01-15",
            "state": "confirmed",
        }

        fields_metadata = {
            "credit_limit": {"type": "monetary"},
            "user_id": {"type": "many2one", "relation": "res.users"},
            "date": {"type": "date"},
            "state": {
                "type": "selection",
                "selection": [("draft", "Draft"), ("confirmed", "Confirmed"), ("done", "Done")],
            },
        }

        result = formatter.format_record(record, fields_metadata)

        assert "credit_limit: 5,000.00" in result  # Monetary formatting
        assert "user_id: Not set" in result
        assert "date: 2024-01-15" in result
        assert "state: Confirmed (confirmed)" in result  # Selection formatting

    def test_format_numeric_fields(self, formatter):
        """Test formatting of numeric fields."""
        record = {
            "id": 3,
            "name": "Test",
            "int_field": 12345,
            "float_field": 3.14159,
            "monetary_field": 9999.99,
        }

        fields_metadata = {
            "int_field": {"type": "integer"},
            "float_field": {"type": "float", "digits": (16, 4)},
            "monetary_field": {"type": "monetary"},
        }

        result = formatter.format_record(record, fields_metadata)

        assert "int_field: 12,345" in result  # Integer with thousand separator
        assert "float_field: 3.1416" in result  # Float with specified precision
        assert "monetary_field: 9,999.99" in result  # Monetary formatting

    def test_format_many2one_field(self, formatter):
        """Test formatting of many2one fields."""
        record = {
            "id": 4,
            "name": "Test",
            "partner_id": (10, "Parent Company"),
            "country_id": False,
        }

        fields_metadata = {
            "partner_id": {"type": "many2one", "relation": "res.partner"},
            "country_id": {"type": "many2one", "relation": "res.country"},
        }

        result = formatter.format_record(record, fields_metadata)

        assert "Relationships:" in result
        assert "partner_id: Parent Company (odoo://res.partner/record/10)" in result
        assert "country_id: Not set" in result

    def test_format_one2many_field(self, formatter):
        """Test formatting of one2many fields."""
        record = {"id": 5, "name": "Parent", "child_ids": [1, 2, 3, 4, 5]}

        fields_metadata = {
            "child_ids": {
                "type": "one2many",
                "relation": "res.partner",
                "relation_field": "parent_id",
            }
        }

        result = formatter.format_record(record, fields_metadata)

        assert "Relationships:" in result
        assert "child_ids: 5 record(s)" in result
        assert "→ View all: odoo://res.partner/search?" in result

    def test_format_many2many_field(self, formatter):
        """Test formatting of many2many fields."""
        record = {"id": 6, "name": "Test", "tag_ids": [10, 20, 30]}

        fields_metadata = {"tag_ids": {"type": "many2many", "relation": "res.partner.category"}}

        result = formatter.format_record(record, fields_metadata)

        assert "tag_ids: 3 record(s)" in result
        assert "odoo://res.partner.category/search?domain" in result

    def test_format_binary_field(self, formatter):
        """Test formatting of binary fields."""
        record = {"id": 7, "name": "Test", "image": b"fake_binary_data"}

        fields_metadata = {"image": {"type": "binary"}}

        result = formatter.format_record(record, fields_metadata)

        assert "[Binary data - use res.partner/image to retrieve]" in result

    def test_omit_internal_fields(self, formatter):
        """Test that internal fields are omitted."""
        record = {
            "id": 8,
            "name": "Test",
            "email": "test@example.com",
            "__last_update": "2024-01-01 00:00:00",
            "write_date": "2024-01-01 00:00:00",
            "create_uid": (1, "Admin"),
            "_prefetch_field": "internal",
        }

        result = formatter.format_record(record)

        assert "__last_update" not in result
        assert "write_date" not in result
        assert "create_uid" not in result
        assert "_prefetch_field" not in result
        assert "email: test@example.com" in result

    def test_format_list(self, formatter):
        """Test formatting a list of records."""
        records = [
            {"id": 1, "name": "Company A", "display_name": "Company A"},
            {"id": 2, "name": "Company B", "display_name": "Company B"},
            {"id": 3, "name": "Company C", "display_name": "Company C"},
        ]

        result = formatter.format_list(records)

        assert "res.partner Records (3 found)" in result
        assert "[1] Company A" in result
        assert "[2] Company B" in result
        assert "[3] Company C" in result

    def test_format_empty_list(self, formatter):
        """Test formatting an empty list."""
        result = formatter.format_list([])

        assert "No res.partner records found." in result

    def test_format_datetime_field(self, formatter):
        """Test formatting of datetime fields."""
        record = {
            "id": 9,
            "name": "Test",
            "date_field": "2024-01-15",
            "datetime_field": "2024-01-15 14:30:00",
            "datetime_compact": "20240115T14:30:00",  # Odoo compact format
            "date_obj": date(2024, 1, 15),
            "datetime_obj": datetime(2024, 1, 15, 14, 30),
        }

        fields_metadata = {
            "date_field": {"type": "date"},
            "datetime_field": {"type": "datetime"},
            "datetime_compact": {"type": "datetime"},
            "date_obj": {"type": "date"},
            "datetime_obj": {"type": "datetime"},
        }

        result = formatter.format_record(record, fields_metadata)

        assert "date_field: 2024-01-15" in result
        assert "datetime_field: 2024-01-15T14:30:00+00:00" in result
        assert "datetime_compact: 2024-01-15T14:30:00+00:00" in result
        assert "date_obj: 2024-01-15" in result
        assert "datetime_obj: 2024-01-15T14:30:00+00:00" in result


class TestDatasetFormatter:
    """Test DatasetFormatter functionality."""

    @pytest.fixture
    def formatter(self):
        """Create a DatasetFormatter instance."""
        return DatasetFormatter("res.partner")

    def test_format_search_results(self, formatter):
        """Test formatting search results."""
        records = [
            {"id": 1, "name": "Company A", "email": "a@example.com"},
            {"id": 2, "name": "Company B", "email": "b@example.com"},
        ]

        result = formatter.format_search_results(
            records,
            domain=[("is_company", "=", True)],
            fields=["name", "email"],
            limit=10,
            offset=0,
            total_count=50,
        )

        assert "Search Results: res.partner" in result
        assert "Search criteria: is_company = True" in result
        assert "Showing records 1-2 of 50" in result
        assert "Fields: name, email" in result
        assert "[1] Company A" in result
        assert "email: a@example.com" in result

    def test_format_empty_search_results(self, formatter):
        """Test formatting empty search results."""
        result = formatter.format_search_results(
            [], domain=[("name", "ilike", "nonexistent")], total_count=0
        )

        assert "No records found matching the criteria." in result
        assert "Search criteria: name ilike nonexistent" in result

    def test_format_search_with_pagination(self, formatter):
        """Test formatting with pagination info."""
        records = [{"id": i, "name": f"Record {i}"} for i in range(11, 21)]

        result = formatter.format_search_results(
            records,
            limit=10,
            offset=10,
            total_count=30,
            current_page=2,
            total_pages=3,
            prev_uri="odoo://res.partner/search?limit=10&offset=0",
            next_uri="odoo://res.partner/search?limit=10&offset=20",
        )

        assert "Page 2 of 3" in result
        assert "Showing records 11-20 of 30" in result
        assert "[11] Record 11" in result
        assert "[20] Record 20" in result
        assert "← Previous page: odoo://res.partner/search?limit=10&offset=0" in result
        assert "→ Next page: odoo://res.partner/search?limit=10&offset=20" in result

    def test_format_complex_domain(self, formatter):
        """Test formatting complex search domains."""
        domain = [
            "|",
            ("is_company", "=", True),
            "&",
            ("customer_rank", ">", 0),
            ("active", "=", True),
        ]

        records = [{"id": 1, "name": "Test"}]
        result = formatter.format_search_results(records, domain=domain)

        assert "| is_company = True & customer_rank > 0 active = True" in result

    def test_format_search_with_selected_fields(self, formatter):
        """Test formatting with specific fields shown inline."""
        records = [
            {
                "id": 1,
                "name": "Test Company",
                "email": "test@example.com",
                "phone": "123-456-7890",
                "is_company": True,
            }
        ]

        result = formatter.format_search_results(records, fields=["email", "phone", "is_company"])

        assert "[1] Test Company" in result
        assert "    email: test@example.com" in result
        assert "    phone: 123-456-7890" in result
        assert "    is_company: Yes" in result


class TestFormattingIntegration:
    """Integration tests with real Odoo data."""

    @pytest.mark.mcp
    def test_format_real_partner_record(self):
        """Test formatting real partner records from Odoo."""
        config = get_config()
        connection = OdooConnection(config)

        try:
            connection.connect()
            connection.authenticate()

            # Get a partner record with fields metadata
            try:
                partner_ids = connection.search("res.partner", [], limit=1)
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e):
                    pytest.skip("Rate limited by server")
                raise

            if partner_ids:
                # Get fields metadata
                fields_meta = connection.fields_get("res.partner")

                # Read the record with specific fields to avoid marshaling issues
                records = connection.read(
                    "res.partner",
                    partner_ids,
                    [
                        "name",
                        "email",
                        "phone",
                        "street",
                        "city",
                        "country_id",
                        "is_company",
                        "child_ids",
                        "parent_id",
                    ],
                )

                # Format the record
                formatter = RecordFormatter("res.partner")
                result = formatter.format_record(records[0], fields_meta)

                # Basic assertions
                assert f"Record: res.partner/{partner_ids[0]}" in result
                assert "Fields:" in result or "Relationships:" in result
                assert "=" * 50 in result

        finally:
            connection.disconnect()

    @pytest.mark.mcp
    def test_format_real_search_results(self):
        """Test formatting real search results from Odoo."""
        config = get_config()
        connection = OdooConnection(config)

        try:
            connection.connect()
            connection.authenticate()

            # Search for companies
            domain = [("is_company", "=", True)]
            records = connection.search_read(
                "res.partner", domain, fields=["name", "email", "phone", "country_id"], limit=5
            )

            # Get total count
            total = connection.search_count("res.partner", domain)

            # Format the results
            formatter = DatasetFormatter("res.partner")
            # Calculate pagination info
            current_page = 1
            total_pages = (total + 4) // 5 if total > 0 else 1
            next_uri = "odoo://res.partner/search?limit=5&offset=5" if total > 5 else None

            result = formatter.format_search_results(
                records,
                domain=domain,
                fields=["name", "email", "phone", "country_id"],
                limit=5,
                offset=0,
                total_count=total,
                current_page=current_page,
                total_pages=total_pages,
                next_uri=next_uri,
            )

            # Basic assertions
            assert "Search Results: res.partner" in result
            assert "is_company = True" in result
            assert f"of {total}" in result

            # Check for specific fields if records exist
            if records:
                assert "[1]" in result
                if "email" in records[0] and records[0]["email"]:
                    assert "email:" in result

        finally:
            connection.disconnect()

    @pytest.mark.mcp
    def test_format_record_with_relationships(self):
        """Test formatting records with relationship fields."""
        config = get_config()
        connection = OdooConnection(config)

        try:
            connection.connect()
            connection.authenticate()

            # Find a partner with relationships
            domain = ["|", ("child_ids", "!=", False), ("parent_id", "!=", False)]
            partner_ids = connection.search("res.partner", domain, limit=1)

            if partner_ids:
                # Get fields metadata
                fields_meta = connection.fields_get("res.partner")

                # Read the record with specific fields to avoid marshaling issues
                records = connection.read(
                    "res.partner",
                    partner_ids,
                    [
                        "name",
                        "email",
                        "phone",
                        "street",
                        "city",
                        "country_id",
                        "is_company",
                        "child_ids",
                        "parent_id",
                    ],
                )

                # Format the record
                formatter = RecordFormatter("res.partner")
                result = formatter.format_record(records[0], fields_meta)

                # Check for relationships section
                if "parent_id" in records[0] and records[0]["parent_id"]:
                    assert "Relationships:" in result
                    assert "parent_id:" in result
                    assert "odoo://res.partner/record/" in result

                if "child_ids" in records[0] and records[0]["child_ids"]:
                    assert "child_ids:" in result
                    assert "record(s)" in result
                    assert "odoo://res.partner/search?" in result

        finally:
            connection.disconnect()

    @pytest.mark.mcp
    def test_format_various_field_types(self):
        """Test formatting various Odoo field types."""
        config = get_config()
        connection = OdooConnection(config)

        try:
            connection.connect()
            try:
                connection.authenticate()
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

            # Get a product record (has various field types and is usually enabled)
            try:
                product_ids = connection.search("product.product", [], limit=1)
                model = "product.product"
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e):
                    pytest.skip("Rate limited by server")
                # Fallback to res.partner which we know is enabled
                try:
                    product_ids = connection.search("res.partner", [], limit=1)
                    model = "res.partner"
                except OdooConnectionError as e:
                    if "429" in str(e) or "Too many requests" in str(e):
                        pytest.skip("Rate limited by server")
                    raise

            if product_ids:
                # Get fields metadata
                fields_meta = connection.fields_get(model)

                # Read the record with limited fields to avoid marshaling issues
                # Select fields that are likely to exist in both product and partner models
                basic_fields = ["name", "active", "create_date", "write_date"]
                if model == "res.partner":
                    basic_fields.extend(["email", "phone", "is_company", "country_id"])
                else:  # product.product
                    basic_fields.extend(["list_price", "standard_price", "type", "categ_id"])

                records = connection.read(model, product_ids, basic_fields)

                # Format the record
                formatter = RecordFormatter(model)
                result = formatter.format_record(records[0], fields_meta)

                # Check basic structure
                assert f"Record: {model}/{product_ids[0]}" in result
                assert "Fields:" in result or "Relationships:" in result

                # Check for different field types based on what's in the record
                record = records[0]

                # Check for boolean fields
                bool_fields = [
                    k for k, v in fields_meta.items() if v.get("type") == "boolean" and k in record
                ]
                if bool_fields:
                    field = bool_fields[0]
                    if record[field]:
                        assert f"{field}: Yes" in result
                    else:
                        assert f"{field}: No" in result

                # Check for many2one fields
                m2o_fields = [
                    k
                    for k, v in fields_meta.items()
                    if v.get("type") == "many2one" and k in record and record[k]
                ]
                if m2o_fields:
                    field = m2o_fields[0]
                    assert f"{field}:" in result
                    assert "odoo://" in result

                # Check for date/datetime fields (excluding create_date which is omitted)
                date_fields = [
                    k
                    for k, v in fields_meta.items()
                    if v.get("type") in ("date", "datetime")
                    and k in record
                    and record[k]
                    and k not in RecordFormatter.OMIT_FIELDS
                ]
                if date_fields:
                    field = date_fields[0]
                    assert f"{field}:" in result

        finally:
            connection.disconnect()
