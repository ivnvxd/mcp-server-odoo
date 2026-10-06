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


class TestListRecordAttachments:
    ROWS = [
        {
            "id": 31,
            "name": "contract.pdf",
            "mimetype": "application/pdf",
            "file_size": 2048,
            "type": "binary",
            "create_date": "2026-10-01 09:00:00",
        },
        {
            "id": 30,
            "name": "Website",
            "mimetype": False,
            "file_size": 0,
            "type": "url",
            "create_date": "2026-09-30 09:00:00",
        },
    ]

    @pytest.fixture(autouse=True)
    def two_attachments(self, connection):
        connection.search_count.side_effect = lambda model, domain, **kw: (
            1 if model == "res.partner" else 2
        )
        connection.search_read.return_value = [dict(row) for row in self.ROWS]

    async def test_lists_the_files_attached_to_the_record(self, handler, connection, access):
        result = await handler._handle_list_record_attachments_tool("res.partner", 7)

        call = connection.search_read.call_args
        assert call[0][0] == "ir.attachment"
        assert call[0][1] == [
            ["res_model", "=", "res.partner"],
            ["res_id", "=", 7],
            ["res_field", "=", False],
        ]
        assert call[1]["order"] == "create_date desc, id desc"
        access.validate_model_access.assert_any_call("res.partner", "read")
        access.validate_model_access.assert_any_call("ir.attachment", "read")
        assert result["total"] == 2
        assert result["note"] is None
        first, second = result["attachments"]
        assert first["uri"] == "odoo://attachment/31"
        assert (first["name"], first["mimetype"], first["size"]) == (
            "contract.pdf",
            "application/pdf",
            2048,
        )
        assert (second["type"], second["mimetype"], second["size"]) == ("url", None, None)

    async def test_notes_a_cut_off_list(self, handler, connection):
        connection.search_count.side_effect = lambda model, domain, **kw: (
            1 if model == "res.partner" else 250
        )

        result = await handler._handle_list_record_attachments_tool("res.partner", 7)

        assert result["note"] == "Showing the newest 2 of 250 attachments."

    async def test_empty_list(self, handler, connection):
        connection.search_count.side_effect = lambda model, domain, **kw: (
            1 if model == "res.partner" else 0
        )
        connection.search_read.return_value = []

        result = await handler._handle_list_record_attachments_tool("res.partner", 7)

        assert result["attachments"] == []
        assert result["total"] == 0

    @pytest.mark.parametrize("model", ["res.partner", "ir.attachment"])
    async def test_access_refusal_on_either_model(self, handler, connection, access, model):
        def check(checked_model, operation):
            if checked_model == model:
                raise AccessControlError(f"{checked_model} not enabled")

        access.validate_model_access.side_effect = check

        with pytest.raises(ValidationError, match="not enabled"):
            await handler._handle_list_record_attachments_tool("res.partner", 7)
        connection.search_read.assert_not_called()

    async def test_missing_record(self, handler, connection):
        connection.search_count.side_effect = lambda model, domain, **kw: 0

        with pytest.raises(ValidationError, match="Record not found"):
            await handler._handle_list_record_attachments_tool("res.partner", 7)
        connection.search_read.assert_not_called()


