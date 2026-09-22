"""Tests for the declarative dashboard builder."""

import json

import pytest

from mcp_server_odoo.dashboard_builder import (
    DATA_VERSION,
    DashboardSpecError,
    build_dashboard,
    collect_date_hints,
    collect_models,
    summarize_dashboard,
)

INVOICE_FIELDS = {
    "invoice_date": {"type": "date", "store": True, "string": "Invoice Date"},
    "create_date": {"type": "datetime", "store": True, "string": "Created on"},
    "partner_id": {"type": "many2one", "relation": "res.partner", "string": "Partner"},
    "create_uid": {"type": "many2one", "relation": "res.users", "string": "Created by"},
    "invoice_user_id": {"type": "many2one", "relation": "res.users", "string": "Salesperson"},
    "amount_total": {"type": "monetary", "store": True, "string": "Total"},
    "amount_residual": {"type": "monetary", "store": True, "string": "Amount Due"},
}

FIELDS = {"account.move": INVOICE_FIELDS}


def kpi_spec():
    return {
        "title": "Cash",
        "filters": [
            {"type": "date", "label": "Period", "default": "last_90_days"},
            {"type": "relation", "label": "Customer", "model": "res.partner"},
        ],
        "widgets": [
            {
                "type": "kpi",
                "title": "Open receivables",
                "model": "account.move",
                "measure": "amount_residual",
                "domain": [["move_type", "=", "out_invoice"]],
            },
            {"type": "kpi", "title": "Invoices", "model": "account.move", "measure": "__count"},
        ],
    }


