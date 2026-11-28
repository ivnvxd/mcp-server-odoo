"""Contract tests for the aggregate_records tool (totals and groupings).

The Odoo connection is mocked at the OdooConnection boundary, like the other
tool tests. Covers the read_group path (Odoo < 19) and its response
normalization, the formatted_read_group path (19+), the act-as-uid pinned path
(every RPC goes through res.users.mcp_execute_as_user) and the error mapping
for a model the pinned user cannot read.
"""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.access_control import AccessControlError
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnectionError
from mcp_server_odoo.schemas import AggregateResult
from mcp_server_odoo.tools import OdooToolHandler

FIELDS = {
    "partner_id": {"type": "many2one", "string": "Customer"},
    "date_order": {"type": "datetime", "string": "Order Date"},
    "amount_total": {"type": "monetary", "string": "Total"},
    "name": {"type": "char", "string": "Reference"},
    "state": {"type": "selection", "string": "Status"},
}

DENIED = (
    "Operation failed: You are not allowed to access 'Journal Entry' (account.move) "
    "records. This operation is allowed for the following groups: - Accounting/Billing"
)


def _make_handler(act_as_uid=None, major=17, rpc_result=None):
    tools = {}

    def tool_decorator(**kwargs):
        def register(func):
            tools[func.__name__] = func
            return func

        return register

    app = Mock()
    app.tool = Mock(side_effect=tool_decorator)

    connection = Mock()
    connection.is_authenticated = True
    connection.get_major_version.return_value = major
    connection.fields_get.return_value = FIELDS
    connection.execute_kw.return_value = rpc_result if rpc_result is not None else []

    def as_user(uid, model, method, args, kwargs):
        if method == "fields_get":
            return FIELDS
        return as_user.result

    as_user.result = rpc_result if rpc_result is not None else []
    connection.execute_kw_as_user = Mock(side_effect=as_user)

    access = Mock()
    config = OdooConfig(
        url="http://localhost:8069",
        api_key="test-key",
        act_as_uid=act_as_uid,
        default_limit=10,
        max_limit=100,
    )
    handler = OdooToolHandler(app, connection, access, config)
    return handler, connection, access, tools


def _rpc_calls(connection, method):
    """execute_kw / execute_kw_as_user calls for a given ORM method."""
    direct = [c for c in connection.execute_kw.call_args_list if c.args[1] == method]
    as_user = [c for c in connection.execute_kw_as_user.call_args_list if c.args[2] == method]
    return direct, as_user


class TestRegistration:
    def test_tool_is_registered(self):
        _, _, _, tools = _make_handler()
        assert "aggregate_records" in tools