class TestReadAttachment:
    """read_attachment: text, extracted text, image block, URL, or a download link."""

    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    @staticmethod
    def _serve(handler, connection, meta, content=None, mimetype=None, res_model="res.partner"):
        """Attachment 9 with ``meta``; resources/read answers ``content``."""
        from unittest.mock import AsyncMock

        from mcp.server.lowlevel.helper_types import ReadResourceContents

        def search_read(model, domain, fields=None, **kwargs):
            if fields == ["res_model"]:
                return [{"id": 9, "res_model": res_model}]
            return [{"id": 9, **meta}] if meta is not None else []

        connection.search_read.side_effect = search_read
        handler.app.read_resource = AsyncMock(
            return_value=[ReadResourceContents(content=content, mime_type=mimetype)]
        )

    async def test_text_file(self, handler, connection):
        meta = {"name": "a.txt", "mimetype": "text/plain", "file_size": 5, "type": "binary"}
        self._serve(handler, connection, meta, "hello", "text/plain")

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "text"
        assert result.structured_content["text"] == "hello"
        assert result.structured_content["download_url"].endswith("/web/content/9?download=true")
        assert "hello" in result.content[0].text
        handler.app.read_resource.assert_awaited_once_with("odoo://attachment/9")

    async def test_long_text_is_cut(self, handler, connection):
        from mcp_server_odoo.tools import READ_TEXT_MAX_CHARS

        meta = {"name": "a.txt", "mimetype": "text/plain", "file_size": 1000, "type": "binary"}
        self._serve(handler, connection, meta, "x" * (READ_TEXT_MAX_CHARS + 5), "text/plain")

        result = await handler._handle_read_attachment_tool("odoo://attachment/9", None)

        assert result.structured_content["truncated"] is True
        assert len(result.structured_content["text"]) == READ_TEXT_MAX_CHARS

    async def test_large_text_file_is_only_linked(self, handler, connection):
        meta = {"name": "a.log", "mimetype": "text/plain", "file_size": 5_000_000, "type": "binary"}
        self._serve(handler, connection, meta)

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "link"
        handler.app.read_resource.assert_not_awaited()

    async def test_pdf_returns_the_extracted_text(self, handler, connection):
        meta = {
            "name": "a.pdf",
            "mimetype": "application/pdf",
            "file_size": 90_000,
            "type": "binary",
            "index_content": "Invoice 42, total 100 EUR",
        }
        self._serve(handler, connection, meta)

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "extracted_text"
        assert result.structured_content["text"] == "Invoice 42, total 100 EUR"
        handler.app.read_resource.assert_not_awaited()

    async def test_pdf_without_extracted_text_is_only_linked(self, handler, connection):
        meta = {"name": "scan.pdf", "mimetype": "application/pdf", "file_size": 9, "type": "binary"}
        self._serve(handler, connection, {**meta, "index_content": False})

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "link"
        assert "no text" in result.structured_content["note"]

    async def test_small_image_comes_back_as_an_image_block(self, handler, connection):
        meta = {
            "name": "a.png",
            "mimetype": "image/png",
            "file_size": len(self.PNG),
            "type": "binary",
        }
        self._serve(handler, connection, meta, self.PNG, "image/png")

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "image"
        image = result.content[1]
        assert image.type == "image"
        assert image.mime_type == "image/png"
        assert base64.b64decode(image.data) == self.PNG

    async def test_large_image_is_only_linked(self, handler, connection):
        meta = {"name": "big.png", "mimetype": "image/png", "file_size": 900_000, "type": "binary"}
        self._serve(handler, connection, meta)

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "link"
        handler.app.read_resource.assert_not_awaited()

    async def test_url_attachment(self, handler, connection):
        meta = {
            "name": "site",
            "mimetype": False,
            "file_size": 0,
            "type": "url",
            "url": "https://x.y",
        }
        self._serve(handler, connection, meta)

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "url"
        assert result.structured_content["text"] == "https://x.y"

    async def test_other_types_are_only_linked(self, handler, connection):
        meta = {"name": "a.zip", "mimetype": "application/zip", "file_size": 10, "type": "binary"}
        self._serve(handler, connection, meta)

        result = await handler._handle_read_attachment_tool(None, 9)

        assert result.structured_content["kind"] == "link"
        handler.app.read_resource.assert_not_awaited()

    async def test_image_field_uri(self, handler, connection):
        meta = {"name": "image_1920", "mimetype": "image/png", "file_size": len(self.PNG)}
        self._serve(handler, connection, meta, self.PNG, "image/png")
        uri = "odoo://res.partner/record/3/image_1920"

        result = await handler._handle_read_attachment_tool(uri, None)

        assert result.structured_content["kind"] == "image"
        assert result.structured_content["download_url"].endswith(
            "/web/content/res.partner/3/image_1920?download=true"
        )
        handler.app.read_resource.assert_awaited_once_with(uri)

    async def test_field_uri_without_attachment_access_reads_the_content(
        self, handler, connection, access
    ):
        def check(model, operation):
            if model == "ir.attachment":
                raise AccessControlError("not enabled")

        access.validate_model_access.side_effect = check
        self._serve(handler, connection, None, self.PNG, "image/png")

        result = await handler._handle_read_attachment_tool(
            "odoo://res.partner/record/3/image_128", None
        )

        assert result.structured_content["kind"] == "image"

    async def test_attachment_on_an_inaccessible_model_is_refused(
        self, handler, connection, access
    ):
        def check(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("not enabled")

        access.validate_model_access.side_effect = check
        meta = {"name": "p.pdf", "mimetype": "application/pdf", "index_content": "salary"}
        self._serve(handler, connection, meta, res_model="hr.payslip")

        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_read_attachment_tool(None, 9)

    @pytest.mark.parametrize(
        "uri,attachment_id,message",
        [
            (None, None, "exactly one"),
            ("odoo://attachment/9", 9, "exactly one"),
            ("odoo://res.partner/record/3", None, "Pass an odoo://attachment"),
        ],
    )
    async def test_refuses_bad_targets(self, handler, uri, attachment_id, message):
        with pytest.raises(ValidationError, match=message):
            await handler._handle_read_attachment_tool(uri, attachment_id)

    async def test_missing_attachment(self, handler, connection):
        self._serve(handler, connection, None)

        with pytest.raises(ValidationError, match="Attachment not found"):
            await handler._handle_read_attachment_tool(None, 9)