class TestDocumentShape:
    def test_minimal_document_is_json_serialisable(self):
        doc = build_dashboard({"widgets": []})
        assert json.loads(json.dumps(doc)) == doc

    def test_declares_the_migratable_format_version(self):
        doc = build_dashboard({"widgets": []})
        assert doc["version"] == DATA_VERSION
        assert doc["revisionId"] == "START_REVISION"

    def test_has_a_visible_dashboard_sheet_and_a_hidden_data_sheet(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        dashboard, data = doc["sheets"]
        assert dashboard["name"] == "Dashboard"
        assert dashboard["isVisible"] is True
        assert data["name"] == "Data"
        assert data["isVisible"] is False

    def test_rejects_an_unknown_widget_type(self):
        with pytest.raises(DashboardSpecError, match="unknown widget type"):
            build_dashboard({"widgets": [{"type": "gauge", "model": "account.move"}]})

    def test_rejects_a_widget_without_a_model(self):
        with pytest.raises(DashboardSpecError, match="needs model"):
            build_dashboard({"widgets": [{"type": "kpi", "title": "x"}]})


class TestKpis:
    def test_each_kpi_gets_a_pivot_a_formula_and_a_scorecard(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        assert len(doc["pivots"]) == 2
        scorecards = [f for f in doc["sheets"][0]["figures"] if f["data"]["type"] == "scorecard"]
        assert len(scorecards) == 2
        assert all(f["data"]["keyValue"].startswith("Data!") for f in scorecards)

    def test_kpi_pivot_aggregates_without_grouping(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        pivot = doc["pivots"]["1"]
        assert pivot["rows"] == []
        assert pivot["columns"] == []
        assert pivot["measures"] == [{"id": "amount_residual", "fieldName": "amount_residual"}]

    def test_the_formula_addresses_the_pivot_measure(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        cells = doc["sheets"][1]["cells"]
        assert cells["B1"] == '=PIVOT.VALUE(1,"amount_residual")'
        assert cells["B2"] == '=PIVOT.VALUE(2,"__count")'

    def test_consecutive_kpis_are_laid_out_side_by_side(self):
        doc = build_dashboard(kpi_spec(), FIELDS)
        scorecards = [f for f in doc["sheets"][0]["figures"] if f["data"]["type"] == "scorecard"]
        assert {f["row"] for f in scorecards} == {0}
        assert [f["offset"]["x"] for f in scorecards] == [0, 250]


class TestCharts:
    def test_builds_an_odoo_chart_figure(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "chart",
                        "chart": "line",
                        "title": "Revenue",
                        "model": "account.move",
                        "measure": "amount_total",
                        "group_by": ["invoice_date:month"],
                    }
                ]
            },
            FIELDS,
        )
        figure = doc["sheets"][0]["figures"][0]
        assert figure["data"]["type"] == "odoo_line"
        assert figure["data"]["metaData"] == {
            "groupBy": ["invoice_date:month"],
            "measure": "amount_total",
            "order": None,
            "resModel": "account.move",
            "mode": "line",
        }
        assert figure["data"]["searchParams"]["groupBy"] == ["invoice_date"]

    def test_rejects_a_chart_without_a_group_by(self):
        with pytest.raises(DashboardSpecError, match="needs a group_by"):
            build_dashboard(
                {"widgets": [{"type": "chart", "model": "account.move", "measure": "x"}]}
            )

    def test_rejects_an_unknown_chart_kind(self):
        with pytest.raises(DashboardSpecError, match="unknown chart type"):
            build_dashboard(
                {
                    "widgets": [
                        {
                            "type": "chart",
                            "chart": "donut",
                            "model": "account.move",
                            "group_by": ["partner_id"],
                        }
                    ]
                }
            )


class TestPivotsAndLists:
    def test_pivot_spills_from_a_single_formula(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "pivot",
                        "title": "Top customers",
                        "model": "account.move",
                        "rows": ["partner_id"],
                        "measures": ["amount_total"],
                        "sort_by": "amount_total",
                        "limit": 5,
                    }
                ]
            },
            FIELDS,
        )
        cells = doc["sheets"][0]["cells"]
        assert cells["A1"] == "Top customers"
        assert cells["A3"] == "=PIVOT(1, 5, FALSE, FALSE)"
        assert doc["pivots"]["1"]["sortedColumn"] == {
            "measure": "amount_total",
            "order": "desc",
            "domain": [],
        }

    def test_list_writes_headers_and_one_formula_per_line(self):
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "list",
                        "title": "Overdue",
                        "model": "account.move",
                        "columns": ["name", {"name": "amount_residual", "string": "Due"}],
                        "order": ["amount_residual desc"],
                        "limit": 2,
                    }
                ]
            },
            FIELDS,
        )
        cells = doc["sheets"][0]["cells"]
        assert cells["A3"] == '=ODOO.LIST.HEADER(1,"name")'
        assert cells["B3"] == '=ODOO.LIST.HEADER(1,"amount_residual","Due")'
        assert cells["A4"] == '=ODOO.LIST(1,1,"name")'
        assert cells["B5"] == '=ODOO.LIST(1,2,"amount_residual")'
        assert doc["lists"]["1"]["orderBy"] == [{"name": "amount_residual", "asc": False}]

    def test_list_columns_are_stored_as_plain_field_names(self):
        """An object here fails the o-spreadsheet migration on load."""
        doc = build_dashboard(
            {
                "widgets": [
                    {
                        "type": "list",
                        "model": "account.move",
                        "columns": ["name", {"name": "amount_residual", "string": "Due"}],
                    }
                ]
            },
            FIELDS,
        )
        assert doc["lists"]["1"]["columns"] == ["name", "amount_residual"]

    def test_rejects_a_list_without_columns(self):
        with pytest.raises(DashboardSpecError, match="needs columns"):
            build_dashboard({"widgets": [{"type": "list", "model": "account.move"}]})


