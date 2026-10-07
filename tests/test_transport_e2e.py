"""Every tool over the transport the environment selects, against a live Odoo.

The configuration takes the API key, with ``ODOO_RPC_TRANSPORT`` (default
``auto``). On Odoo 19 and later ``auto`` uses JSON-2, before that XML-RPC.
``ODOO_EXPECT_RPC_TRANSPORT`` makes a CI leg fail unless that transport is the
one in use: a leg named after JSON-2 must not pass over XML-RPC.
"""

import base64
import os

import pytest
from mcp import Client

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.server import OdooMCPServer

pytestmark = [
    pytest.mark.yolo,
    pytest.mark.skipif(not os.getenv("ODOO_API_KEY"), reason="needs ODOO_API_KEY"),
]


@pytest.fixture
async def server():
    config = OdooConfig(
        url=os.getenv("ODOO_URL", "http://localhost:8069"),
        api_key=os.getenv("ODOO_API_KEY"),
        # Only for the XML-RPC fallback of "auto" before Odoo 19
        username=os.getenv("ODOO_USER"),
        database=os.getenv("ODOO_DB"),
        yolo_mode="true",
        enable_method_calls=True,
        rpc_transport=os.getenv("ODOO_RPC_TRANSPORT", "auto"),
    )
    server = OdooMCPServer(config)
    await server.ensure_connected()
    yield server
    server.connection.disconnect()


async def call(server, tool, arguments):
    async with Client(server.app, mode="legacy") as client:
        result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content


def test_the_expected_transport(server):
    expected = os.getenv("ODOO_EXPECT_RPC_TRANSPORT")
    if not expected:
        pytest.skip("ODOO_EXPECT_RPC_TRANSPORT is not set")
    assert server.connection.rpc_transport == expected


def test_health_names_the_transport(server):
    connection = server.get_health_status()["connection"]
    assert connection == {"connected": True, "rpc_transport": server.connection.rpc_transport}


async def test_record_lifecycle(server):
    created = await call(
        server, "create_record", {"model": "res.partner", "values": {"name": "Transport E2E"}}
    )
    partner_id = created["record"]["id"]
    try:
        found = await call(
            server,
            "search_records",
            {"model": "res.partner", "domain": [["id", "=", partner_id]], "fields": ["name"]},
        )
        assert found["records"] == [{"id": partner_id, "name": "Transport E2E"}]
        assert found["total"] == 1

        record = await call(server, "get_record", {"model": "res.partner", "record_id": partner_id})
        assert record["record"]["name"] == "Transport E2E"

        await call(
            server,
            "update_record",
            {"model": "res.partner", "record_id": partner_id, "values": {"phone": "+1 555"}},
        )
        fields = await call(
            server,
            "search_records",
            {
                "model": "res.partner",
                "domain": [["id", "=", partner_id]],
                "fields": ["phone"],
                "context": {"active_test": False},
            },
        )
        assert fields["records"][0]["phone"] == "+1 555"

        posted = await call(
            server,
            "post_message",
            {"model": "res.partner", "record_id": partner_id, "body": "Transport note"},
        )
        assert isinstance(posted["message_id"], int)
    finally:
        await call(server, "delete_record", {"model": "res.partner", "record_id": partner_id})


async def test_batch_writes(server):
    created = await call(
        server,
        "create_records",
        {"model": "res.partner", "records": [{"name": "Transport A"}, {"name": "Transport B"}]},
    )
    ids = [record["id"] for record in created["records"]]
    try:
        assert created["created_count"] == 2
        shared = await call(
            server,
            "update_records",
            {"model": "res.partner", "record_ids": ids, "values": {"phone": "7"}},
        )
        assert shared["updated_count"] == 2

        if (server.connection.get_major_version() or 0) >= 19:
            each = await call(
                server,
                "update_records",
                {
                    "model": "res.partner",
                    "updates": [
                        {"id": ids[0], "values": {"phone": "1"}},
                        {"id": ids[1], "values": {"phone": "2"}},
                    ],
                },
            )
            assert [r["id"] for r in each["records"]] == ids
    finally:
        for record_id in ids:
            await call(server, "delete_record", {"model": "res.partner", "record_id": record_id})


async def test_attachments(server):
    created = await call(
        server, "create_record", {"model": "res.partner", "values": {"name": "Transport Files"}}
    )
    partner_id = created["record"]["id"]
    try:
        uploaded = await call(
            server,
            "upload_attachment",
            {
                "model": "res.partner",
                "record_id": partner_id,
                "name": "note.txt",
                "data": base64.b64encode(b"hello transport").decode(),
                "mimetype": "text/plain",
            },
        )
        listed = await call(
            server, "list_record_attachments", {"model": "res.partner", "record_id": partner_id}
        )
        assert [a["name"] for a in listed["attachments"]] == ["note.txt"]

        read = await call(server, "read_attachment", {"uri": uploaded["uri"]})
        assert read["kind"] == "text"
        assert read["text"] == "hello transport"
    finally:
        await call(server, "delete_record", {"model": "res.partner", "record_id": partner_id})


async def test_reads_and_aggregates(server):
    fields = await call(
        server, "get_fields", {"model": "res.partner", "field_names": ["name", "email"]}
    )
    assert {f["name"] for f in fields["fields"]} == {"name", "email"}

    grouped = await call(
        server, "aggregate_records", {"model": "res.partner", "groupby": ["active"]}
    )
    assert sum(group["__count"] for group in grouped["groups"]) > 0

    context = await call(server, "get_current_context", {})
    assert context["user_name"]


async def test_call_model_method(server):
    created = await call(
        server, "create_record", {"model": "res.partner", "values": {"name": "Transport Method"}}
    )
    partner_id = created["record"]["id"]
    try:
        archived = await call(
            server,
            "call_model_method",
            {"model": "res.partner", "method": "action_archive", "arguments": [[partner_id]]},
        )
        assert archived["success"] is True
        found = await call(
            server,
            "search_records",
            {
                "model": "res.partner",
                "domain": [["id", "=", partner_id], ["active", "=", False]],
                "fields": ["name"],
                "context": {"active_test": False},
            },
        )
        assert found["total"] == 1
    finally:
        await call(server, "delete_record", {"model": "res.partner", "record_id": partner_id})


async def test_errors_keep_their_text(server):
    async with Client(server.app, mode="legacy") as client:
        missing = await client.call_tool(
            "get_record", {"model": "res.partner", "record_id": 2_000_000_000}
        )
        invalid = await client.call_tool(
            "search_records", {"model": "res.partner", "domain": [["nope", "=", 1]]}
        )

    assert missing.is_error and "Record not found" in missing.content[0].text
    assert invalid.is_error and "Invalid field" in invalid.content[0].text


async def test_resources(server):
    async with Client(server.app, mode="legacy") as client:
        record = await client.read_resource("odoo://res.partner/record/1")
        count = await client.read_resource("odoo://res.partner/count")

    assert "res.partner/1" in record.contents[0].text
    assert count.contents
