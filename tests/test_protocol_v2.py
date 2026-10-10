"""Protocol-level behavior through the in-memory MCP client.

The unit tests call handlers on a mocked app and cannot see what reaches a
client: error text, mimeTypes, instructions. These tests drive the real server
object through ``mcp.Client``, in the legacy handshake and in the protocol
mode the client picks by default.
"""

import base64
from unittest.mock import MagicMock, patch

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from mcp_server_odoo import __version__
from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.server import OdooMCPServer

MODES = ["legacy", "auto"]

# 1x1 transparent PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def server():
    """A server on an authenticated mock connection and an allow-all controller."""
    connection = MagicMock(spec=OdooConnection)
    connection.is_authenticated = True
    connection.get_major_version.return_value = 19
    with (
        patch("mcp_server_odoo.server.OdooConnection", return_value=connection),
        patch(
            "mcp_server_odoo.server.AccessController",
            return_value=MagicMock(spec=AccessController),
        ),
    ):
        server = OdooMCPServer(
            OdooConfig(url="http://localhost:8069", api_key="k", database="test_db")
        )
        server._mock_connection = connection
        yield server


async def _read_resource_error(app, mode, uri) -> MCPError:
    """The protocol error a resources/read answers with (raised after the client closes)."""
    async with Client(app, mode=mode) as client:
        try:
            await client.read_resource(uri)
        except MCPError as e:
            error = e
        else:
            pytest.fail(f"{uri} read without an error")
    return error


@pytest.mark.parametrize("mode", MODES)
async def test_tool_error_text_reaches_the_client(server, mode):
    async with Client(server.app, mode=mode) as client:
        result = await client.call_tool("search_records", {"model": "res.partner", "domain": 5})

    assert result.is_error
    assert "Domain must be a list" in result.content[0].text


@pytest.mark.parametrize("mode", MODES)
async def test_resource_not_found_text_and_code_reach_the_client(server, mode):
    server._mock_connection.search.return_value = []

    error = await _read_resource_error(server.app, mode, "odoo://res.partner/record/999")

    assert "Record not found" in str(error)
    assert error.code == INVALID_PARAMS


@pytest.mark.parametrize("mode", MODES)
async def test_binary_resource_has_its_own_mimetype(server, mode):
    connection = server._mock_connection
    connection.fields_get.return_value = {"image_128": {"type": "binary", "store": True}}
    payload = base64.b64encode(PNG_BYTES).decode("ascii")

    def search_read(model, domain, fields=None, **kwargs):
        if model == "ir.attachment":
            return [{"id": 9, "mimetype": "image/png"}]
        context = kwargs.get("context") or {}
        return [{"id": 1, "image_128": "70.00 bytes" if context.get("bin_size") else payload}]

    connection.search_read.side_effect = search_read

    async with Client(server.app, mode=mode) as client:
        result = await client.read_resource("odoo://res.partner/record/1/image_128")

    content = result.contents[0]
    assert content.mime_type == "image/png"
    assert base64.b64decode(content.blob) == PNG_BYTES


@pytest.mark.parametrize("mode", MODES)
async def test_dynamic_instructions_and_version_reach_the_client(server, mode):
    with patch(
        "mcp_server_odoo.server.build_user_context",
        return_value="You are connected to Odoo via MCP as:\n- User: Test",
    ):
        await server._apply_dynamic_instructions()

    async with Client(server.app, mode=mode) as client:
        instructions = client.instructions
        version = client.server_info.version

    assert instructions.startswith("MCP server for accessing and managing Odoo ERP data")
    assert "- User: Test" in instructions
    assert version == __version__


@pytest.mark.parametrize("mode", MODES)
async def test_unknown_argument_is_refused_by_name(server, mode):
    """The SDK drops unknown arguments silently; a misspelled limit would run with the default."""
    async with Client(server.app, mode=mode) as client:
        result = await client.call_tool(
            "search_records", {"model": "res.partner", "limt": 5, "filter": []}
        )

    assert result.is_error
    text = result.content[0].text
    assert "Unknown argument(s) for search_records: filter, limt" in text
    assert "limit" in text.split("Valid arguments:")[1]
    server._mock_connection.search.assert_not_called()


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("get_record", {"model": "res.partner", "record_id": True}),
        ("delete_record", {"model": "res.partner", "record_id": False}),
        (
            "update_records",
            {"model": "res.partner", "record_ids": [5, True], "values": {"name": "X"}},
        ),
        ("read_attachment", {"attachment_id": True}),
    ],
)
async def test_boolean_id_is_refused(server, tool, arguments):
    """Pydantic reads True as 1: record_id=true would act on record 1."""
    async with Client(server.app, mode="legacy") as client:
        result = await client.call_tool(tool, arguments)

    assert result.is_error
    assert "not a boolean" in result.content[0].text
    connection = server._mock_connection
    connection.read.assert_not_called()
    connection.write.assert_not_called()
    connection.unlink.assert_not_called()


async def test_digit_string_id_stays_valid(server):
    connection = server._mock_connection
    connection.fields_get.return_value = {"name": {"type": "char"}}
    connection.read.return_value = [{"id": 7, "name": "Seven"}]

    async with Client(server.app, mode="legacy") as client:
        result = await client.call_tool(
            "get_record", {"model": "res.partner", "record_id": "7", "fields": ["name"]}
        )

    assert not result.is_error, result.content
    assert connection.read.call_args.args[1] == [7]


async def test_search_reads_25_records_by_default(server):
    connection = server._mock_connection
    connection.search.return_value = []
    connection.search_count.return_value = 0

    async with Client(server.app, mode="legacy") as client:
        result = await client.call_tool("search_records", {"model": "res.partner"})

    assert not result.is_error, result.content
    assert connection.search.call_args.kwargs["limit"] == 25
