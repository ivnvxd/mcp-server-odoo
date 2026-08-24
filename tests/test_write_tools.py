"""Tests for write operation tools."""

from unittest.mock import Mock, call

import pytest

from mcp_server_odoo.access_control import AccessControlError
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnectionError
from mcp_server_odoo.tools import OdooToolHandler, register_tools


class TestWriteTools:
    """Test write operation tools."""

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        app = Mock()
        app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
        return app

    @pytest.fixture
    def mock_connection(self):
        """Create mock OdooConnection."""
        conn = Mock()
        conn.is_authenticated = True
        conn.build_record_url.side_effect = lambda model, record_id: (
            f"http://localhost:8069/web#id={record_id}&model={model}&view_type=form"
        )
        return conn

    @pytest.fixture
    def mock_access_controller(self):
        """Create mock AccessController."""
        controller = Mock()
        controller.validate_model_access = Mock()
        return controller

    @pytest.fixture
    def mock_config(self):
        """Create mock OdooConfig."""
        config = Mock()
        config.default_limit = 10
        config.max_limit = 100
        config.url = "http://localhost:8069"
        return config

    @pytest.fixture
    def tool_handler(self, mock_app, mock_connection, mock_access_controller, mock_config):
        """Create OdooToolHandler instance."""
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, mock_config)

    @pytest.mark.asyncio
    async def test_create_record_success(self, tool_handler, mock_connection):
        """Test successful record creation."""
        # Setup
        model = "res.partner"
        values = {"name": "Test Partner", "email": "test@example.com"}
        created_id = 123
        essential_record = {
            "id": created_id,
            "display_name": "Test Partner",
        }

        mock_connection.create.return_value = created_id
        mock_connection.read.return_value = [essential_record]

        # Execute
        result = await tool_handler._handle_create_record_tool(model, values)

        # Verify
        assert result["success"] is True
        assert result["record"] == essential_record
        assert (
            result["url"]
            == f"http://localhost:8069/web#id={created_id}&model={model}&view_type=form"
        )
        assert "Successfully created" in result["message"]
        mock_connection.create.assert_called_once_with(model, values)
        mock_connection.read.assert_called_once_with(model, [created_id], ["id", "display_name"])

    @pytest.mark.asyncio
    async def test_create_record_model_without_name_field(self, tool_handler, mock_connection):
        """Test creating a record on a model that lacks the 'name' field (e.g. mail.activity)."""
        model = "mail.activity"
        values = {"res_model_id": 448, "res_id": 2887, "activity_type_id": 4}
        created_id = 42
        essential_record = {"id": created_id, "display_name": "Activity #42"}

        mock_connection.create.return_value = created_id
        mock_connection.read.return_value = [essential_record]

        result = await tool_handler._handle_create_record_tool(model, values)

        assert result["success"] is True
        assert result["record"] == essential_record
        # Only universally available fields requested — no 'name'
        mock_connection.read.assert_called_once_with(model, [created_id], ["id", "display_name"])

    @pytest.mark.asyncio
    async def test_create_record_no_values(self, tool_handler):
        """Test create record with no values."""
        with pytest.raises(ValidationError, match="No values provided"):
            await tool_handler._handle_create_record_tool("res.partner", {})

    @pytest.mark.asyncio
    async def test_update_record_success(self, tool_handler, mock_connection):
        """Test successful record update."""
        # Setup
        model = "res.partner"
        record_id = 123
        values = {"email": "updated@example.com"}
        # First read call (existence check) returns just ID
        existing_record = {"id": record_id}
        # Second read call returns essential fields
        updated_record = {"id": record_id, "display_name": "Test Partner"}

        mock_connection.read.side_effect = [[existing_record], [updated_record]]
        mock_connection.write.return_value = True

        # Execute
        result = await tool_handler._handle_update_record_tool(model, record_id, values)

        # Verify
        assert result["success"] is True
        assert result["record"] == updated_record
        assert (
            result["url"]
            == f"http://localhost:8069/web#id={record_id}&model={model}&view_type=form"
        )
        assert "Successfully updated" in result["message"]
        mock_connection.write.assert_called_once_with(model, [record_id], values)
        # Verify both read calls with correct parameters
        expected_calls = [
            call(model, [record_id], ["id"]),  # Existence check
            call(model, [record_id], ["id", "display_name"]),  # Essential fields
        ]
        mock_connection.read.assert_has_calls(expected_calls)

    @pytest.mark.asyncio
    async def test_update_record_model_without_name_field(self, tool_handler, mock_connection):
        """Test updating a record on a model that lacks the 'name' field."""
        model = "mail.activity"
        record_id = 42
        values = {"summary": "Updated summary"}
        existing_record = {"id": record_id}
        updated_record = {"id": record_id, "display_name": "Activity #42"}

        mock_connection.read.side_effect = [[existing_record], [updated_record]]
        mock_connection.write.return_value = True

        result = await tool_handler._handle_update_record_tool(model, record_id, values)

        assert result["success"] is True
        # Only universally available fields requested — no 'name'
        expected_calls = [
            call(model, [record_id], ["id"]),
            call(model, [record_id], ["id", "display_name"]),
        ]
        mock_connection.read.assert_has_calls(expected_calls)

    @pytest.mark.asyncio
    async def test_update_record_not_found(self, tool_handler, mock_connection):
        """Test update record that doesn't exist."""
        mock_connection.read.return_value = []

        with pytest.raises(ValidationError, match="Record not found"):
            await tool_handler._handle_update_record_tool("res.partner", 999, {"name": "Test"})

    @pytest.mark.asyncio
    async def test_update_record_no_values(self, tool_handler):
        """Test update record with no values."""
        with pytest.raises(ValidationError, match="No values provided"):
            await tool_handler._handle_update_record_tool("res.partner", 123, {})

    @pytest.mark.asyncio
    async def test_delete_record_success(self, tool_handler, mock_connection):
        """Test successful record deletion."""
        # Setup
        model = "res.partner"
        record_id = 123
        existing_record = {"id": record_id, "display_name": "Test Partner"}

        mock_connection.read.return_value = [existing_record]
        mock_connection.unlink.return_value = True

        # Execute
        result = await tool_handler._handle_delete_record_tool(model, record_id)

        # Verify
        assert result["success"] is True
        assert result["deleted_id"] == record_id
        assert result["deleted_name"] == "Test Partner"
        assert "Successfully deleted" in result["message"]
        mock_connection.unlink.assert_called_once_with(model, [record_id])
        mock_connection.read.assert_called_once_with(model, [record_id], ["id", "display_name"])

    @pytest.mark.asyncio
    async def test_delete_record_without_display_name(self, tool_handler, mock_connection):
        """Records without a display name (e.g. mail.message) return False —
        the result must fall back to an ID label, not crash DeleteResult
        validation AFTER the unlink already succeeded.

        Found in manual testing: deleting a mail.message deleted the record
        but returned a Pydantic error, leaving the client believing the
        delete failed.
        """
        from mcp_server_odoo.schemas import DeleteResult

        mock_connection.read.return_value = [{"id": 6115, "display_name": False}]
        mock_connection.unlink.return_value = True

        result = await tool_handler._handle_delete_record_tool("mail.message", 6115)

        assert result["success"] is True
        assert result["deleted_name"] == "ID 6115"
        # The dict must validate against the declared result schema
        DeleteResult(**result)

    @pytest.mark.asyncio
    async def test_delete_record_not_found(self, tool_handler, mock_connection):
        """Test delete record that doesn't exist."""
        mock_connection.read.return_value = []

        with pytest.raises(ValidationError, match="Record not found"):
            await tool_handler._handle_delete_record_tool("res.partner", 999)

    @pytest.mark.asyncio
    async def test_create_record_not_authenticated(self, tool_handler, mock_connection):
        """Test create record when not authenticated."""
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError, match="Not authenticated"):
            await tool_handler._handle_create_record_tool("res.partner", {"name": "Test"})

    @pytest.mark.asyncio
    async def test_update_record_connection_error(self, tool_handler, mock_connection):
        """Test update record with connection error."""
        mock_connection.read.side_effect = OdooConnectionError("Connection failed")

        with pytest.raises(ValidationError, match="Connection error"):
            await tool_handler._handle_update_record_tool("res.partner", 123, {"name": "Test"})

    @pytest.mark.asyncio
    async def test_create_record_calls_validate_model_access(
        self, tool_handler, mock_access_controller
    ):
        """Verify that create_record actually calls validate_model_access with 'create'."""
        # If someone removes the access control check, this test will fail
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        with pytest.raises(ValidationError, match="Access denied"):
            await tool_handler._handle_create_record_tool("res.partner", {"name": "Test"})

        mock_access_controller.validate_model_access.assert_called_once_with(
            "res.partner", "create"
        )

    @pytest.mark.asyncio
    async def test_update_record_calls_validate_model_access(
        self, tool_handler, mock_access_controller, mock_connection
    ):
        """Verify that update_record actually calls validate_model_access with 'write'."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        with pytest.raises(ValidationError, match="Access denied"):
            await tool_handler._handle_update_record_tool("res.partner", 1, {"name": "Test"})

        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "write")

    @pytest.mark.asyncio
    async def test_delete_record_calls_validate_model_access(
        self, tool_handler, mock_access_controller
    ):
        """Verify that delete_record actually calls validate_model_access with 'unlink'."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        with pytest.raises(ValidationError, match="Access denied"):
            await tool_handler._handle_delete_record_tool("res.partner", 1)

        mock_access_controller.validate_model_access.assert_called_once_with(
            "res.partner", "unlink"
        )

    @pytest.mark.asyncio
    async def test_create_record_access_control_precedes_connection(
        self, tool_handler, mock_access_controller, mock_connection
    ):
        """Access control check must happen before any connection calls."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError("No access")

        with pytest.raises(ValidationError):
            await tool_handler._handle_create_record_tool("res.partner", {"name": "X"})

        # Connection should never be touched if access is denied
        mock_connection.create.assert_not_called()
        mock_connection.read.assert_not_called()

    def test_tools_registered(self, mock_app, mock_connection, mock_access_controller, mock_config):
        """Test that write tools are registered."""
        # Track functions that were decorated
        decorated_functions = []

        def mock_tool_decorator(**kwargs):
            def decorator(func):
                decorated_functions.append(func.__name__)
                return func

            return decorator

        mock_app.tool = mock_tool_decorator

        register_tools(mock_app, mock_connection, mock_access_controller, mock_config)

        # Check that tool decorator was called for write operations
        assert "create_record" in decorated_functions
        assert "update_record" in decorated_functions
        assert "delete_record" in decorated_functions


class TestWriteToolsIntegration:
    """Integration tests for write tools with real connection."""

    @pytest.fixture
    def real_config(self):
        """Create config with YOLO full-access mode for write testing."""
        import os

        from mcp_server_odoo.config import OdooConfig

        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )

    @pytest.fixture
    def real_connection(self, real_config):
        """Create real connection."""
        from mcp_server_odoo.odoo_connection import OdooConnection

        conn = OdooConnection(real_config)
        conn.connect()
        conn.authenticate()
        yield conn
        conn.disconnect()

    @pytest.fixture
    def real_access_controller(self, real_config):
        """Create real access controller."""
        from mcp_server_odoo.access_control import AccessController

        return AccessController(real_config)

    @pytest.fixture
    def real_app(self):
        """Create real FastMCP app."""
        from mcp.server.fastmcp import FastMCP

        return FastMCP("test-app")

    @pytest.fixture
    def real_tool_handler(self, real_app, real_connection, real_access_controller, real_config):
        """Create real tool handler."""
        return register_tools(real_app, real_connection, real_access_controller, real_config)

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_create_update_delete_cycle(self, real_config, real_tool_handler):
        """Test full create, update, delete cycle with real Odoo."""
        handler = real_tool_handler

        # Create a test partner
        create_values = {
            "name": "MCP Test Partner",
            "email": "mcp.test@example.com",
            "is_company": False,
        }

        # Create
        create_result = await handler._handle_create_record_tool("res.partner", create_values)
        assert create_result["success"] is True
        record_id = create_result["record"]["id"]
        assert "MCP Test Partner" in create_result["record"]["display_name"]

        try:
            # Update
            update_values = {
                "email": "mcp.updated@example.com",
                "phone": "+1234567890",
            }
            update_result = await handler._handle_update_record_tool(
                "res.partner", record_id, update_values
            )
            assert update_result["success"] is True

            # Verify updated values via get_record (update result only has essential fields)
            get_result = await handler._handle_get_record_tool(
                "res.partner", record_id, fields=["email", "phone"]
            )
            assert get_result.record["email"] == "mcp.updated@example.com"
            assert get_result.record["phone"] == "+1234567890"

            # Delete
            delete_result = await handler._handle_delete_record_tool("res.partner", record_id)
            assert delete_result["success"] is True
            assert delete_result["deleted_id"] == record_id

            # Verify deletion
            from mcp_server_odoo.tools import ValidationError

            with pytest.raises(ValidationError, match="Record not found"):
                await handler._handle_get_record_tool("res.partner", record_id, fields=None)

        except Exception:
            # Clean up if test fails
            try:
                handler.connection.unlink("res.partner", [record_id])
            except Exception:
                pass
            raise

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_post_message_note_to_partner(self, real_tool_handler):
        """Posting a note to res.partner creates a mail.message linked to the record."""
        handler = real_tool_handler

        # Plain-str → '&lt;' escape behavior is v17+ in mail.thread.message_post.
        # On v16 the mail module strips text after '<' as if it were a tag.
        # This test asserts on the modern behavior; skip on v16.
        major = handler.connection.get_major_version()
        if major is not None and major < 17:
            pytest.skip(f"plain-str HTML escape is v17+; server is v{major}")

        # Use main_partner (always present) — we'll clean up the message afterwards
        partner_ids = handler.connection.search("res.partner", [], limit=1)
        assert partner_ids, "Need at least one res.partner for this test"
        partner_id = partner_ids[0]

        # Body contains '<' so we can verify Odoo 17+'s plain-str escape behavior
        # (Odoo wraps any '<' in str body to '&lt;' when body_is_html is False).
        body = "MCP integration test: 5 < 10 & still works"
        result = await handler._handle_post_message_tool(
            "res.partner",
            partner_id,
            body,
            "note",
            "comment",
            None,
            None,
            False,
        )

        message_id = result["message_id"]
        assert result["success"] is True
        assert isinstance(message_id, int) and message_id > 0

        try:
            # Verify the message exists, is linked correctly, AND that the angle
            # bracket got escaped — confirming body_is_html=False is honored.
            messages = handler.connection.search_read(
                "mail.message",
                [("id", "=", message_id)],
                ["model", "res_id", "body", "subtype_id"],
            )
            assert len(messages) == 1
            msg = messages[0]
            assert msg["model"] == "res.partner"
            assert msg["res_id"] == partner_id
            stored_body = msg["body"]
            # Plain-str path must escape '<' to '&lt;' (positive assertion catches
            # both "literal < survived" and "substring stripped entirely" failures).
            assert "5 &lt; 10" in stored_body, (
                f"Expected '<' to be escaped to '&lt;', got: {stored_body!r}"
            )
            assert "5 < 10" not in stored_body, (
                f"Plain-str body should be escaped, but stored as: {stored_body!r}"
            )
        finally:
            try:
                handler.connection.unlink("mail.message", [message_id])
            except Exception:
                pass

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_post_message_comment_subtype_resolves_to_mt_comment(self, real_tool_handler):
        """subtype='comment' yields a message whose subtype xmlid is mail.mt_comment."""
        handler = real_tool_handler

        partner_ids = handler.connection.search("res.partner", [], limit=1)
        partner_id = partner_ids[0]

        result = await handler._handle_post_message_tool(
            "res.partner",
            partner_id,
            "MCP integration test: comment subtype",
            "comment",
            "comment",
            None,
            None,
            False,
        )
        message_id = result["message_id"]

        try:
            messages = handler.connection.search_read(
                "mail.message",
                [("id", "=", message_id)],
                ["subtype_id"],
            )
            assert len(messages) == 1
            subtype_field = messages[0]["subtype_id"]
            # Many2one comes back as [id, name] or False
            assert subtype_field, "Expected a subtype to be set"
            subtype_id = subtype_field[0] if isinstance(subtype_field, list) else subtype_field

            # Resolve xmlid via ir.model.data
            xml_records = handler.connection.search_read(
                "ir.model.data",
                [
                    ("model", "=", "mail.message.subtype"),
                    ("res_id", "=", subtype_id),
                ],
                ["module", "name"],
            )
            assert any(
                r.get("module") == "mail" and r.get("name") == "mt_comment" for r in xml_records
            ), f"Expected mail.mt_comment subtype, got {xml_records}"
        finally:
            try:
                handler.connection.unlink("mail.message", [message_id])
            except Exception:
                pass

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_post_message_non_mail_thread_model_rejected(self, real_tool_handler):
        """Posting against a model without mail.thread raises ValidationError mentioning mail.thread."""
        from mcp_server_odoo.error_handling import ValidationError

        handler = real_tool_handler

        # res.country has no mail.thread — pick any country id (always populated)
        country_ids = handler.connection.search("res.country", [], limit=1)
        assert country_ids, "Need at least one res.country for this test"

        with pytest.raises(ValidationError, match="mail.thread"):
            await handler._handle_post_message_tool(
                "res.country",
                country_ids[0],
                "should fail",
                "note",
                "comment",
                None,
                None,
                False,
            )

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_post_message_body_is_html_preserves_markup(self, real_tool_handler):
        """body_is_html=True forwards the kwarg so Odoo stores HTML markup unescaped."""
        handler = real_tool_handler

        partner_ids = handler.connection.search("res.partner", [], limit=1)
        partner_id = partner_ids[0]

        body = "<p><b>bold</b> and <i>italic</i></p>"
        result = await handler._handle_post_message_tool(
            "res.partner",
            partner_id,
            body,
            "note",
            "comment",
            None,
            None,
            True,  # body_is_html
        )
        message_id = result["message_id"]

        try:
            messages = handler.connection.search_read(
                "mail.message",
                [("id", "=", message_id)],
                ["body"],
            )
            stored_body = messages[0]["body"]
            # With body_is_html=True the literal HTML tags survive — neither
            # '<p>' nor '<b>' should be encoded to '&lt;p&gt;' / '&lt;b&gt;'
            assert "<p>" in stored_body, f"Expected literal <p>, got: {stored_body!r}"
            assert "<b>bold</b>" in stored_body, (
                f"Expected literal <b>bold</b>, got: {stored_body!r}"
            )
            assert "&lt;p&gt;" not in stored_body, (
                f"HTML markup was double-escaped: {stored_body!r}"
            )
        finally:
            try:
                handler.connection.unlink("mail.message", [message_id])
            except Exception:
                pass

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_post_message_partner_ids_added_as_recipients(self, real_tool_handler):
        """partner_ids forwards to message_post and lands on mail.message.partner_ids."""
        handler = real_tool_handler

        # Need at least two partners — one to post to, one to notify
        partner_ids = handler.connection.search("res.partner", [], limit=2)
        if len(partner_ids) < 2:
            pytest.skip("Need at least two res.partner records for this test")
        target_id, recipient_id = partner_ids[0], partner_ids[1]

        result = await handler._handle_post_message_tool(
            "res.partner",
            target_id,
            "MCP integration test: notify recipient",
            "comment",
            "comment",
            [recipient_id],
            None,
            False,
        )
        message_id = result["message_id"]

        try:
            messages = handler.connection.search_read(
                "mail.message",
                [("id", "=", message_id)],
                ["partner_ids"],
            )
            recipients = messages[0]["partner_ids"]  # list of ids
            assert recipient_id in recipients, (
                f"Expected partner {recipient_id} in recipients, got {recipients}"
            )
        finally:
            try:
                handler.connection.unlink("mail.message", [message_id])
            except Exception:
                pass

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_post_message_subject_stored(self, real_tool_handler):
        """subject= forwards to message_post and lands on mail.message.subject."""
        handler = real_tool_handler

        partner_ids = handler.connection.search("res.partner", [], limit=1)
        assert partner_ids, "Need at least one res.partner for this test"
        partner_id = partner_ids[0]

        result = await handler._handle_post_message_tool(
            "res.partner",
            partner_id,
            "MCP integration test: message with subject",
            "note",
            "comment",
            None,
            None,
            False,
            subject="MCP subject test",
        )
        message_id = result["message_id"]

        try:
            messages = handler.connection.read("mail.message", [message_id], ["subject"])
            assert messages[0]["subject"] == "MCP subject test"
        finally:
            try:
                handler.connection.unlink("mail.message", [message_id])
            except Exception:
                pass


class TestCallModelMethodIntegration:
    """YOLO integration tests for the gated call_model_method tool.

    Requires both ``ODOO_YOLO=true`` and ``ODOO_MCP_ENABLE_METHOD_CALLS=true``;
    skipped otherwise. The tool is invisible to the client without the second
    flag, so without it there is nothing to integration-test.
    """

    @pytest.fixture(autouse=True)
    def _require_opt_in(self):
        import os

        if os.getenv("ODOO_MCP_ENABLE_METHOD_CALLS", "").strip().lower() != "true":
            pytest.skip(
                "Set ODOO_MCP_ENABLE_METHOD_CALLS=true (with ODOO_YOLO=true) "
                "to run call_model_method integration tests."
            )

    @pytest.fixture
    def real_config(self):
        import os

        from mcp_server_odoo.config import OdooConfig

        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
            enable_method_calls=True,
        )

    @pytest.fixture
    def real_connection(self, real_config):
        from mcp_server_odoo.odoo_connection import OdooConnection

        conn = OdooConnection(real_config)
        conn.connect()
        conn.authenticate()
        yield conn
        conn.disconnect()

    @pytest.fixture
    def real_access_controller(self, real_config):
        from mcp_server_odoo.access_control import AccessController

        return AccessController(real_config)

    @pytest.fixture
    def real_app(self):
        from mcp.server.fastmcp import FastMCP

        return FastMCP("test-app")

    @pytest.fixture
    def real_tool_handler(self, real_app, real_connection, real_access_controller, real_config):
        return register_tools(real_app, real_connection, real_access_controller, real_config)

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_toggle_active_round_trip(self, real_tool_handler):
        """Happy path: toggle_active flips res.partner.active; idempotent under double toggle."""
        handler = real_tool_handler

        create_result = await handler._handle_create_record_tool(
            "res.partner", {"name": "MCP CallMethod Test", "is_company": False}
        )
        partner_id = create_result["record"]["id"]

        try:
            # First toggle: True -> False (do not assert toggle_active's return
            # value; it varies across Odoo versions).
            await handler._handle_call_model_method_tool(
                "res.partner", "toggle_active", [[partner_id]], None
            )
            row = handler.connection.read("res.partner", [partner_id], ["active"])
            assert row[0]["active"] is False, "expected partner deactivated"

            # Second toggle: False -> True
            await handler._handle_call_model_method_tool(
                "res.partner", "toggle_active", [[partner_id]], None
            )
            row = handler.connection.read("res.partner", [partner_id], ["active"])
            assert row[0]["active"] is True, "expected partner reactivated"
        finally:
            try:
                handler.connection.unlink("res.partner", [partner_id])
            except Exception:
                pass

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_json_string_arguments_round_trip(self, real_tool_handler):
        """JSON-string ``arguments`` form is parsed and reaches Odoo identically."""
        handler = real_tool_handler

        create_result = await handler._handle_create_record_tool(
            "res.partner", {"name": "MCP CallMethod JSON-args"}
        )
        partner_id = create_result["record"]["id"]

        try:
            await handler._handle_call_model_method_tool(
                "res.partner", "toggle_active", f"[[{partner_id}]]", None
            )
            row = handler.connection.read("res.partner", [partner_id], ["active"])
            assert row[0]["active"] is False
        finally:
            try:
                handler.connection.unlink("res.partner", [partner_id])
            except Exception:
                pass

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_kwargs_path_via_toggle_active_with_context(self, real_tool_handler):
        """``keyword_arguments`` reach execute_kw — a context kwarg rides along toggle_active.

        (``read`` used to be the vehicle here, but ORM data-access primitives
        are now denylisted; any method accepts a ``context`` kwarg via execute_kw.)
        """
        handler = real_tool_handler

        create_result = await handler._handle_create_record_tool(
            "res.partner", {"name": "MCP CallMethod kwargs"}
        )
        partner_id = create_result["record"]["id"]

        try:
            result = await handler._handle_call_model_method_tool(
                "res.partner",
                "toggle_active",
                [[partner_id]],
                {"context": {"lang": "en_US"}},
            )
            assert result["success"] is True
            row = handler.connection.read("res.partner", [partner_id], ["active"])
            assert row[0]["active"] is False, "expected partner deactivated"
        finally:
            try:
                handler.connection.unlink("res.partner", [partner_id])
            except Exception:
                pass

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_denylisted_calls_rejected_live(self, real_tool_handler):
        """Blocked models/methods are refused before any RPC happens."""
        from mcp_server_odoo.error_handling import ValidationError

        handler = real_tool_handler

        with pytest.raises(ValidationError, match="elevated privileges"):
            await handler._handle_call_model_method_tool("ir.actions.server", "run", [[1]], None)
        with pytest.raises(ValidationError, match="elevated privileges"):
            await handler._handle_call_model_method_tool(
                "ir.cron", "method_direct_trigger", [[1]], None
            )
        with pytest.raises(ValidationError, match=r"web_\* data-access family"):
            await handler._handle_call_model_method_tool("res.partner", "web_read", [[1]], None)
        with pytest.raises(ValidationError, match="ORM data-access primitive"):
            await handler._handle_call_model_method_tool(
                "res.partner", "create", [[{"name": "x"}]], None
            )

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_private_method_rejected_live(self, real_tool_handler):
        """``_compute_*`` is rejected before any RPC happens."""
        from mcp_server_odoo.error_handling import ValidationError

        handler = real_tool_handler
        with pytest.raises(ValidationError, match="public ASCII Python identifiers"):
            await handler._handle_call_model_method_tool(
                "res.partner", "_compute_display_name", [[1]], None
            )

    @pytest.mark.yolo
    @pytest.mark.asyncio
    async def test_nonexistent_method_returns_validation_error(self, real_tool_handler):
        """Unknown public method on a real model surfaces as a Connection error → ValidationError."""
        from mcp_server_odoo.error_handling import ValidationError

        handler = real_tool_handler
        with pytest.raises(ValidationError) as exc_info:
            await handler._handle_call_model_method_tool(
                "res.partner", "definitely_does_not_exist", [[1]], None
            )
        # Either the OdooConnectionError → "Connection error" path or the
        # generic sanitized "Failed to call model method" — accept both.
        msg = str(exc_info.value)
        assert "Connection error" in msg or "Failed to call model method" in msg


class TestPostMessageMCPIntegration:
    """MCP-mode integration tests for post_message — exercises the standard MCP module endpoints."""

    @pytest.fixture
    def mcp_config(self):
        """Standard-mode config (no YOLO) — relies on .env / environment."""
        from mcp_server_odoo.config import get_config

        return get_config()

    @pytest.fixture
    def mcp_connection(self, mcp_config):
        from mcp_server_odoo.odoo_connection import OdooConnection

        conn = OdooConnection(mcp_config)
        conn.connect()
        conn.authenticate()
        yield conn
        conn.disconnect()

    @pytest.fixture
    def mcp_access_controller(self, mcp_config):
        from mcp_server_odoo.access_control import AccessController

        return AccessController(mcp_config)

    @pytest.fixture
    def mcp_app(self):
        from mcp.server.fastmcp import FastMCP

        return FastMCP("test-app-mcp")

    @pytest.fixture
    def mcp_tool_handler(self, mcp_app, mcp_connection, mcp_access_controller, mcp_config):
        return register_tools(mcp_app, mcp_connection, mcp_access_controller, mcp_config)

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_post_message_mcp_happy_path(self, mcp_tool_handler, writable_model):
        """Posting a note via MCP endpoints succeeds on a writable model."""
        handler = mcp_tool_handler
        model = writable_model.model

        record_ids = handler.connection.search(model, [], limit=1)
        if not record_ids:
            pytest.skip(f"No records of {model} available for post_message test")

        # If the writable model lacks mail.thread, skip — the MCP write gate is
        # the focus here, not chatter coverage (covered by unit + YOLO tests).
        result = None
        try:
            result = await handler._handle_post_message_tool(
                model,
                record_ids[0],
                "MCP integration test: standard-mode note",
                "note",
                "comment",
                None,
                None,
                False,
            )
        except ValidationError as e:
            if "mail.thread" in str(e):
                pytest.skip(f"Writable model {model} has no mail.thread — skipping")
            raise

        assert result["success"] is True
        message_id = result["message_id"]
        assert isinstance(message_id, int) and message_id > 0

        # Verification reads mail.message, which not every MCP deployment exposes
        try:
            messages = handler.connection.search_read(
                "mail.message",
                [("id", "=", message_id)],
                ["model", "res_id"],
            )
        except OdooConnectionError as e:
            if "Permission denied" in str(e) or "Access denied" in str(e):
                pytest.skip("mail.message not exposed via MCP — post succeeded but unverifiable")
            raise

        try:
            assert len(messages) == 1
            assert messages[0]["model"] == model
            assert messages[0]["res_id"] == record_ids[0]
        finally:
            try:
                handler.connection.unlink("mail.message", [message_id])
            except Exception:
                pass

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_post_message_mcp_comment_subtype(self, mcp_tool_handler, writable_model):
        """subtype='comment' yields mail.mt_comment via MCP endpoints."""
        handler = mcp_tool_handler
        model = writable_model.model

        record_ids = handler.connection.search(model, [], limit=1)
        if not record_ids:
            pytest.skip(f"No records of {model} available for post_message test")

        message_id = None
        try:
            result = await handler._handle_post_message_tool(
                model,
                record_ids[0],
                "MCP integration test: standard-mode comment",
                "comment",
                "comment",
                None,
                None,
                False,
            )
            assert result["success"] is True
            message_id = result["message_id"]
            assert isinstance(message_id, int) and message_id > 0
        except ValidationError as e:
            err = str(e)
            if "mail.thread" in err:
                pytest.skip(f"Writable model {model} has no mail.thread — skipping")
            if "rate limit" in err.lower():
                pytest.skip("MCP rate limiter fired under test load — non-deterministic")
            raise

        # Verification reads mail.message and ir.model.data, neither of which
        # every MCP deployment exposes. Skip visibly when they aren't.
        try:
            messages = handler.connection.search_read(
                "mail.message",
                [("id", "=", message_id)],
                ["subtype_id"],
            )
        except OdooConnectionError as e:
            if "Permission denied" in str(e) or "Access denied" in str(e):
                pytest.skip("mail.message not exposed via MCP — subtype unverifiable")
            raise

        try:
            subtype_field = messages[0]["subtype_id"]
            assert subtype_field, "Expected a subtype to be set"
            subtype_id = subtype_field[0] if isinstance(subtype_field, list) else subtype_field

            try:
                xml_records = handler.connection.search_read(
                    "ir.model.data",
                    [
                        ("model", "=", "mail.message.subtype"),
                        ("res_id", "=", subtype_id),
                    ],
                    ["module", "name"],
                )
            except OdooConnectionError as e:
                if "Permission denied" in str(e) or "Access denied" in str(e):
                    pytest.skip("ir.model.data not exposed via MCP — subtype name unverifiable")
                raise

            assert any(
                r.get("module") == "mail" and r.get("name") == "mt_comment" for r in xml_records
            ), f"Expected mail.mt_comment subtype, got {xml_records}"
        finally:
            if message_id:
                try:
                    handler.connection.unlink("mail.message", [message_id])
                except Exception:
                    pass

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_post_message_mcp_disabled_model_denied(self, mcp_tool_handler, disabled_model):
        """Posting to a model not enabled in MCP module → ValidationError 'Access denied'."""
        handler = mcp_tool_handler
        with pytest.raises(ValidationError, match="Access denied"):
            await handler._handle_post_message_tool(
                disabled_model,
                1,
                "should be denied",
                "note",
                "comment",
                None,
                None,
                False,
            )
