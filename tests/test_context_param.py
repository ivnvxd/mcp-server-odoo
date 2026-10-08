"""The per-call ``context`` parameter (issue #129).

Only allowlisted keys pass. The context reaches every query whose result the
tool returns: the search, the count, the read, the existence check and the
read-back after a write. Security gates keep their fixed context, and the
tool's own keys (bin_size, the existence check's active_test=False) win.
"""

from unittest.mock import MagicMock

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection, OdooValidationFault
from mcp_server_odoo.tools import OdooToolHandler

USER_COMPANIES = [1, 2, 5]
COMPANY_5 = {"allowed_company_ids": [5]}


@pytest.fixture
def connection():
    connection = MagicMock(spec=OdooConnection)
    connection.is_authenticated = True
    connection.uid = 2
    connection.get_major_version.return_value = 19
    connection.fields_get.return_value = {
        "id": {"type": "integer"},
        "name": {"type": "char", "required": True},
        "standard_price": {"type": "float"},
        "tag_ids": {"type": "many2many", "relation": "x.tag"},
    }
    connection.search.return_value = [7]
    connection.search_count.return_value = 1
    connection.create.return_value = 7
    connection.create_many.return_value = [7, 8]
    connection.write.return_value = True
    connection.web_save_multi.return_value = [{"id": 7, "display_name": "Seven"}]
    connection.build_record_url.return_value = "http://odoo/x/7"

    def read(model, ids, fields=None, context=None):
        if model == "x.tag":
            return [{"id": i, "display_name": f"Tag {i}"} for i in ids]
        return [{"id": i, "name": f"N{i}", "display_name": f"N{i}", "tag_ids": [3]} for i in ids]

    connection.read.side_effect = read

    def execute_kw(model, method, args, kwargs, scoped=True):
        if (model, method) == ("res.users", "read"):
            return [{"id": 2, "company_ids": USER_COMPANIES}]
        return []

    connection.execute_kw.side_effect = execute_kw
    # The real read of the user's companies, through the execute_kw above
    connection.user_company_ids.side_effect = lambda: OdooConnection.user_company_ids(connection)
    return connection


def _handler(connection, **config):
    config = OdooConfig(url="http://localhost:8069", api_key="k", database="d", **config)
    return OdooToolHandler(
        MagicMock(spec=MCPServer), connection, MagicMock(spec=AccessController), config
    )


@pytest.fixture
def handler(connection):
    return _handler(connection)


def _contexts(mock):
    """The context of each call of a connection method mock."""
    return [
        call.kwargs.get("context", call.args[3] if len(call.args) > 3 else None)
        for call in mock.call_args_list
    ]


class TestAllowlist:
    @pytest.mark.parametrize(
        "context",
        [
            {"lang": "de_DE"},
            {"tz": "Europe/Berlin"},
            {"active_test": False},
            COMPANY_5,
            {"lang": "fr_FR", "allowed_company_ids": [5, 1]},
        ],
    )
    async def test_each_key_reaches_search_count_and_read(self, handler, connection, context):
        await handler._handle_search_tool("x.model", None, ["name"], 10, 0, None, context=context)

        assert connection.search.call_args.kwargs["context"] == context
        assert connection.search_count.call_args.kwargs["context"] == context
        assert connection.read.call_args.args[3] == {**context, "bin_size": True}

    @pytest.mark.parametrize("key", ["default_partner_id", "tracking_disable", "uid"])
    async def test_other_keys_are_refused_by_name(self, handler, connection, key):
        with pytest.raises(ValidationError, match=f"Unknown context key\\(s\\): {key}"):
            await handler._handle_search_tool("x.model", None, None, 10, 0, None, context={key: 1})
        connection.search.assert_not_called()

    async def test_company_id_points_to_allowed_company_ids(self, handler):
        with pytest.raises(ValidationError, match="Use allowed_company_ids: its first ID"):
            await handler._handle_get_record_tool("x.model", 7, ["name"], context={"company_id": 5})

    @pytest.mark.parametrize(
        "context,message",
        [
            ({"active_test": "no"}, "active_test must be true or false"),
            ({"lang": ""}, "lang must be a non-empty string"),
            ({"tz": 5}, "tz must be a non-empty string"),
            ({"allowed_company_ids": []}, "non-empty list of company IDs"),
            ({"allowed_company_ids": [True]}, "non-empty list of company IDs"),
            ({"allowed_company_ids": ["5"]}, "non-empty list of company IDs"),
            ({"allowed_company_ids": 5}, "non-empty list of company IDs"),
            ("lang=de_DE", "context must be an object"),
        ],
    )
    async def test_bad_values_are_refused(self, handler, connection, context, message):
        with pytest.raises(ValidationError, match=message):
            await handler._handle_search_tool("x.model", None, None, 10, 0, None, context=context)
        connection.search.assert_not_called()

    async def test_no_context_changes_nothing(self, handler, connection):
        await handler._handle_search_tool("x.model", None, ["name"], 10, 0, None)

        assert "context" not in connection.search.call_args.kwargs
        assert "context" not in connection.search_count.call_args.kwargs
        assert connection.read.call_args.args[3] == {"bin_size": True}


