"""Tests for tools functionality in YOLO mode.

This module tests the tool handlers behavior in YOLO modes.
"""

import os
from unittest.mock import MagicMock

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.tools import OdooToolHandler


class TestYoloModeTools:
    """Test tools in YOLO mode."""

    @pytest.fixture
    def config_yolo_read(self):
        """Create configuration for read-only YOLO mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="read",
        )

    @pytest.fixture
    def config_yolo_full(self):
        """Create configuration for full access YOLO mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )

    @pytest.fixture
    def config_standard(self):
        """Create configuration for standard mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="test_api_key",
            database=os.getenv("ODOO_DB"),
            yolo_mode="off",
        )

    @pytest.fixture
    def mock_connection(self):
        """Create mock OdooConnection."""
        mock = MagicMock()
        mock.is_authenticated = True
        mock.search_read = MagicMock()
        return mock

    @pytest.fixture
    def mock_access_controller(self):
        """Create mock AccessController."""
        mock = MagicMock()
        mock.get_enabled_models = MagicMock()
        mock.get_model_permissions = MagicMock()
        return mock

    @pytest.fixture
    def mock_app(self):
        """Create mock MCPServer app."""
        mock = MagicMock()
        return mock

    @pytest.mark.asyncio
    async def test_list_models_yolo_read_mode(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models in read-only YOLO mode."""
        # Setup mock data
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "product.product", "name": "Product"},
            {"model": "sale.order", "name": "Sales Order"},
        ]

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Verify connection was called to query models
        mock_connection.search_read.assert_called_once()
        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"  # Model name
        assert "transient" in str(call_args[0][1])  # Domain includes transient filter

        # Check result structure and values
        assert result["total"] == 3
        assert len(result["models"]) == 3

        # Verify model names from the mock actually appear in the result
        model_names = [m["model"] for m in result["models"]]
        assert "res.partner" in model_names
        assert "product.product" in model_names
        assert "sale.order" in model_names

        # Check YOLO mode metadata
        yolo_meta = result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "read"
        assert "READ-ONLY" in yolo_meta["description"]
        assert "🚨" in yolo_meta["warning"]
        assert yolo_meta["operations"]["read"] is True
        assert yolo_meta["operations"]["write"] is False
        assert yolo_meta["operations"]["create"] is False
        assert yolo_meta["operations"]["unlink"] is False

        # Verify search_read was called with correct domain and the
        # context-flood cap
        from mcp_server_odoo.tools import MAX_LISTED_MODELS

        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"
        assert ("transient", "=", False) in call_args[0][1]
        assert call_args.kwargs["limit"] == MAX_LISTED_MODELS

        # Rows carry no per-model operations: the flags are global and
        # reported once under yolo_mode.operations, so repeating them on
        # every row only inflated the response.
        for model in result["models"]:
            assert "operations" not in model
        assert result["yolo_mode"]["operations"]["read"] is True

    @pytest.mark.asyncio
    async def test_list_models_yolo_full_mode(
        self, config_yolo_full, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models in full access YOLO mode."""
        # Setup mock data
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "account.move", "name": "Journal Entry"},
        ]

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_full
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Check result structure and values
        assert result["total"] == 2
        assert len(result["models"]) == 2

        # Verify model names from the mock actually appear in the result
        model_names = [m["model"] for m in result["models"]]
        assert "res.partner" in model_names
        assert "account.move" in model_names

        # Check YOLO mode metadata
        yolo_meta = result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "true"
        assert "FULL ACCESS" in yolo_meta["description"]
        assert "🚨" in yolo_meta["warning"]
        assert yolo_meta["operations"]["read"] is True
        assert yolo_meta["operations"]["write"] is True
        assert yolo_meta["operations"]["create"] is True
        assert yolo_meta["operations"]["unlink"] is True

        # Verify search_read was called with correct domain and fields
        mock_connection.search_read.assert_called_once()
        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"

        # Rows carry no per-model operations: the flags are global and
        # reported once under yolo_mode.operations, so repeating them on
        # every row only inflated the response.
        for model in result["models"]:
            assert "operations" not in model
        assert result["yolo_mode"]["operations"]["read"] is True

    @pytest.mark.asyncio
    async def test_list_models_yolo_under_cap_no_truncation(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """Under MAX_LISTED_MODELS rows: total is the page length, no note,
        no extra search_count round trip (the former 200 cap stays gone)."""
        from mcp_server_odoo.tools import MAX_LISTED_MODELS

        mock_connection.search_read.return_value = [
            {"model": f"x.model.{i}", "name": f"Model {i}"} for i in range(250)
        ]

        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )
        result = await handler._handle_list_models_tool()

        # The cap travels as the search_read limit kwarg
        call_args = mock_connection.search_read.call_args
        assert call_args.kwargs["limit"] == MAX_LISTED_MODELS

        # Short page → the page length IS the total; no count query, no note
        mock_connection.search_count.assert_not_called()
        assert result["total"] == 250
        assert result["total_available"] == 250
        assert len(result["models"]) == 250
        assert result["note"] is None

    @pytest.mark.asyncio
    async def test_list_models_yolo_truncated_with_note_and_true_total(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """A full page (Studio-heavy DB with 500+ models) triggers a
        search_count on the same domain; the result carries the real total
        and an explicit truncation note."""
        from mcp_server_odoo.tools import MAX_LISTED_MODELS

        # Server applies the limit: exactly MAX_LISTED_MODELS rows come back
        mock_connection.search_read.return_value = [
            {"model": f"x.model.{i}", "name": f"Model {i}"} for i in range(MAX_LISTED_MODELS)
        ]
        mock_connection.search_count.return_value = 731

        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )
        result = await handler._handle_list_models_tool()

        assert mock_connection.search_read.call_args.kwargs["limit"] == MAX_LISTED_MODELS
        # True total from search_count on the SAME domain as the listing
        mock_connection.search_count.assert_called_once()
        count_args = mock_connection.search_count.call_args[0]
        assert count_args[0] == "ir.model"
        assert count_args[1] == mock_connection.search_read.call_args[0][1]

        assert len(result["models"]) == MAX_LISTED_MODELS
        # total counts the page; total_available carries the real DB count
        assert result["total"] == MAX_LISTED_MODELS
        assert result["total_available"] == 731
        assert f"truncated to {MAX_LISTED_MODELS} of 731" in result["note"]
        assert "ir.model" in result["note"]

    @pytest.mark.asyncio
    async def test_list_models_standard_mode(
        self, config_standard, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models in standard mode uses MCP access controller."""
        # Setup mock data
        mock_access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "res.users", "name": "Users"},
        ]

        def mock_get_permissions(model):
            mock_perm = MagicMock()
            mock_perm.can_read = True
            mock_perm.can_write = True
            mock_perm.can_create = False
            mock_perm.can_unlink = False
            return mock_perm

        mock_access_controller.get_model_permissions.side_effect = mock_get_permissions

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_standard
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Verify connection was NOT called (standard mode uses access controller)
        mock_connection.search_read.assert_not_called()

        # Verify access controller was called
        mock_access_controller.get_enabled_models.assert_called_once()

        # Verify result contains the models with correct permissions
        models = result["models"]
        assert len(models) == 2
        for model in models:
            assert "operations" in model
            assert model["operations"]["read"] is True
            assert model["operations"]["write"] is True
            assert model["operations"]["create"] is False
            assert model["operations"]["unlink"] is False

    @pytest.mark.asyncio
    async def test_list_models_yolo_error_handling(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """Test error handling in YOLO mode model listing."""
        # Setup connection to raise error
        mock_connection.search_read.side_effect = Exception("Database connection failed")

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Check error response structure
        assert "yolo_mode" in result
        assert "models" in result
        assert "error" in result

        # Check YOLO mode metadata in error case
        yolo_meta = result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "read"
        assert "Error querying models" in yolo_meta["warning"]
        assert yolo_meta["operations"]["read"] is False
        assert yolo_meta["operations"]["write"] is False

        # Models should be empty on error
        assert result["models"] == []
        assert result["total"] == 0
        assert "Database connection failed" in result["error"]

    @pytest.mark.asyncio
    async def test_list_models_yolo_domain_construction(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """Test that domain is properly constructed in YOLO mode."""
        mock_connection.search_read.return_value = []

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method and verify empty result is handled
        result = await handler._handle_list_models_tool()
        assert result["models"] == []
        assert result["total"] == 0

        # Verify the domain passed to search_read
        call_args = mock_connection.search_read.call_args
        domain = call_args[0][1]

        # Check domain structure — verify the actual Polish-notation domain
        assert isinstance(domain, list)
        assert domain[0] == "&", "Domain should start with AND operator"
        assert ("transient", "=", False) in domain
        # Should have OR conditions for model filtering
        assert "|" in domain, "Domain should include OR conditions for model filtering"
        # '=like' is prefix-anchored; plain 'like' would match a SUBSTRING.
        # Negation uses the '!' prefix operator, NOT 'not =like' — see the
        # portability assertion below.
        assert ("model", "=like", "ir.%") in domain
        assert ("model", "=like", "base.%") in domain
        assert domain.count("!") == 2, "both prefix exclusions must be negated"

        # Every operator must exist on the oldest Odoo this server supports.
        # 'not =like' is Odoo 19 ONLY (odoo/orm/domains.py); on 15-18 an
        # unknown operator raises ValueError server-side, which would make
        # list_models — the primary YOLO discovery tool — return nothing.
        portable_ops = {"=", "!=", "in", "not in", "like", "not like", "=like", "ilike"}
        for term in domain:
            if isinstance(term, tuple):
                assert term[1] in portable_ops, (
                    f"operator {term[1]!r} is not available on all supported Odoo versions"
                )
            else:
                assert term in ("&", "|", "!"), f"unexpected domain operator {term!r}"
        # Should include whitelist of allowed ir.* models
        ir_whitelist = [
            c for c in domain if isinstance(c, tuple) and c[0] == "model" and c[1] == "in"
        ]
        assert len(ir_whitelist) == 1, "Should have exactly one 'model in [...]' whitelist"
        assert "ir.attachment" in ir_whitelist[0][2]

        # Evaluate the prefix-notation domain to prove it actually filters
        # (the previous OR-of-two-not-likes collapsed to transient=False).
        def evaluate(dom, record):
            def leaf(term):
                field, op, value = term
                actual = record[field]
                if op == "=":
                    return actual == value
                if op == "=like":
                    # '=like' does not wrap the pattern in %...%, so a
                    # trailing % is a prefix anchor and nothing else.
                    return actual.startswith(value.rstrip("%"))
                if op == "in":
                    return actual in value
                raise AssertionError(f"unexpected operator {op}")

            def consume(i):
                token = dom[i]
                if token == "&":
                    left, i = consume(i + 1)
                    right, i = consume(i)
                    return left and right, i
                if token == "|":
                    left, i = consume(i + 1)
                    right, i = consume(i)
                    return left or right, i
                if token == "!":
                    operand, i = consume(i + 1)
                    return (not operand), i
                return leaf(token), i + 1

            result, end = consume(0)
            assert end == len(dom), "domain has dangling terms"
            return result

        assert evaluate(domain, {"model": "res.partner", "transient": False})
        assert evaluate(domain, {"model": "ir.attachment", "transient": False})
        assert not evaluate(domain, {"model": "ir.cron", "transient": False})
        assert not evaluate(domain, {"model": "base.language.export", "transient": False})
        # Models that merely CONTAIN 'ir.' or 'base.' must survive — plain
        # 'like' matched them as substrings and silently hid them from
        # list_models (and from the search_count that reports the total).
        assert evaluate(domain, {"model": "repair.order", "transient": False})
        assert evaluate(domain, {"model": "properties.base.definition", "transient": False})
        assert not evaluate(domain, {"model": "res.partner", "transient": True})

    @pytest.mark.asyncio
    async def test_yolo_mode_logging(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app, caplog
    ):
        """Test that appropriate logging occurs in YOLO mode."""
        import logging

        # Set logging level to capture INFO messages
        caplog.set_level(logging.INFO)

        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
        ]

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method and verify result
        result = await handler._handle_list_models_tool()
        assert result["total"] == 1
        assert len(result["models"]) == 1
        assert result["models"][0]["model"] == "res.partner"

        # Check logs
        assert "YOLO mode (READ-ONLY)" in caplog.text
        assert "Listed 1 models from database" in caplog.text


@pytest.mark.yolo
class TestGetFieldsYoloIntegration:
    """Live-Odoo integration tests for the get_fields tool (vanilla XML-RPC)."""

    @pytest.mark.asyncio
    async def test_get_fields_returns_requested_fields_with_types(self):
        from mcp_server_odoo.access_control import AccessController
        from mcp_server_odoo.odoo_connection import OdooConnection

        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="read",
        )
        with OdooConnection(config) as connection:
            connection.authenticate()
            handler = OdooToolHandler(MagicMock(), connection, AccessController(config), config)

            result = await handler._handle_get_fields_tool("res.partner", ["name", "email"], None)

        assert result.model == "res.partner"
        assert result.total == 2
        types = {f.name: f.type for f in result.fields}
        assert types == {"email": "char", "name": "char"}


