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
        """One2many with an inverse field: view-all hint filters on it."""
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
        # A one2many is exactly the records whose inverse many2one points at
        # the parent — the hint filters on it instead of an id-in domain
        assert (
            "→ View all: use the search_records tool with model='res.partner', "
            'domain=[["parent_id", "=", 5]]' in result
        )
        assert "odoo://res.partner/search?" not in result

    def test_format_one2many_field_without_relation_field(self, formatter):
        """One2many lacking an inverse field falls back to the id-in domain."""
        record = {"id": 5, "name": "Parent", "child_ids": [1, 2, 3]}

        fields_metadata = {"child_ids": {"type": "one2many", "relation": "res.partner"}}

        result = formatter.format_record(record, fields_metadata)

        assert (
            "→ View all: use the search_records tool with model='res.partner', "
            'domain=[["id", "in", [1, 2, 3]]]' in result
        )

    def test_format_one2many_generic_res_id_inverse_keeps_id_in_domain(self, formatter):
        """A generic `res_id` inverse (mail.activity-style) needs a res_model
        constraint the formatter cannot know — the hint keeps the exact
        id-in domain instead of a bare res_id filter."""
        record = {"id": 5, "name": "Parent", "activity_ids": [1, 2, 3]}

        fields_metadata = {
            "activity_ids": {
                "type": "one2many",
                "relation": "mail.activity",
                "relation_field": "res_id",
            }
        }

        result = formatter.format_record(record, fields_metadata)

        assert (
            "→ View all: use the search_records tool with model='mail.activity', "
            'domain=[["id", "in", [1, 2, 3]]]' in result
        )
        assert '"res_id"' not in result

    def test_format_one2many_with_field_domain_keeps_id_in_domain(self, formatter):
        """A domain-restricted one2many (e.g. account.move.invoice_line_ids
        with domain excluding tax/payment-term lines) holds a SUBSET of the
        inverse-pointing records — an inverse-only hint would over-return, so
        the hint keeps the exact id-in domain. Any truthy `domain` (string
        form for dynamic domains included) disqualifies the inverse hint."""
        record = {"id": 5, "name": "INV/001", "invoice_line_ids": [1, 2, 3]}

        fields_metadata = {
            "invoice_line_ids": {
                "type": "one2many",
                "relation": "account.move.line",
                "relation_field": "move_id",
                "domain": "[('display_type', '=', False)]",
            }
        }

        result = formatter.format_record(record, fields_metadata)

        assert (
            "→ View all: use the search_records tool with model='account.move.line', "
            'domain=[["id", "in", [1, 2, 3]]]' in result
        )
        assert '"move_id"' not in result

    def test_format_one2many_field_without_parent_id(self, formatter):
        """One2many with inverse field but no usable parent id keeps id-in domain."""
        record = {"id": "Unknown", "name": "Parent", "child_ids": [1, 2]}

        fields_metadata = {
            "child_ids": {
                "type": "one2many",
                "relation": "res.partner",
                "relation_field": "parent_id",
            }
        }

        result = formatter.format_record(record, fields_metadata)

        assert 'domain=[["id", "in", [1, 2]]]' in result

    def test_format_many2many_field(self, formatter):
        """Many2many (no scalar inverse) keeps the id-in domain, capped at 50."""
        record = {"id": 6, "name": "Test", "tag_ids": [10, 20, 30]}

        fields_metadata = {"tag_ids": {"type": "many2many", "relation": "res.partner.category"}}

        result = formatter.format_record(record, fields_metadata)

        assert "tag_ids: 3 record(s)" in result
        assert (
            "use the search_records tool with model='res.partner.category', "
            'domain=[["id", "in", [10, 20, 30]]]' in result
        )
        assert "odoo://res.partner.category/search?" not in result

    def test_format_float_precision_from_list_digits(self, formatter):
        """XML-RPC delivers digits as a list — precision must still apply."""
        record = {"id": 8, "name": "Test", "qty": 0.0625}
        fields_metadata = {"qty": {"type": "float", "digits": [16, 5]}}

        result = formatter.format_record(record, fields_metadata)

        assert "qty: 0.06250" in result

    def test_format_unset_name_falls_back_to_record_id(self, formatter):
        """Odoo returns False for unset char fields — never print 'Name: False'."""
        record = {"id": 9, "name": False, "display_name": False}

        result = formatter.format_record(record, {})

        assert "Name: Record 9" in result
        assert "Name: False" not in result

    def test_format_long_text_truncated_with_marker(self, formatter):
        """Long text values are capped with an explicit truncation marker."""
        long_text = "x" * 5000
        record = {"id": 10, "name": "Test", "comment": long_text}
        fields_metadata = {"comment": {"type": "text"}}

        result = formatter.format_record(record, fields_metadata)

        assert "[truncated, 5000 chars total]" in result
        assert long_text not in result

    def test_format_without_metadata_truncates_blobs(self, formatter):
        """Metadata-less formatting must not dump huge raw values verbatim."""
        blob = "QUJD" * 2000  # base64-ish, 8000 chars
        record = {"id": 11, "name": "Test", "image_1920": blob}

        result = formatter.format_record(record, fields_metadata=None)

        assert "[truncated, 8000 chars total]" in result
        assert blob not in result

    def test_format_without_metadata_renders_many2one_shapes(self, formatter):
        """[id, name] pairs render readably even without field metadata."""
        record = {"id": 12, "name": "Test", "company_id": [5, "Agrolait"]}

        result = formatter.format_record(record, fields_metadata=None)

        assert "company_id: Agrolait (ID: 5)" in result

    def test_format_binary_field(self, formatter):
        """Populated binary fields render as a fetchable resource URI."""
        record = {"id": 7, "name": "Test", "image": b"fake_binary_data"}

        fields_metadata = {"image": {"type": "binary"}}

        result = formatter.format_record(record, fields_metadata)

        assert "image: odoo://res.partner/record/7/image" in result
        assert "[Binary data - use" not in result

    def test_format_binary_field_with_size_placeholder(self, formatter):
        """bin_size placeholders (short strings) are appended to the URI."""
        record = {"id": 7, "name": "Test", "image": "12.5 KB"}

        fields_metadata = {"image": {"type": "binary"}}

        result = formatter.format_record(record, fields_metadata)

        assert "image: odoo://res.partner/record/7/image (12.5 KB)" in result

    def test_format_binary_field_long_string_value_omits_blob(self, formatter):
        """A long string value (raw base64, not a short bin_size placeholder)
        is dropped — only the bare URI is rendered, never the blob."""
        blob = "A" * 200  # > MAX_BINARY_PLACEHOLDER_LENGTH (32)
        record = {"id": 7, "name": "Test", "image": blob}

        fields_metadata = {"image": {"type": "binary"}}

        result = formatter.format_record(record, fields_metadata)

        assert "image: odoo://res.partner/record/7/image" in result
        assert blob not in result
        assert "AAAA" not in result

    def test_format_binary_field_empty_is_not_set(self, formatter):
        """Empty binary fields render as 'Not set', not as a URI."""
        record = {"id": 7, "name": "Test", "image": False}

        fields_metadata = {"image": {"type": "binary"}}

        result = formatter.format_record(record, fields_metadata)

        assert "image: Not set" in result
        assert "odoo://res.partner/record/7/image" not in result

    def test_format_attachment_datas_uses_attachment_uri(self):
        """ir.attachment.datas renders as odoo://attachment/{id}; other
        binary fields on ir.attachment keep the record-field URI."""
        formatter = RecordFormatter("ir.attachment")
        record = {"id": 7, "name": "report.pdf", "datas": "12.5 KB", "thumbnail": "1 KB"}

        fields_metadata = {"datas": {"type": "binary"}, "thumbnail": {"type": "image"}}

        result = formatter.format_record(record, fields_metadata)

        assert "datas: odoo://attachment/7 (12.5 KB)" in result
        assert "thumbnail: odoo://ir.attachment/record/7/thumbnail (1 KB)" in result

    def test_private_binary_field_is_dropped_before_uri_building(self, formatter):
        """Odoo permits a leading underscore in a field name (core's barcode
        mixin ships `_barcode_scanned`) and the URI grammar rejects one, so
        this is the reason _format_field_value needs no URIValidationError
        guard: format_record drops private fields before the type dispatch.
        """
        record = {"id": 7, "name": "Test", "_scan": "12.5 KB", "image": "3 KB"}
        fields_metadata = {"_scan": {"type": "binary"}, "image": {"type": "binary"}}

        result = formatter.format_record(record, fields_metadata)

        assert "_scan" not in result
        assert "image: odoo://res.partner/record/7/image (3 KB)" in result

    def test_format_binary_field_without_record_id(self, formatter):
        """No usable record ID → generic marker, no broken URI."""
        record = {"name": "Test", "image": "12.5 KB"}

        fields_metadata = {"image": {"type": "binary"}}

        result = formatter.format_record(record, fields_metadata)

        assert "image: [Binary data]" in result

    def test_format_file_type_renders_placeholder_not_base64(self, formatter):
        """A non-core binary-like 'file' field (custom/OCA) renders a
        placeholder — no fetchable URI exists for it (the resource handler
        rejects the type), and the unknown-type branch would dump base64."""
        blob = "QUJD" * 1000  # 4000 chars of base64
        record = {"id": 7, "name": "Test", "attachment_file": blob}

        fields_metadata = {"attachment_file": {"type": "file"}}

        result = formatter.format_record(record, fields_metadata)

        assert "attachment_file: [Binary data]" in result
        assert "QUJD" not in result
        assert "odoo://res.partner/record/7/attachment_file" not in result

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