class TestCompanyLimit:
    async def test_a_company_of_the_user_passes(self, handler, connection):
        await handler._handle_search_tool("x.model", None, ["name"], 10, 0, None, context=COMPANY_5)

        # the user's companies are read without the ODOO_ALLOWED_COMPANIES scoping
        users_read = connection.execute_kw.call_args_list[0]
        assert users_read.args[:2] == ("res.users", "read")
        assert users_read.kwargs == {"scoped": False}

    async def test_a_company_outside_the_users_is_refused(self, handler, connection):
        with pytest.raises(ValidationError, match=r"\[9\] are outside the user's companies"):
            await handler._handle_search_tool(
                "x.model", None, None, 10, 0, None, context={"allowed_company_ids": [5, 9]}
            )
        connection.search.assert_not_called()

    async def test_a_company_outside_the_configured_limit_is_refused(self, connection):
        handler = _handler(connection, allowed_companies=[1, 2])

        with pytest.raises(ValidationError, match=r"\[5\] are outside ODOO_ALLOWED_COMPANIES"):
            await handler._handle_search_tool("x.model", None, None, 10, 0, None, context=COMPANY_5)
        connection.search.assert_not_called()

    async def test_unreadable_user_companies_leave_the_check_to_odoo(self, handler, connection):
        """Standard mode: the MCP module may not allow res.users."""
        connection.execute_kw.side_effect = OdooValidationFault("res.users not enabled", 403)

        await handler._handle_search_tool("x.model", None, ["name"], 10, 0, None, context=COMPANY_5)

        assert connection.search.call_args.kwargs["context"] == COMPANY_5

    async def test_a_refused_record_is_not_probed_field_by_field(self, handler, connection):
        """A foreign company refuses every field. A read of only id shows it in one call."""
        connection.read.side_effect = OdooValidationFault(
            "Access to unauthorized or invalid companies.", 4
        )

        with pytest.raises(ValidationError, match="unauthorized or invalid companies"):
            await handler._handle_search_tool(
                "x.model", None, ["__all__"], 10, 0, None, context=COMPANY_5
            )
        # the bulk read and the read of only id, no halving probe
        assert connection.read.call_count == 2
        assert connection.read.call_args.args[2] == ["id"]

    async def test_refused_fields_are_cached_per_company(self, handler, connection):
        def read(model, ids, fields=None, context=None):
            names = fields or ["id", "name", "standard_price"]
            if "standard_price" in names and (context or {}).get("allowed_company_ids") == [5]:
                raise OdooValidationFault("refused", 4)
            return [{"id": i, **{n: 1 for n in names}} for i in ids]

        connection.read.side_effect = read
        result = await handler._handle_search_tool(
            "x.model", None, ["__all__"], 10, 0, None, context=COMPANY_5
        )
        assert result["skipped_fields"] == ["standard_price"]

        result = await handler._handle_search_tool("x.model", None, ["__all__"], 10, 0, None)

        assert result["skipped_fields"] is None