@pytest.mark.yolo
class TestRelatedSummariesYoloIntegration:
    """Live-Odoo integration test for x2many summaries on get_record."""

    @pytest.mark.asyncio
    async def test_get_record_returns_child_contact_names(self):
        from mcp_server_odoo.access_control import AccessController
        from mcp_server_odoo.odoo_connection import OdooConnection

        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )
        connection = OdooConnection(config)
        connection.connect()
        connection.authenticate()

        # Track every created id so a partial setup failure still cleans up
        created_ids = []
        try:
            parent_id = connection.create("res.partner", {"name": "Related Summary Parent"})
            created_ids.append(parent_id)
            child_ids = []
            for i in (1, 2):
                child_id = connection.create(
                    "res.partner", {"name": f"Related Summary Child {i}", "parent_id": parent_id}
                )
                created_ids.append(child_id)
                child_ids.append(child_id)

            handler = OdooToolHandler(MagicMock(), connection, AccessController(config), config)

            result = await handler._handle_get_record_tool(
                "res.partner", parent_id, ["name", "child_ids"]
            )

            assert sorted(result.record["child_ids"]) == sorted(child_ids)
            summaries = result.related_summaries["child_ids"]
            assert {s.id for s in summaries} == set(child_ids)
            # A child contact's display_name may be prefixed with the parent
            # name ("Parent, Child") — check containment, not equality.
            joined = " | ".join(s.display_name for s in summaries)
            assert "Related Summary Child 1" in joined
            assert "Related Summary Child 2" in joined
        finally:
            if created_ids:
                # Children first (reverse creation order)
                connection.unlink("res.partner", list(reversed(created_ids)))
            connection.disconnect()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


