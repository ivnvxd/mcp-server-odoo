"""Tests for the batch write tools (create_records)."""

from unittest.mock import MagicMock

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection, OdooValidationFault
from mcp_server_odoo.tools import MAX_BATCH_RECORDS, OdooToolHandler


@pytest.fixture
def config():
    return OdooConfig(url="http://localhost:8069", api_key="k", database="d")


@pytest.fixture
def connection():
    connection = MagicMock(spec=OdooConnection)
    connection.is_authenticated = True
    connection.create_many.return_value = [5, 6]
    # Odoo does not promise read order; the tool restores input order
    connection.read.return_value = [
        {"id": 6, "display_name": "Beta"},
        {"id": 5, "display_name": "Alpha"},
    ]
    connection.build_record_url.side_effect = lambda model, rid: f"http://odoo/{model}/{rid}"
    return connection


@pytest.fixture
def access():
    return MagicMock(spec=AccessController)


@pytest.fixture
def handler(connection, access, config):
    return OdooToolHandler(MagicMock(spec=MCPServer), connection, access, config)


class TestCreateRecords:
    async def test_creates_all_records_in_one_call(self, handler, connection, access):
        records = [{"name": "Alpha"}, {"name": "Beta"}]

        result = await handler._handle_create_records_tool("res.partner", records)

        connection.create_many.assert_called_once_with("res.partner", records)
        access.validate_model_access.assert_called_once_with("res.partner", "create")
        assert result["created_count"] == 2
        assert [r["id"] for r in result["records"]] == [5, 6]
        assert result["records"][0] == {
            "id": 5,
            "display_name": "Alpha",
            "url": "http://odoo/res.partner/5",
        }

    async def test_refuses_an_empty_list(self, handler, connection):
        with pytest.raises(ValidationError, match="No records provided"):
            await handler._handle_create_records_tool("res.partner", [])
        connection.create_many.assert_not_called()

    async def test_refuses_more_than_the_cap(self, handler, connection):
        records = [{"name": f"P{i}"} for i in range(MAX_BATCH_RECORDS + 1)]

        with pytest.raises(ValidationError, match="Too many records"):
            await handler._handle_create_records_tool("res.partner", records)
        connection.create_many.assert_not_called()

    @pytest.mark.parametrize("entry", [{}, "name=Beta", None])
    async def test_refuses_an_entry_that_is_not_field_values(self, handler, connection, entry):
        with pytest.raises(ValidationError, match="Record 1: provide a non-empty object"):
            await handler._handle_create_records_tool("res.partner", [{"name": "Alpha"}, entry])
        connection.create_many.assert_not_called()

    async def test_refuses_an_out_of_range_integer_before_any_rpc(self, handler, connection):
        with pytest.raises(ValidationError, match=r"records\[0\]"):
            await handler._handle_create_records_tool("res.partner", [{"parent_id": 2**31}])
        connection.create_many.assert_not_called()

    async def test_access_refusal(self, handler, connection, access):
        access.validate_model_access.side_effect = AccessControlError("create not allowed")

        with pytest.raises(ValidationError, match="create not allowed"):
            await handler._handle_create_records_tool("res.partner", [{"name": "Alpha"}])
        connection.create_many.assert_not_called()

    async def test_refuses_an_attachment_for_an_inaccessible_model(
        self, handler, connection, access
    ):
        def check(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("not enabled")

        access.validate_model_access.side_effect = check

        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_create_records_tool(
                "ir.attachment",
                [
                    {"name": "a.pdf", "res_model": "res.partner"},
                    {"name": "b.pdf", "res_model": "hr.payslip"},
                ],
            )
        connection.create_many.assert_not_called()

    async def test_odoo_error_keeps_its_message(self, handler, connection):
        connection.create_many.side_effect = OdooValidationFault(
            "The operation cannot be completed: Name is required"
        )

        with pytest.raises(ValidationError, match="Name is required"):
            await handler._handle_create_records_tool("res.partner", [{"email": "x@y.z"}])

    async def test_records_sent_as_a_json_string_are_accepted(self, connection, access, config):
        """Some clients send a list argument as a JSON string; the SDK parses it."""
        app = MCPServer("batch-test")
        OdooToolHandler(app, connection, access, config)

        async with Client(app, mode="legacy") as client:
            result = await client.call_tool(
                "create_records",
                {"model": "res.partner", "records": '[{"name": "Alpha"}, {"name": "Beta"}]'},
            )

        assert not result.is_error, result.content
        connection.create_many.assert_called_once_with(
            "res.partner", [{"name": "Alpha"}, {"name": "Beta"}]
        )


@pytest.fixture
def tool_app(connection, access, config):
    """A real app, so update_records is called with its form dispatch."""
    app = MCPServer("batch-test")
    OdooToolHandler(app, connection, access, config)
    return app


class TestUpdateRecordsPerRecordValues:
    """update_records(updates=[{id, values}]): one web_save_multi call, Odoo 19+."""

    UPDATES = [{"id": 5, "values": {"phone": "1"}}, {"id": 6, "values": {"phone": "2"}}]

    @pytest.fixture(autouse=True)
    def odoo_19(self, connection):
        connection.get_major_version.return_value = 19
        connection.search.return_value = [5, 6]
        connection.web_save_multi.return_value = [
            {"id": 5, "display_name": "Alpha"},
            {"id": 6, "display_name": "Beta"},
        ]

    async def test_writes_each_record_in_one_call(self, handler, connection, access):
        result = await handler._handle_update_records_each_tool("res.partner", self.UPDATES)

        connection.web_save_multi.assert_called_once_with(
            "res.partner", [5, 6], [{"phone": "1"}, {"phone": "2"}]
        )
        connection.search.assert_called_once_with(
            "res.partner", [["id", "in", [5, 6]]], context={"active_test": False}
        )
        access.validate_model_access.assert_called_once_with("res.partner", "write")
        assert result["updated_count"] == 2
        assert result["records"][1] == {"id": 6, "display_name": "Beta"}

    @pytest.mark.parametrize("major", [16, 17, 18])
    async def test_refused_before_odoo_19(self, handler, connection, major):
        connection.get_major_version.return_value = major

        with pytest.raises(ValidationError, match="need Odoo 19 or later"):
            await handler._handle_update_records_each_tool("res.partner", self.UPDATES)
        connection.web_save_multi.assert_not_called()

    async def test_refuses_a_record_given_twice(self, handler, connection):
        updates = self.UPDATES + [{"id": 5, "values": {"phone": "3"}}]

        with pytest.raises(ValidationError, match="Record 5 appears more than once"):
            await handler._handle_update_records_each_tool("res.partner", updates)
        connection.web_save_multi.assert_not_called()

    @pytest.mark.parametrize(
        "entry",
        [
            {"id": 5},
            {"id": 5, "values": {}},
            {"id": True, "values": {"phone": "1"}},
            {"values": {"phone": "1"}},
            [5, {"phone": "1"}],
        ],
    )
    async def test_refuses_a_malformed_entry(self, handler, connection, entry):
        with pytest.raises(ValidationError, match="Update 0: provide"):
            await handler._handle_update_records_each_tool("res.partner", [entry])
        connection.web_save_multi.assert_not_called()

    async def test_refuses_more_than_the_cap(self, handler, connection):
        updates = [{"id": i, "values": {"phone": "1"}} for i in range(1, MAX_BATCH_RECORDS + 2)]

        with pytest.raises(ValidationError, match="Too many records"):
            await handler._handle_update_records_each_tool("res.partner", updates)

    async def test_names_missing_records(self, handler, connection):
        connection.search.return_value = [5]

        with pytest.raises(ValidationError, match=r"not found.*\[6\]"):
            await handler._handle_update_records_each_tool("res.partner", self.UPDATES)
        connection.web_save_multi.assert_not_called()

    async def test_refusal_by_the_mcp_module_keeps_its_text(self, handler, connection):
        """Standard mode: a module version without web_save_multi in its method map refuses it."""
        connection.web_save_multi.side_effect = OdooValidationFault(
            "Access denied by MCP for model 'res.partner' method 'web_save_multi'."
        )

        with pytest.raises(ValidationError, match="method 'web_save_multi'"):
            await handler._handle_update_records_each_tool("res.partner", self.UPDATES)

    async def test_refuses_an_attachment_on_an_inaccessible_model(self, handler, connection):
        connection.search_read.return_value = [{"id": 5, "res_model": "hr.payslip"}]

        def check(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("not enabled")

        handler.access_controller.validate_model_access.side_effect = check

        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_update_records_each_tool(
                "ir.attachment", [{"id": 5, "values": {"name": "x.pdf"}}]
            )
        connection.web_save_multi.assert_not_called()

    @pytest.mark.parametrize(
        "arguments,message",
        [
            (
                {"record_ids": [5], "values": {"phone": "1"}, "updates": UPDATES},
                "not both",
            ),
            ({"record_ids": [5]}, "Provide record_ids with values, or updates"),
            ({}, "Provide record_ids with values, or updates"),
        ],
    )
    async def test_exactly_one_form(self, tool_app, connection, arguments, message):
        async with Client(tool_app, mode="legacy") as client:
            result = await client.call_tool("update_records", {"model": "res.partner", **arguments})

        assert result.is_error
        assert message in result.content[0].text
        connection.write.assert_not_called()
        connection.web_save_multi.assert_not_called()

    async def test_updates_form_through_the_protocol(self, tool_app, connection):
        async with Client(tool_app, mode="legacy") as client:
            result = await client.call_tool(
                "update_records", {"model": "res.partner", "updates": self.UPDATES}
            )

        assert not result.is_error, result.content
        assert result.structured_content["updated_count"] == 2