class TestGlobalFilters:
    def test_date_filter_binds_to_the_charted_date_field(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [
                    {
                        "type": "chart",
                        "model": "account.move",
                        "measure": "amount_total",
                        "group_by": ["invoice_date:month"],
                    }
                ],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        matching = doc["sheets"][0]["figures"][0]["data"]["fieldMatching"]
        assert matching[filter_id] == {"chain": "invoice_date", "type": "date", "offset": 0}

    def test_relation_filter_prefers_the_field_named_after_its_model(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "relation", "label": "Customer", "model": "res.partner"}],
                "widgets": [{"type": "kpi", "model": "account.move", "measure": "amount_total"}],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        assert doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"] == "partner_id"

    def test_relation_filter_skips_bookkeeping_fields(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "relation", "label": "Salesperson", "model": "res.users"}],
                "widgets": [{"type": "kpi", "model": "account.move", "measure": "amount_total"}],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        assert doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"] == "invoice_user_id"

    def test_an_explicit_chain_wins_over_the_automatic_match(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "relation", "label": "Customer", "model": "res.partner"}],
                "widgets": [
                    {
                        "type": "kpi",
                        "model": "account.move",
                        "measure": "amount_total",
                        "filters": {"Customer": "partner_id.parent_id"},
                    }
                ],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        chain = doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"]
        assert chain == "partner_id.parent_id"

    def test_filters_false_detaches_the_widget(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [
                    {
                        "type": "kpi",
                        "model": "account.move",
                        "measure": "amount_total",
                        "filters": False,
                    }
                ],
            },
            FIELDS,
        )
        assert doc["pivots"]["1"]["fieldMatching"] == {}

    def test_without_field_metadata_nothing_is_matched(self):
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [{"type": "kpi", "model": "account.move", "measure": "amount_total"}],
            }
        )
        assert doc["pivots"]["1"]["fieldMatching"] == {}

    def test_relation_filter_needs_a_model(self):
        with pytest.raises(DashboardSpecError, match="needs a model"):
            build_dashboard({"filters": [{"type": "relation", "label": "Customer"}], "widgets": []})


class TestCollectModels:
    def test_lists_every_model_the_spec_touches(self):
        spec = {
            "widgets": [
                {"type": "kpi", "model": "account.move", "measure": "x"},
                {
                    "type": "chart",
                    "model": "sale.report",
                    "measure": "x",
                    "group_by": ["date"],
                },
                {"type": "text", "text": "hello"},
            ]
        }
        assert collect_models(spec) == ["account.move", "sale.report"]


class TestDateHints:
    def test_one_date_field_per_model_across_widgets(self):
        """A KPI must filter on the same date field as the chart beside it."""
        doc = build_dashboard(
            {
                "filters": [{"type": "date", "label": "Period"}],
                "widgets": [
                    {"type": "kpi", "model": "account.move", "measure": "amount_total"},
                    {
                        "type": "chart",
                        "model": "account.move",
                        "measure": "amount_total",
                        "group_by": ["invoice_date:month"],
                    },
                ],
            },
            FIELDS,
        )
        filter_id = doc["globalFilters"][0]["id"]
        kpi_chain = doc["pivots"]["1"]["fieldMatching"][filter_id]["chain"]
        chart_chain = doc["sheets"][0]["figures"][1]["data"]["fieldMatching"][filter_id]["chain"]
        assert kpi_chain == chart_chain == "invoice_date"

    def test_an_explicit_date_field_sets_the_hint(self):
        hints = collect_date_hints(
            {"widgets": [{"model": "account.move", "date_field": "invoice_date_due"}]}
        )
        assert hints == {"account.move": "invoice_date_due"}


class TestSummary:
    def test_reports_the_data_sources_without_the_document(self):
        summary = summarize_dashboard(build_dashboard(kpi_spec(), FIELDS))
        assert summary["scorecards"] == ["Open receivables", "Invoices"]
        assert summary["widgets"] == 2
        assert [f["label"] for f in summary["filters"]] == ["Period", "Customer"]

    def test_kpi_pivots_are_not_counted_twice(self):
        """Each KPI card is backed by a pivot; only the card should show up."""
        summary = summarize_dashboard(build_dashboard(kpi_spec(), FIELDS))
        assert summary["pivots"] == []

    def test_lists_report_field_names_in_either_column_form(self):
        """Odoo's editor saves newer documents with {name, string} list columns."""
        doc = {
            "lists": {
                "1": {"name": "Ours", "model": "m", "columns": ["name", "amount"]},
                "2": {"name": "Editor", "model": "m", "columns": [{"name": "x", "string": "X"}]},
            }
        }
        assert [lst["columns"] for lst in summarize_dashboard(doc)["lists"]] == [
            ["name", "amount"],
            ["x"],
        ]