@pytest.mark.yolo
class TestCreateRecordsYoloIntegration:
    """Live-Odoo integration test for create_records (one create(vals_list) call)."""

    @pytest.mark.asyncio
    async def test_creates_two_partners_in_one_call(self):
        from mcp_server_odoo.access_control import AccessController
        from mcp_server_odoo.odoo_connection import OdooConnection

        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )
        connection = OdooConnection(config)
        connection.connect()
        connection.authenticate()
        created_ids = []
        try:
            handler = OdooToolHandler(MagicMock(), connection, AccessController(config), config)

            result = await handler._handle_create_records_tool(
                "res.partner", [{"name": "Batch Create A"}, {"name": "Batch Create B"}]
            )
            created_ids = [record["id"] for record in result["records"]]

            assert result["created_count"] == 2
            assert [r["display_name"] for r in result["records"]] == [
                "Batch Create A",
                "Batch Create B",
            ]
            assert all(r["url"] for r in result["records"])
        finally:
            if created_ids:
                connection.unlink("res.partner", created_ids)
            connection.disconnect()


@pytest.mark.yolo
class TestUpdateRecordsPerRecordYoloIntegration:
    """Live-Odoo test for update_records(updates=...) through web_save_multi."""

    @pytest.mark.asyncio
    async def test_different_values_per_record_in_one_call(self):
        from mcp_server_odoo.access_control import AccessController
        from mcp_server_odoo.odoo_connection import OdooConnection

        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )
        connection = OdooConnection(config)
        connection.connect()
        connection.authenticate()
        major = connection.get_major_version()
        if major is not None and major < 19:
            connection.disconnect()
            pytest.skip("web_save_multi needs Odoo 19 or later")
        created_ids = []
        try:
            created_ids = [
                connection.create("res.partner", {"name": f"Per Record {i}"}) for i in (1, 2)
            ]
            handler = OdooToolHandler(MagicMock(), connection, AccessController(config), config)

            result = await handler._handle_update_records_each_tool(
                "res.partner",
                [
                    {"id": created_ids[0], "values": {"phone": "+100"}},
                    {"id": created_ids[1], "values": {"phone": "+200"}},
                ],
            )

            assert result["updated_count"] == 2
            phones = {
                r["id"]: r["phone"] for r in connection.read("res.partner", created_ids, ["phone"])
            }
            assert phones == {created_ids[0]: "+100", created_ids[1]: "+200"}
        finally:
            if created_ids:
                connection.unlink("res.partner", created_ids)
            connection.disconnect()