class TestLang:
    """Odoo 16 and 17 ignore a lang they have not installed; the tool refuses it."""

    @pytest.fixture(autouse=True)
    def odoo_17(self, connection):
        connection.get_major_version.return_value = 17
        connection.search_read.return_value = [{"code": "en_US"}]

    async def test_an_uninstalled_lang_is_refused_before_the_write(self, handler, connection):
        with pytest.raises(ValidationError, match="Language 'fr_FR' is not installed"):
            await handler._handle_update_record_tool(
                "x.model", 7, {"name": "Chaise"}, context={"lang": "fr_FR"}
            )
        connection.write.assert_not_called()

    async def test_an_installed_lang_passes(self, handler, connection):
        await handler._handle_update_record_tool(
            "x.model", 7, {"name": "Chair"}, context={"lang": "en_US"}
        )

        assert connection.write.called

    async def test_a_lang_installed_since_the_cache_passes(self, handler, connection):
        handler._active_langs = {"en_US"}
        connection.search_read.return_value = [{"code": "en_US"}, {"code": "fr_FR"}]

        await handler._handle_update_record_tool(
            "x.model", 7, {"name": "Chaise"}, context={"lang": "fr_FR"}
        )

        assert connection.write.called

    async def test_an_unreadable_language_list_leaves_it_to_odoo(self, handler, connection):
        connection.search_read.side_effect = OdooValidationFault("res.lang not enabled", 403)

        await handler._handle_update_record_tool(
            "x.model", 7, {"name": "Chaise"}, context={"lang": "fr_FR"}
        )

        assert connection.write.called

    async def test_odoo_18_refuses_it_itself(self, handler, connection):
        connection.get_major_version.return_value = 18

        await handler._handle_update_record_tool(
            "x.model", 7, {"name": "Chaise"}, context={"lang": "fr_FR"}
        )

        connection.search_read.assert_not_called()


class TestReads:
    async def test_get_record_read_and_related_names_take_the_context(self, handler, connection):
        await handler._handle_get_record_tool("x.model", 7, ["name", "tag_ids"], context=COMPANY_5)

        assert _contexts(connection.read) == [{**COMPANY_5, "bin_size": True}, COMPANY_5]

    async def test_the_attachment_gate_keeps_its_fixed_context(self, handler, connection):
        connection.search_read.return_value = [{"id": 7, "res_model": "x.model"}]

        await handler._handle_get_record_tool("ir.attachment", 7, ["name"], context=COMPANY_5)

        assert connection.search_read.call_args.kwargs["context"] == {"active_test": False}

    async def test_aggregate_formatted_read_group(self, handler, connection):
        await handler._handle_aggregate_records_tool(
            "x.model", ["name"], None, None, None, None, 0, context=COMPANY_5
        )

        method, kwargs = (
            connection.execute_kw.call_args.args[1],
            connection.execute_kw.call_args.args[3],
        )
        assert method == "formatted_read_group"
        assert kwargs["context"] == COMPANY_5

    async def test_aggregate_read_group_before_odoo_19(self, handler, connection):
        connection.get_major_version.return_value = 18

        await handler._handle_aggregate_records_tool(
            "x.model", ["name"], None, None, None, None, 0, context=COMPANY_5
        )

        method, kwargs = (
            connection.execute_kw.call_args.args[1],
            connection.execute_kw.call_args.args[3],
        )
        assert method == "read_group"
        assert kwargs["context"] == COMPANY_5


