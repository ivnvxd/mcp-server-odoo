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