class TestFormatSimpleValue:
    """Test _format_simple_value boolean handling."""

    @pytest.fixture
    def formatter(self):
        return DatasetFormatter("res.partner")

    def test_format_simple_value_boolean_false(self, formatter):
        """Test that False renders as 'No', not 'Not set'."""
        result = formatter._format_simple_value(False)
        assert result == "No"

    def test_format_simple_value_boolean_true(self, formatter):
        """Test that True renders as 'Yes'."""
        result = formatter._format_simple_value(True)
        assert result == "Yes"

    def test_format_simple_value_none(self, formatter):
        """Test that None renders as 'Not set'."""
        result = formatter._format_simple_value(None)
        assert result == "Not set"


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
            prev_hint="use the search_records tool with offset=0, limit=10",
            next_hint="use the search_records tool with offset=20, limit=10",
        )

        assert "Page 2 of 3" in result
        assert "Showing records 11-20 of 30" in result
        assert "[11] Record 11" in result
        assert "[20] Record 20" in result
        assert "← Previous page: use the search_records tool with offset=0, limit=10" in result
        assert "→ Next page: use the search_records tool with offset=20, limit=10" in result

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

            assert partner_ids, "expected at least one res.partner record"

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
            next_hint = "use the search_records tool with offset=5, limit=5" if total > 5 else None

            result = formatter.format_search_results(
                records,
                domain=domain,
                fields=["name", "email", "phone", "country_id"],
                limit=5,
                offset=0,
                total_count=total,
                current_page=current_page,
                total_pages=total_pages,
                next_hint=next_hint,
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
        created_ids = []

        try:
            connection.connect()
            connection.authenticate()

            # Create a parent/child pair so both relationship directions are
            # guaranteed to exist regardless of database contents. res.company
            # is used because the test instance grants it full CRUD (res.partner
            # is read/write only) and it has the same parent_id/child_ids pair.
            parent_id = connection.create("res.company", {"name": "Formatting Test Parent Co"})
            created_ids.append(parent_id)
            child_id = connection.create(
                "res.company", {"name": "Formatting Test Child Co", "parent_id": parent_id}
            )
            created_ids.append(child_id)

            fields_meta = connection.fields_get("res.company")
            fields = ["name", "child_ids", "parent_id"]
            parent_rec = connection.read("res.company", [parent_id], fields)[0]
            child_rec = connection.read("res.company", [child_id], fields)[0]
            assert parent_rec["child_ids"] == [child_id]
            assert child_rec["parent_id"][0] == parent_id

            formatter = RecordFormatter("res.company")

            # one2many: parent record links to its children
            result = formatter.format_record(parent_rec, fields_meta)
            assert "Relationships:" in result
            assert "child_ids: 1 record(s)" in result
            assert "use the search_records tool with model='res.company'" in result

            # many2one: child record links back to the parent
            result = formatter.format_record(child_rec, fields_meta)
            assert "Relationships:" in result
            assert "parent_id:" in result
            assert f"(odoo://res.company/record/{parent_id})" in result

        finally:
            if created_ids:
                try:
                    # children first — Odoo blocks deleting a company with children
                    connection.unlink("res.company", list(reversed(created_ids)))
                except Exception:
                    pass
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

            assert product_ids, f"expected at least one {model} record"

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

            # Boolean: 'active' exists on both models and is always returned
            assert "active" in record
            if record["active"]:
                assert "active: Yes" in result
            else:
                assert "active: No" in result

            # Many2one: categ_id is required on product.product, so the check
            # is guaranteed to run there; on the res.partner fallback country_id
            # may legitimately be unset
            m2o_fields = [
                k
                for k, v in fields_meta.items()
                if v.get("type") == "many2one" and k in record and record[k]
            ]
            if model == "product.product":
                assert m2o_fields, "categ_id is required on product.product"
            if m2o_fields:
                field = m2o_fields[0]
                assert f"{field}:" in result
                assert "odoo://" in result

            # Note: date/datetime formatting is not checked here — the only date
            # fields requested (create_date/write_date) are in OMIT_FIELDS and
            # never rendered; unit tests in test_datetime_formatting.py cover it

        finally:
            connection.disconnect()


class TestBinaryTypedNonPayloadValues:
    """Odoo declares several non-stored widget fields as fields.Binary while
    returning a dict (sale.order.tax_totals, account.move.tax_totals /
    invoice_payments_widget / needed_terms / payment_term_details). bin_size
    does not apply to them, so the formatter must not hand out a URI that
    fails on read — mirroring tools._replace_binary_values.
    """

    @pytest.fixture
    def formatter(self):
        return RecordFormatter("sale.order")

    def test_dict_valued_binary_renders_value_not_uri(self, formatter):
        record = {"id": 5, "name": "S0005", "tax_totals": {"amount_total": 6152.5}}

        result = formatter.format_record(record, {"tax_totals": {"type": "binary"}})

        assert "odoo://sale.order/record/5/tax_totals" not in result, (
            "a dict payload must not be advertised as a fetchable binary"
        )
        assert "amount_total" in result

    def test_list_valued_binary_renders_value_not_uri(self, formatter):
        record = {"id": 5, "name": "S0005", "widget": [1, 2, 3]}

        result = formatter.format_record(record, {"widget": {"type": "binary"}})

        assert "odoo://sale.order/record/5/widget" not in result

    @pytest.mark.parametrize(
        "payload", [b"fake_binary_data", bytearray(b"raw"), "12.5 KB", "A" * 200]
    )
    def test_real_payloads_still_get_a_uri(self, formatter, payload):
        """str / bytes / bytearray are all servable by the binary resource."""
        record = {"id": 5, "name": "S0005", "image": payload}

        result = formatter.format_record(record, {"image": {"type": "binary"}})

        assert "image: odoo://sale.order/record/5/image" in result

    def test_xmlrpc_binary_still_gets_a_uri(self, formatter):
        import xmlrpc.client

        record = {"id": 5, "name": "S0005", "image": xmlrpc.client.Binary(b"bytes")}

        result = formatter.format_record(record, {"image": {"type": "binary"}})

        assert "image: odoo://sale.order/record/5/image" in result