class TestWrites:
    async def test_create_record_and_its_read_back(self, handler, connection):
        await handler._handle_create_record_tool("x.model", {"name": "A"}, context=COMPANY_5)

        assert connection.create.call_args.kwargs["context"] == COMPANY_5
        assert connection.read.call_args.kwargs["context"] == COMPANY_5

    async def test_create_records_and_their_read_back(self, handler, connection):
        await handler._handle_create_records_tool(
            "x.model", [{"name": "A"}, {"name": "B"}], context=COMPANY_5
        )

        assert connection.create_many.call_args.kwargs["context"] == COMPANY_5
        assert connection.read.call_args.kwargs["context"] == COMPANY_5

    async def test_update_record_check_write_and_read_back(self, handler, connection):
        """#129: under another company the record may be invisible, or read back
        another company's value, unless all three share the context."""
        await handler._handle_update_record_tool(
            "x.model", 7, {"standard_price": 12.5}, context=COMPANY_5
        )

        assert connection.search_count.call_args.kwargs["context"] == {
            **COMPANY_5,
            "active_test": False,
        }
        assert connection.write.call_args.kwargs["context"] == COMPANY_5
        assert connection.read.call_args.kwargs["context"] == COMPANY_5

    async def test_update_by_id_finds_an_archived_record_even_with_active_test(
        self, handler, connection
    ):
        """An update names its records by ID: the caller's active_test cannot hide one."""
        await handler._handle_update_record_tool(
            "x.model", 7, {"name": "B"}, context={"active_test": True}
        )

        assert connection.search_count.call_args.kwargs["context"] == {"active_test": False}
        assert connection.write.call_args.kwargs["context"] == {"active_test": True}

    async def test_update_records_shared_values(self, handler, connection):
        connection.search.return_value = [7, 8]

        await handler._handle_update_records_tool(
            "x.model", [7, 8], {"name": "B"}, context=COMPANY_5
        )

        assert connection.search.call_args.kwargs["context"] == {**COMPANY_5, "active_test": False}
        assert connection.write.call_args.kwargs["context"] == COMPANY_5
        assert connection.read.call_args.kwargs["context"] == COMPANY_5

    async def test_update_records_per_record_values(self, handler, connection):
        await handler._handle_update_records_each_tool(
            "x.model", [{"id": 7, "values": {"name": "B"}}], context=COMPANY_5
        )

        assert connection.search.call_args.kwargs["context"] == {**COMPANY_5, "active_test": False}
        assert connection.web_save_multi.call_args.kwargs["context"] == COMPANY_5


class TestProtocol:
    TOOLS = (
        "search_records",
        "get_record",
        "aggregate_records",
        "create_record",
        "create_records",
        "update_record",
        "update_records",
    )

    @pytest.fixture
    def app(self, connection):
        app = MCPServer("context-test")
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        OdooToolHandler(app, connection, MagicMock(spec=AccessController), config)
        return app

    async def test_the_seven_tools_take_a_context(self, app):
        tools = {tool.name: tool.input_schema["properties"] for tool in await app.list_tools()}

        assert {name for name, props in tools.items() if "context" in props} == set(self.TOOLS)
        schema = tools["update_record"]["context"]["anyOf"][0]
        assert set(schema["properties"]) == {"lang", "tz", "active_test", "allowed_company_ids"}
        assert schema["additionalProperties"] is False

    async def test_context_through_the_client(self, app, connection):
        async with Client(app, mode="legacy") as client:
            result = await client.call_tool(
                "update_record",
                {
                    "model": "x.model",
                    "record_id": 7,
                    "values": {"standard_price": 12.5},
                    "context": COMPANY_5,
                },
            )

        assert not result.is_error, result.content
        assert connection.write.call_args.kwargs["context"] == COMPANY_5

    async def test_unknown_key_through_the_client(self, app, connection):
        async with Client(app, mode="legacy") as client:
            result = await client.call_tool(
                "get_record",
                {"model": "x.model", "record_id": 7, "context": {"allowed_company_id": [5]}},
            )

        assert result.is_error
        assert "Unknown context key(s): allowed_company_id" in result.content[0].text
        connection.read.assert_not_called()
