"""Tests for the attachment tools (upload_attachment)."""

import base64
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.tools import MAX_UPLOAD_BYTES, OdooToolHandler

PDF = b"%PDF-1.4 attachment test"
PDF_B64 = base64.b64encode(PDF).decode("ascii")


@pytest.fixture
def connection():
    connection = MagicMock(spec=OdooConnection)
    connection.is_authenticated = True
    connection.get_major_version.return_value = 19
    connection.search_count.return_value = 1
    connection.create.return_value = 77
    return connection


@pytest.fixture
def access():
    return MagicMock(spec=AccessController)


@pytest.fixture
def handler(connection, access):
    config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
    return OdooToolHandler(MagicMock(spec=MCPServer), connection, access, config)


class TestUploadAttachment:
    async def test_attaches_the_file_to_the_record(self, handler, connection, access):
        result = await handler._handle_upload_attachment_tool(
            "res.partner", 7, "contract.pdf", PDF_B64, "application/pdf"
        )

        connection.create.assert_called_once_with(
            "ir.attachment",
            {
                "name": "contract.pdf",
                "res_model": "res.partner",
                "res_id": 7,
                "datas": PDF_B64,
                "mimetype": "application/pdf",
            },
        )
        access.validate_model_access.assert_any_call("res.partner", "write")
        access.validate_model_access.assert_any_call("ir.attachment", "create")
        assert result["attachment_id"] == 77
        assert result["uri"] == "odoo://attachment/77"
        assert result["size"] == len(PDF)

    async def test_odoo_20_writes_raw(self, handler, connection):
        """Odoo 20 removed ir.attachment.datas; a create drops it silently."""
        connection.get_major_version.return_value = 20

        await handler._handle_upload_attachment_tool("res.partner", 7, "a.pdf", PDF_B64)

        values = connection.create.call_args[0][1]
        assert values["raw"] == PDF_B64
        assert "datas" not in values
        assert "mimetype" not in values, "Odoo detects it when not given"

    @pytest.mark.parametrize(
        "data,message",
        [
            ("not base64!", "not valid base64"),
            (f"data:application/pdf;base64,{PDF_B64}", "without the 'data:...;base64,' prefix"),
            ("", "The file is empty"),
        ],
    )
    async def test_refuses_bad_data(self, handler, connection, data, message):
        with pytest.raises(ValidationError, match=message):
            await handler._handle_upload_attachment_tool("res.partner", 7, "a.pdf", data)
        connection.create.assert_not_called()

    async def test_refuses_a_file_over_the_cap(self, handler, connection):
        data = base64.b64encode(b"x" * (MAX_UPLOAD_BYTES + 1)).decode("ascii")

        with pytest.raises(ValidationError, match="limit for one upload"):
            await handler._handle_upload_attachment_tool("res.partner", 7, "big.bin", data)
        connection.create.assert_not_called()

    def test_cap_leaves_room_in_the_4_mib_request_body(self):
        encoded = (MAX_UPLOAD_BYTES + 2) // 3 * 4
        assert encoded + 64 * 1024 <= 4 * 1024 * 1024

    @pytest.mark.parametrize("model", ["res.partner", "ir.attachment"])
    async def test_access_refusal_on_either_model(self, handler, connection, access, model):
        def check(checked_model, operation):
            if checked_model == model:
                raise AccessControlError(f"{checked_model} {operation} not allowed")

        access.validate_model_access.side_effect = check

        with pytest.raises(ValidationError, match="not allowed"):
            await handler._handle_upload_attachment_tool("res.partner", 7, "a.pdf", PDF_B64)
        connection.create.assert_not_called()

    async def test_refused_in_yolo_read_mode(self, connection):
        config = OdooConfig(
            url="http://localhost:8069",
            username="admin",
            password="admin",
            database="d",
            yolo_mode="read",
        )
        handler = OdooToolHandler(
            MagicMock(spec=MCPServer), connection, AccessController(config), config
        )

        with pytest.raises(ValidationError):
            await handler._handle_upload_attachment_tool("res.partner", 7, "a.pdf", PDF_B64)
        connection.create.assert_not_called()

    async def test_refuses_a_missing_record(self, handler, connection):
        connection.search_count.return_value = 0

        with pytest.raises(ValidationError, match="Record not found"):
            await handler._handle_upload_attachment_tool("res.partner", 7, "a.pdf", PDF_B64)
        connection.search_count.assert_called_once_with(
            "res.partner", [["id", "=", 7]], context={"active_test": False}
        )
        connection.create.assert_not_called()

    @pytest.mark.parametrize(
        "model,name,message",
        [
            ("ir.attachment", "a.pdf", "not to another attachment"),
            ("res.partner", "  ", "Provide a file name"),
        ],
    )
    async def test_refuses_bad_targets(self, handler, connection, model, name, message):
        with pytest.raises(ValidationError, match=message):
            await handler._handle_upload_attachment_tool(model, 7, name, PDF_B64)
        connection.create.assert_not_called()