class TestReadGroupPath:
    """Odoo 17 (foxlogik): read_group with response normalization."""

    @pytest.mark.asyncio
    async def test_groupby_month_with_sum(self):
        rows = [
            {
                "date_order:month": "October 2026",
                "__count": 3,
                "amount_total": 1500.0,
                "message_bounce": 0,  # unrequested aggregator field: dropped
                "__domain": [["state", "=", "sale"], ["date_order", ">=", "2026-10-01"]],
                "__range": {"date_order:month": {"from": "2026-10-01", "to": "2026-11-01"}},
            }
        ]
        handler, conn, _, _ = _make_handler(rpc_result=rows)

        result = await handler._handle_aggregate_records_tool(
            "sale.order",
            ["date_order:month"],
            ["amount_total:sum"],
            [["state", "=", "sale"]],
            "date_order:month desc",
            None,
            0,
        )

        conn.execute_kw.assert_called_once_with(
            "sale.order",
            "read_group",
            [[["state", "=", "sale"]]],
            {
                "fields": ["amount_total:sum"],
                "groupby": ["date_order:month"],
                "limit": 11,  # default_limit + 1 peek
                "offset": 0,
                "lazy": False,
                "orderby": "date_order:month desc",
            },
        )
        conn.execute_kw_as_user.assert_not_called()
        (group,) = result["groups"]
        assert group["amount_total:sum"] == 1500.0
        assert group["__count"] == 3
        assert group["__extra_domain"][0] == ["state", "=", "sale"]
        assert "__domain" not in group and "message_bounce" not in group
        assert "amount_total" not in group
        assert result["has_more"] is False and result["next_hint"] is None
        assert result["user_scoped"] is False
        AggregateResult(**result)  # matches the declared output schema

    @pytest.mark.asyncio
    async def test_default_aggregate_is_count(self):
        handler, conn, _, _ = _make_handler(rpc_result=[{"partner_id": [1, "A"], "__count": 2}])
        result = await handler._handle_aggregate_records_tool(
            "sale.order", ["partner_id"], None, None, None, None, 0
        )
        kwargs = conn.execute_kw.call_args.args[3]
        assert kwargs["fields"] == ["__count"]
        assert "orderby" not in kwargs
        assert result["aggregates"] == ["__count"]

    @pytest.mark.asyncio
    async def test_overall_row_without_groupby_gets_empty_extra_domain(self):
        handler, _, _, _ = _make_handler(rpc_result=[{"__count": 7, "amount_total": 10.0}])
        result = await handler._handle_aggregate_records_tool(
            "sale.order", None, ["__count", "amount_total:sum"], None, None, None, 0
        )
        assert result["groups"] == [{"__count": 7, "amount_total:sum": 10.0, "__extra_domain": []}]

    @pytest.mark.asyncio
    async def test_has_more_trims_the_peeked_row(self):
        rows = [{"partner_id": [i, f"P{i}"], "__count": 1} for i in range(4)]
        handler, conn, _, _ = _make_handler(rpc_result=rows)
        result = await handler._handle_aggregate_records_tool(
            "sale.order", ["partner_id"], None, None, None, 3, 6
        )
        kwargs = conn.execute_kw.call_args.args[3]
        assert kwargs["limit"] == 4 and kwargs["offset"] == 6
        assert len(result["groups"]) == 3
        assert result["has_more"] is True
        assert result["next_hint"] == "aggregate_records with offset=9, limit=3"

    @pytest.mark.asyncio
    async def test_json_string_arguments(self):
        handler, conn, _, _ = _make_handler(rpc_result=[])
        await handler._handle_aggregate_records_tool(
            "sale.order",
            '["partner_id", "date_order:year"]',
            '["amount_total:sum"]',
            '[["state", "=", "sale"]]',
            None,
            None,
            0,
        )
        args, kwargs = conn.execute_kw.call_args.args[2:4]
        assert args == [[["state", "=", "sale"]]]
        assert kwargs["groupby"] == ["partner_id", "date_order:year"]

    @pytest.mark.asyncio
    async def test_limit_is_capped(self):
        handler, conn, _, _ = _make_handler(rpc_result=[])
        await handler._handle_aggregate_records_tool(
            "sale.order", ["partner_id"], None, None, None, 5000, 0
        )
        assert conn.execute_kw.call_args.args[3]["limit"] == 101

    @pytest.mark.asyncio
    async def test_odoo16_order_uses_bare_aggregate_name(self):
        handler, conn, _, _ = _make_handler(major=16, rpc_result=[])
        await handler._handle_aggregate_records_tool(
            "sale.order",
            ["partner_id"],
            ["amount_total:sum"],
            None,
            "amount_total:sum desc",
            None,
            0,
        )
        assert conn.execute_kw.call_args.args[3]["orderby"] == "amount_total desc"


class TestValidation:
    @pytest.mark.asyncio
    async def test_aggregate_over_groupby_field_refused(self):
        handler, conn, _, _ = _make_handler()
        with pytest.raises(ValidationError, match="also a groupby key"):
            await handler._handle_aggregate_records_tool(
                "sale.order", ["partner_id"], ["partner_id:count_distinct"], None, None, None, 0
            )
        conn.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_two_aggregates_on_one_field_refused(self):
        handler, _, _, _ = _make_handler()
        with pytest.raises(ValidationError, match="one aggregate per field"):
            await handler._handle_aggregate_records_tool(
                "sale.order",
                ["partner_id"],
                ["amount_total:sum", "amount_total:avg"],
                None,
                None,
                None,
                0,
            )

    @pytest.mark.asyncio
    async def test_sum_on_char_field_refused(self):
        handler, _, _, _ = _make_handler()
        with pytest.raises(ValidationError, match="'name' is a char field"):
            await handler._handle_aggregate_records_tool(
                "sale.order", ["partner_id"], ["name:sum"], None, None, None, 0
            )

    @pytest.mark.asyncio
    async def test_unknown_groupby_field_refused_before_rpc(self):
        handler, conn, _, _ = _make_handler()
        with pytest.raises(ValidationError, match="partner"):
            await handler._handle_aggregate_records_tool(
                "sale.order", ["partnr_id"], None, None, None, None, 0
            )
        conn.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_limit_refused(self):
        handler, _, _, _ = _make_handler()
        with pytest.raises(ValidationError, match="limit"):
            await handler._handle_aggregate_records_tool(
                "sale.order", ["partner_id"], None, None, None, -1, 0
            )

    @pytest.mark.asyncio
    async def test_mcp_access_control_refusal(self):
        handler, conn, access, _ = _make_handler()
        access.validate_model_access.side_effect = AccessControlError("model not enabled")
        with pytest.raises(ValidationError, match="Access denied"):
            await handler._handle_aggregate_records_tool(
                "hr.payslip", None, None, None, None, None, 0
            )
        conn.execute_kw.assert_not_called()


