"""Test suite for the dashboard MCP tools."""

import json
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import NotFoundError, ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.tools import OdooToolHandler

SPEC = {
    "filters": [{"type": "date", "label": "Period", "default": "last_90_days"}],
    "widgets": [
        {
            "type": "kpi",
            "title": "Untaxed total",
            "model": "sale.order",
            "measure": "amount_untaxed",
            "domain": [["state", "=", "sale"]],
        },
        {
            "type": "chart",
            "chart": "line",
            "title": "Orders per month",
            "model": "sale.order",
            "measure": "amount_untaxed",
            "group_by": ["date_order:month"],
        },
    ],
}

SALE_FIELDS = {
    "date_order": {"type": "datetime", "store": True, "string": "Order Date"},
    "partner_id": {"type": "many2one", "relation": "res.partner", "string": "Customer"},
    "amount_untaxed": {"type": "monetary", "store": True, "string": "Untaxed Amount"},
}


class TestDashboardTools:
    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        connection.server_version = "18.0"
        connection.get_major_version.return_value = 18
        connection.fields_get.return_value = SALE_FIELDS
        connection.build_record_url.return_value = (
            "http://localhost:8069/web#id=1&model=spreadsheet.dashboard&view_type=form"
        )
        return connection

    @pytest.fixture
    def handler(self, mock_app, mock_connection):
        return OdooToolHandler(
            mock_app,
            mock_connection,
            MagicMock(spec=AccessController),
            OdooConfig(url="http://localhost:8069", api_key="k", database="db"),
        )

    @pytest.mark.asyncio
    async def test_dry_run_builds_without_writing(self, handler, mock_connection):
        result = await handler._handle_write_dashboard_tool(SPEC, dry_run=True)

        assert result["success"] is True
        assert result["created"] is False
        assert result["summary"]["scorecards"] == ["Untaxed total"]
        assert result["summary"]["models"] == ["sale.order"]
        mock_connection.create.assert_not_called()
        mock_connection.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_stores_the_generated_document(self, handler, mock_connection):
        mock_connection.search.return_value = [3]
        mock_connection.create.return_value = 7

        result = await handler._handle_write_dashboard_tool(SPEC, name="Sales", group="Finance")

        assert result == {
            "success": True,
            "dashboard_id": 7,
            "url": "http://localhost:8069/odoo/dashboards",
            "created": True,
            "summary": result["summary"],
            "message": "Created dashboard 7 with 2 widgets.",
        }
        model, values = mock_connection.create.call_args[0]
        assert model == "spreadsheet.dashboard"
        assert values["name"] == "Sales"
        assert values["dashboard_group_id"] == 3
        document = json.loads(values["spreadsheet_data"])
        assert document["globalFilters"][0]["label"] == "Period"
        assert document["pivots"]["1"]["model"] == "sale.order"

    @pytest.mark.asyncio
    async def test_a_new_section_is_created_when_the_name_is_unknown(
        self, handler, mock_connection
    ):
        mock_connection.search.return_value = []
        mock_connection.create.side_effect = [11, 12]

        await handler._handle_write_dashboard_tool(SPEC, name="Sales", group="Ops")

        assert mock_connection.create.call_args_list[0][0] == (
            "spreadsheet.dashboard.group",
            {"name": "Ops"},
        )

    @pytest.mark.asyncio
    async def test_update_requires_an_existing_dashboard(self, handler, mock_connection):
        mock_connection.read.return_value = []

        with pytest.raises(NotFoundError, match="No dashboard with ID 42"):
            await handler._handle_write_dashboard_tool(SPEC, dashboard_id=42)

    @pytest.mark.asyncio
    async def test_creating_without_a_name_is_refused(self, handler):
        with pytest.raises(ValidationError, match="needs a name"):
            await handler._handle_write_dashboard_tool(SPEC, group="Finance")

    @pytest.mark.asyncio
    async def test_an_invalid_spec_names_the_problem(self, handler):
        with pytest.raises(ValidationError, match="unknown widget type"):
            await handler._handle_write_dashboard_tool(
                {"widgets": [{"type": "gauge", "model": "sale.order"}]},
                name="x",
                group="Finance",
            )

    @pytest.mark.asyncio
    async def test_older_odoo_is_refused_before_anything_is_written(self, handler, mock_connection):
        """o-spreadsheet migrates forward only, so 17.0 cannot open the output."""
        mock_connection.get_major_version.return_value = 17
        mock_connection.server_version = "17.0"

        with pytest.raises(ValidationError, match="needs Odoo 18.0 or newer"):
            await handler._handle_write_dashboard_tool(SPEC, name="Sales", group="Finance")

        mock_connection.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_dry_run_still_works_on_an_older_odoo(self, handler, mock_connection):
        mock_connection.get_major_version.return_value = 17

        result = await handler._handle_write_dashboard_tool(SPEC, dry_run=True)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_an_unintrospectable_model_is_reported_not_fatal(self, handler, mock_connection):
        mock_connection.fields_get.side_effect = Exception("nope")

        result = await handler._handle_write_dashboard_tool(SPEC, dry_run=True)

        assert result["summary"]["unresolved_models"] == ["sale.order"]

    @pytest.mark.asyncio
    async def test_community_gets_told_dashboards_are_enterprise(self, handler, mock_connection):
        mock_connection.search_read.side_effect = Exception(
            "Object spreadsheet.dashboard doesn't exist"
        )

        with pytest.raises(ValidationError, match="Enterprise feature"):
            await handler._handle_list_dashboards_tool()

    @pytest.mark.asyncio
    async def test_get_dashboard_summarizes_the_stored_document(self, handler, mock_connection):
        from mcp_server_odoo.dashboard_builder import build_dashboard

        document = build_dashboard(SPEC, {"sale.order": SALE_FIELDS})
        mock_connection.read.return_value = [
            {
                "name": "Sales",
                "dashboard_group_id": [3, "Finance"],
                "is_published": True,
                "spreadsheet_data": json.dumps(document),
            }
        ]

        result = await handler._handle_get_dashboard_tool(5)

        assert result["name"] == "Sales"
        assert result["group"] == "Finance"
        assert result["summary"]["charts"][0]["model"] == "sale.order"
        assert "document" not in result

    @pytest.mark.asyncio
    async def test_raw_returns_the_whole_document(self, handler, mock_connection):
        mock_connection.read.return_value = [
            {
                "name": "Sales",
                "dashboard_group_id": False,
                "is_published": True,
                "spreadsheet_data": '{"version": "18.5.10", "sheets": []}',
            }
        ]

        result = await handler._handle_get_dashboard_tool(5, raw=True)

        assert result["document"] == {"version": "18.5.10", "sheets": []}

    @pytest.mark.asyncio
    async def test_list_dashboards_groups_and_rows(self, handler, mock_connection):
        mock_connection.search_read.side_effect = [
            [{"id": 2, "name": "Sales"}, {"id": 1, "name": "Finance"}],
            [
                {
                    "id": 4,
                    "name": "Invoicing",
                    "dashboard_group_id": [1, "Finance"],
                    "sequence": 20,
                    "is_published": True,
                }
            ],
        ]

        result = await handler._handle_list_dashboards_tool()

        assert [g["name"] for g in result["groups"]] == ["Finance", "Sales"]
        assert result["dashboards"][0]["group"] == "Finance"
        assert result["total"] == 1