@pytest.mark.yolo
class TestUploadAttachmentYoloIntegration:
    """Live round trip: upload_attachment, then read the bytes back as a resource."""

    @pytest.mark.asyncio
    async def test_upload_then_read_back(self):
        import base64

        from mcp_server_odoo.access_control import AccessController
        from mcp_server_odoo.odoo_connection import OdooConnection
        from mcp_server_odoo.resources import OdooResourceHandler

        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )
        connection = OdooConnection(config)
        connection.connect()
        connection.authenticate()
        payload = b"%PDF-1.4 upload round trip"
        partner_id = attachment_id = None
        try:
            partner_id = connection.create("res.partner", {"name": "Upload Round Trip"})
            access = AccessController(config)
            tools = OdooToolHandler(MagicMock(), connection, access, config)
            resources = OdooResourceHandler(MagicMock(), connection, access, config)

            result = await tools._handle_upload_attachment_tool(
                "res.partner",
                partner_id,
                "round-trip.pdf",
                base64.b64encode(payload).decode("ascii"),
                "application/pdf",
            )
            attachment_id = result["attachment_id"]

            content, mimetype = await resources._handle_attachment_read(str(attachment_id))
            assert content == payload
            assert mimetype == "application/pdf"
            listed = connection.search(
                "ir.attachment", [["res_model", "=", "res.partner"], ["res_id", "=", partner_id]]
            )
            assert attachment_id in listed
        finally:
            if attachment_id:
                connection.unlink("ir.attachment", [attachment_id])
            if partner_id:
                connection.unlink("res.partner", [partner_id])
            connection.disconnect()