class TestFormattedReadGroupPath:
    """Odoo 19+: formatted_read_group, rows returned as-is."""

    @pytest.mark.asyncio
    async def test_uses_formatted_read_group(self):
        rows = [{"partner_id": [1, "A"], "__count": 2, "amount_total:sum": 5.0}]
        handler, conn, _, _ = _make_handler(major=19, rpc_result=rows)
        result = await handler._handle_aggregate_records_tool(
            "sale.order",
            ["partner_id"],
            ["__count", "amount_total:sum"],
            None,
            "amount_total:sum desc",
            None,
            0,
        )
        conn.execute_kw.assert_called_once_with(
            "sale.order",
            "formatted_read_group",
            [[]],
            {
                "groupby": ["partner_id"],
                "aggregates": ["__count", "amount_total:sum"],
                "limit": 11,
                "offset": 0,
                "order": "amount_total:sum desc",
            },
        )
        assert result["groups"] == rows


class TestPinnedPath:
    """ODOO_ACT_AS_UID: aggregation runs as the pinned user via mcp_execute_as_user."""

    @pytest.mark.asyncio
    async def test_tool_routes_read_group_through_pinned_user(self):
        rows = [{"partner_id": [3, "C"], "__count": 1, "__domain": [["partner_id", "=", 3]]}]
        handler, conn, _, tools = _make_handler(act_as_uid=42, rpc_result=rows)

        # Through the registered tool: a model-supplied user_id is clamped to 42.
        result = await tools["aggregate_records"](
            model="sale.order", groupby=["partner_id"], user_id=1
        )

        _, as_user = _rpc_calls(conn, "read_group")
        assert len(as_user) == 1
        uid, model, method, args, kwargs = as_user[0].args
        assert (uid, model, method) == (42, "sale.order", "read_group")
        assert args == [[]]
        assert kwargs["groupby"] == ["partner_id"] and kwargs["lazy"] is False
        # Never via the admin connection directly.
        conn.execute_kw.assert_not_called()
        assert isinstance(result, AggregateResult)
        assert result.user_scoped is True
        assert result.groups[0]["__extra_domain"] == [["partner_id", "=", 3]]

    @pytest.mark.asyncio
    async def test_type_check_uses_pinned_users_fields_get(self):
        handler, conn, _, _ = _make_handler(act_as_uid=42)
        await handler._handle_aggregate_records_tool(
            "sale.order",
            ["partner_id"],
            ["amount_total:sum"],
            None,
            None,
            None,
            0,
            user_id=42,
        )
        _, fields_get_calls = _rpc_calls(conn, "fields_get")
        assert fields_get_calls and all(c.args[0] == 42 for c in fields_get_calls)
        conn.fields_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_odoo19_pinned_uses_formatted_read_group_as_user(self):
        handler, conn, _, _ = _make_handler(act_as_uid=42, major=19)
        await handler._handle_aggregate_records_tool(
            "sale.order", ["partner_id"], None, None, None, None, 0, user_id=42
        )
        _, as_user = _rpc_calls(conn, "formatted_read_group")
        assert len(as_user) == 1 and as_user[0].args[0] == 42
        conn.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_model_pinned_user_cannot_read_is_refused(self):
        handler, conn, _, tools = _make_handler(act_as_uid=42)

        def as_user(uid, model, method, args, kwargs):
            if method == "fields_get":
                return {}
            raise OdooConnectionError(DENIED)

        conn.execute_kw_as_user.side_effect = as_user
        with pytest.raises(ValidationError) as exc:
            await tools["aggregate_records"](model="account.move", groupby=["partner_id"])
        message = str(exc.value)
        assert "Access denied: user 42 cannot read account.move" in message
        assert "Accounting/Billing" in message
        conn.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_proxy_without_allowlisted_method_is_explained(self):
        handler, conn, _, _ = _make_handler(act_as_uid=42, major=19)

        def as_user(uid, model, method, args, kwargs):
            if method == "fields_get":
                return FIELDS
            raise OdooConnectionError(
                "Operation failed: Method 'formatted_read_group' is not permitted via "
                "MCP impersonation."
            )

        conn.execute_kw_as_user.side_effect = as_user
        with pytest.raises(ValidationError, match="foxlogik_mcp_proxy"):
            await handler._handle_aggregate_records_tool(
                "sale.order", ["partner_id"], None, None, None, None, 0, user_id=42
            )