@pytest.mark.yolo
class TestListRecordAttachmentsYoloIntegration:
    @pytest.mark.asyncio
    async def test_lists_an_attached_file_but_not_the_image_field(self):
        import base64

        from mcp_server_odoo.access_control import AccessController
        from mcp_server_odoo.odoo_connection import OdooConnection

        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )
        connection = OdooConnection(config)
        connection.connect()
        connection.authenticate()
        png = base64.b64encode(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            )
        ).decode("ascii")
        partner_id = None
        try:
            # The image becomes a res_field attachment, which the tool must skip
            partner_id = connection.create(
                "res.partner", {"name": "List Attachments", "image_1920": png}
            )
            handler = OdooToolHandler(MagicMock(), connection, AccessController(config), config)
            uploaded = await handler._handle_upload_attachment_tool(
                "res.partner", partner_id, "notes.txt", base64.b64encode(b"hello").decode()
            )

            result = await handler._handle_list_record_attachments_tool("res.partner", partner_id)

            assert [a["id"] for a in result["attachments"]] == [uploaded["attachment_id"]]
            assert result["attachments"][0]["name"] == "notes.txt"
            assert result["total"] == 1
        finally:
            if partner_id:
                attachment_ids = connection.search(
                    "ir.attachment",
                    [["res_model", "=", "res.partner"], ["res_id", "=", partner_id]],
                )
                if attachment_ids:
                    connection.unlink("ir.attachment", attachment_ids)
                connection.unlink("res.partner", [partner_id])
            connection.disconnect()


@pytest.mark.yolo
class TestReadAttachmentYoloIntegration:
    """Live: read_attachment through the real server, text file and image field."""

    @pytest.mark.asyncio
    async def test_text_attachment_and_image_field(self):
        import base64

        from mcp import Client

        from mcp_server_odoo.server import OdooMCPServer

        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )
        server = OdooMCPServer(config)
        await server.ensure_connected()
        connection = server.connection
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        partner_id = connection.create(
            "res.partner",
            {"name": "Read Attachment", "image_1920": base64.b64encode(png).decode("ascii")},
        )
        attachment_id = connection.create(
            "ir.attachment",
            {
                "name": "notes.txt",
                "res_model": "res.partner",
                "res_id": partner_id,
                ("raw" if connection.get_major_version() >= 20 else "datas"): base64.b64encode(
                    b"meeting notes"
                ).decode("ascii"),
                "mimetype": "text/plain",
            },
        )
        try:
            async with Client(server.app, mode="legacy") as client:
                text = await client.call_tool("read_attachment", {"attachment_id": attachment_id})
                image = await client.call_tool(
                    "read_attachment", {"uri": f"odoo://res.partner/record/{partner_id}/image_1920"}
                )

            assert text.structured_content["kind"] == "text"
            assert text.structured_content["text"] == "meeting notes"
            assert image.structured_content["kind"] == "image"
            assert image.content[1].type == "image"
        finally:
            # The in-process client ran the lifespan, which disconnected on exit
            await server.ensure_connected()
            connection.unlink("ir.attachment", [attachment_id])
            connection.unlink("res.partner", [partner_id])
            server._cleanup_connection()
