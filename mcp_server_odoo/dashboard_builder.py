"""Build Odoo spreadsheet-dashboard JSON from a small declarative spec.

An Odoo dashboard (``spreadsheet.dashboard``, Enterprise) keeps its whole
content in one o-spreadsheet JSON document in ``spreadsheet_data``.
Hand-writing that document is error prone: data sources, figures, cell
formulas and global filter field matchings all have to agree with each
other.

This module takes a spec like::

    {
      "title": "Cash",
      "filters": [{"type": "date", "label": "Period", "default": "last_90_days"}],
      "widgets": [
        {"type": "kpi", "title": "Open", "model": "account.move",
         "measure": "amount_residual", "domain": [["state", "=", "posted"]]},
        {"type": "chart", "chart": "line", "model": "account.invoice.report",
         "measure": "price_subtotal", "group_by": ["invoice_date:month"]},
      ],
    }

and returns the JSON document. Callers pass in the field metadata of every
model the spec mentions so global filters can be wired to the right field.

Format version
--------------
``DATA_VERSION`` is deliberately an *older* o-spreadsheet version than the one
a current instance writes. o-spreadsheet migrates documents forward on load,
and this is the version Odoo ships its own built-in dashboards in, so it is the
shape with the best-tested migration path. Do not bump it to match the running
instance: that would skip the migrations and pin the output to an internal
format that changes every release.

Migration only runs forward, so a document in this version cannot be opened by
an Odoo older than 18.0. ``write_dashboard`` refuses to write to one.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DATA_VERSION = "18.5.10"

# o-spreadsheet geometry defaults, in pixels.
DEFAULT_ROW_HEIGHT = 23
DASHBOARD_WIDTH = 1000
KPI_WIDTH = 235
KPI_HEIGHT = 109
CHART_HEIGHT = 345
FIGURE_GAP = 15

DASHBOARD_SHEET_NAME = "Dashboard"
DATA_SHEET_NAME = "Data"

CHART_TYPES = {
    "bar": "odoo_bar",
    "line": "odoo_line",
    "pie": "odoo_pie",
    "radar": "odoo_radar",
    "waterfall": "odoo_waterfall",
    "pyramid": "odoo_pyramid",
    "scatter": "odoo_scatter",
    "funnel": "odoo_funnel",
    "geo": "odoo_geo",
    "treemap": "odoo_treemap",
    "sunburst": "odoo_sunburst",
}

FILTER_TYPES = {"date", "relation", "text", "numeric", "boolean", "selection"}

WIDGET_TYPES = {"kpi", "chart", "pivot", "list", "text"}

# Date fields Odoo models commonly use as "the" business date, best first.
PREFERRED_DATE_FIELDS = (
    "date",
    "invoice_date",
    "date_order",
    "date_invoice",
    "date_deadline",
    "date_done",
    "date_start",
    "create_date",
)

# Overridable per dashboard through the spec's `locale` key.
DEFAULT_LOCALE = {
    "name": "English (US)",
    "code": "en_US",
    "thousandsSeparator": ",",
    "decimalSeparator": ".",
    "dateFormat": "mm/dd/yyyy",
    "timeFormat": "hh:mm:ss",
    "formulaArgSeparator": ",",
    "weekStart": 7,
}

TITLE_STYLE_ID = "1"
LABEL_STYLE_ID = "2"

STYLES = {
    TITLE_STYLE_ID: {"textColor": "#01666b", "bold": True, "fontSize": 16},
    LABEL_STYLE_ID: {"textColor": "#434343", "bold": True, "fontSize": 11},
}


class DashboardSpecError(ValueError):
    """The spec is not something we can turn into a dashboard."""


def _uid() -> str:
    return str(uuid.uuid4())


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell(col: int, row: int) -> str:
    """0-based (col, row) -> A1 notation."""
    return f"{_col_letter(col)}{row + 1}"


def _px_to_rows(pixels: int) -> int:
    return -(-pixels // DEFAULT_ROW_HEIGHT)  # ceil


def _as_domain(value: Any) -> List[Any]:
    """Accept a domain as a list or as a JSON string."""
    if value in (None, "", False):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DashboardSpecError(f"domain is not valid JSON: {value!r}") from exc
    if not isinstance(value, list):
        raise DashboardSpecError(f"domain must be a list, got {type(value).__name__}")
    return value


def _split_granularity(field_spec: str) -> Tuple[str, Optional[str]]:
    """``invoice_date:month`` -> ``("invoice_date", "month")``."""
    if ":" in field_spec:
        name, _, granularity = field_spec.partition(":")
        return name.strip(), granularity.strip() or None
    return field_spec.strip(), None


def _dimension(field_spec: Any) -> Dict[str, Any]:
    """Normalise a row/column dimension into an o-spreadsheet PivotDimension."""
    if isinstance(field_spec, dict):
        if "fieldName" not in field_spec:
            raise DashboardSpecError(f"dimension needs a fieldName: {field_spec!r}")
        return dict(field_spec)
    name, granularity = _split_granularity(str(field_spec))
    dim: Dict[str, Any] = {"fieldName": name}
    if granularity:
        dim["granularity"] = granularity
    return dim


def _measure(measure_spec: Any) -> Dict[str, Any]:
    """Normalise a measure into an o-spreadsheet PivotMeasure.

    Accepts ``"amount_total"``, ``"amount_total:avg"``, ``"__count"`` or a
    ready-made dict.
    """
    if isinstance(measure_spec, dict):
        if "fieldName" not in measure_spec:
            raise DashboardSpecError(f"measure needs a fieldName: {measure_spec!r}")
        out = dict(measure_spec)
        out.setdefault("id", out["fieldName"])
        return out
    name, aggregator = _split_granularity(str(measure_spec))
    if aggregator:
        return {"id": f"{name}:{aggregator}", "fieldName": name, "aggregator": aggregator}
    return {"id": name, "fieldName": name}


def _measure_formula_id(measure_spec: Any) -> str:
    """The string PIVOT.VALUE() needs to address this measure."""
    return _measure(measure_spec)["id"]


class FieldMatcher:
    """Wires global filters to the field each data source should filter on.

    ``fields_by_model`` is ``{model: {field_name: field_metadata}}``, exactly
    what ``fields_get`` returns. Models missing from it simply get no automatic
    matching — an explicit ``filters`` mapping on the widget still works.
    """

    def __init__(
        self,
        filters: Sequence[Dict[str, Any]],
        fields_by_model: Optional[Dict[str, Dict[str, Any]]] = None,
        date_hints: Optional[Dict[str, str]] = None,
    ):
        self.filters = list(filters)
        self.fields_by_model = fields_by_model or {}
        # One date field per model, so a date filter narrows every widget on
        # that model the same way. Without this, a KPI on account.move would
        # filter on `date` while a chart beside it filters on `invoice_date`,
        # and the two would disagree.
        self.date_hints = date_hints or {}

    def models(self) -> List[str]:
        return sorted(self.fields_by_model)

    def match(
        self,
        model: str,
        overrides: Any = None,
        date_hint: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Build the ``fieldMatching`` map for one data source.

        ``overrides`` maps a filter label (or id) to a field chain, or is
        ``False`` to detach this widget from every filter.
        """
        if overrides is False:
            return {}
        overrides = overrides or {}
        if not isinstance(overrides, dict):
            raise DashboardSpecError("a widget's `filters` must be a mapping or false")

        by_key: Dict[str, str] = {}
        for key, chain in overrides.items():
            by_key[str(key).strip().lower()] = chain

        fields = self.fields_by_model.get(model, {})
        date_hint = date_hint or self.date_hints.get(model)
        matching: Dict[str, Dict[str, Any]] = {}
        for flt in self.filters:
            chain = by_key.get(flt["label"].strip().lower(), by_key.get(flt["id"]))
            if chain is False:
                continue
            if chain is None:
                chain = self._auto_chain(flt, fields, date_hint)
            if not chain:
                continue
            entry: Dict[str, Any] = {"chain": chain, "type": self._chain_type(flt, fields, chain)}
            if flt["type"] == "date":
                entry["offset"] = 0
            matching[flt["id"]] = entry
        return matching

    @staticmethod
    def _chain_type(flt: Dict[str, Any], fields: Dict[str, Any], chain: str) -> str:
        head = chain.split(".")[0]
        meta = fields.get(head)
        if meta and meta.get("type"):
            return meta["type"]
        return {"date": "date", "relation": "many2one"}.get(flt["type"], "char")

    def _auto_chain(
        self,
        flt: Dict[str, Any],
        fields: Dict[str, Any],
        date_hint: Optional[str],
    ) -> Optional[str]:
        if not fields:
            return None
        if flt["type"] == "date":
            if date_hint and date_hint in fields:
                return date_hint
            for name in PREFERRED_DATE_FIELDS:
                meta = fields.get(name)
                if meta and meta.get("type") in ("date", "datetime"):
                    return name
            for name, meta in sorted(fields.items()):
                if meta.get("type") in ("date", "datetime") and meta.get("store", True):
                    return name
            return None
        if flt["type"] == "relation":
            target = flt.get("modelName")
            if not target:
                return None
            candidates = [
                name
                for name, meta in fields.items()
                if meta.get("type") in ("many2one", "many2many") and meta.get("relation") == target
            ]
            if not candidates:
                return None
            # A field named after the target model beats an incidental match
            # such as create_uid when the filter is on res.users.
            preferred = target.split(".")[-1] + "_id"
            for name in (preferred, target.replace(".", "_") + "_id"):
                if name in candidates:
                    return name
            noisy = ("create_uid", "write_uid", "company_id", "currency_id")
            for name in sorted(candidates):
                if name not in noisy:
                    return name
            return sorted(candidates)[0]
        if flt["type"] == "selection":
            name = flt.get("selectionField")
            return name if name and name in fields else None
        return None


class DashboardBuilder:
    """Assembles the o-spreadsheet document one widget at a time."""

    def __init__(
        self,
        title: Optional[str] = None,
        filters: Optional[Sequence[Dict[str, Any]]] = None,
        locale: Optional[Dict[str, Any]] = None,
        fields_by_model: Optional[Dict[str, Dict[str, Any]]] = None,
        date_hints: Optional[Dict[str, str]] = None,
    ):
        self.title = title
        self.locale = locale or dict(DEFAULT_LOCALE)
        self.filters = [_normalise_filter(f) for f in (filters or [])]
        self.matcher = FieldMatcher(self.filters, fields_by_model, date_hints)

        self.dashboard_sheet_id = _uid()
        self.data_sheet_id = _uid()

        self.pivots: Dict[str, Dict[str, Any]] = {}
        self.lists: Dict[str, Dict[str, Any]] = {}
        self.figures: List[Dict[str, Any]] = []
        self.dash_cells: Dict[str, str] = {}
        self.dash_styles: Dict[str, str] = {}
        self.data_cells: Dict[str, str] = {}

        self._next_pivot = 1
        self._next_list = 1
        self._next_data_row = 0
        self._row = 0  # current anchor row on the dashboard sheet
        self._max_col = 6

    # -- placement ------------------------------------------------------

    def _advance(self, rows: int) -> None:
        self._row += max(rows, 1)

    def _heading(self, text: Optional[str]) -> None:
        if not text:
            return
        ref = _cell(0, self._row)
        self.dash_cells[ref] = text
        self.dash_styles[ref] = TITLE_STYLE_ID
        self._advance(2)

    def _add_figure(self, data: Dict[str, Any], width: int, height: int, x: int = 0) -> str:
        figure_id = _uid()
        data = dict(data)
        data["chartId"] = figure_id
        self.figures.append(
            {
                "id": figure_id,
                "tag": "chart",
                "width": width,
                "height": height,
                "col": 0,
                "row": self._row,
                "offset": {"x": x, "y": 10},
                "data": data,
            }
        )
        return figure_id

    # -- widgets --------------------------------------------------------

    def add_kpis(self, widgets: Sequence[Dict[str, Any]]) -> None:
        """Place a row of scorecards side by side.

        Every KPI is backed by a pivot with no rows and no columns, which is
        how Odoo itself renders a single aggregate number.
        """
        x = 0
        for widget in widgets:
            model = _require(widget, "model")
            measure = widget.get("measure", "__count")
            pivot_id = self._add_pivot_definition(
                model=model,
                name=widget.get("title") or f"KPI - {model}",
                domain=_as_domain(widget.get("domain")),
                context=widget.get("context") or {},
                rows=[],
                columns=[],
                measures=[measure],
                filters=widget.get("filters"),
                date_hint=widget.get("date_field"),
            )
            value_ref = self._add_data_row(
                label=widget.get("title") or model,
                formula=f'=PIVOT.VALUE({pivot_id},"{_measure_formula_id(measure)}")',
                humanize=widget.get("humanize", True),
            )
            self._add_figure(
                {
                    "type": "scorecard",
                    "title": {"text": widget.get("title") or "", "color": "#434343", "bold": True},
                    "background": widget.get("background", "#EFF6FF"),
                    "keyValue": value_ref,
                    "baselineMode": "text",
                    "baselineColorUp": "#00A04A",
                    "baselineColorDown": "#DC6965",
                    "baselineDescr": {"text": widget.get("description", "")},
                    "humanize": False,
                },
                width=KPI_WIDTH,
                height=KPI_HEIGHT,
                x=x,
            )
            x += KPI_WIDTH + FIGURE_GAP
        self._advance(_px_to_rows(KPI_HEIGHT + 2 * FIGURE_GAP))

    def add_chart(self, widget: Dict[str, Any]) -> None:
        model = _require(widget, "model")
        kind = widget.get("chart", "bar")
        if kind not in CHART_TYPES:
            raise DashboardSpecError(
                f"unknown chart type {kind!r}; pick one of {sorted(CHART_TYPES)}"
            )
        group_by = [str(g) for g in (widget.get("group_by") or [])]
        if not group_by:
            raise DashboardSpecError(f"chart {widget.get('title') or model!r} needs a group_by")
        measure = widget.get("measure", "__count")
        if isinstance(measure, dict):
            raise DashboardSpecError("a chart takes a single measure name, not a dict")
        measure_name, _ = _split_granularity(str(measure))
        plain_group_by = [_split_granularity(g)[0] for g in group_by]

        self._heading(widget.get("title"))
        height = int(widget.get("height", CHART_HEIGHT))
        self._add_figure(
            {
                "type": CHART_TYPES[kind],
                "title": {"text": widget.get("title") or ""},
                "background": widget.get("background", "#FFFFFF"),
                "legendPosition": widget.get("legend", "top"),
                "metaData": {
                    "groupBy": group_by,
                    "measure": measure_name,
                    "order": widget.get("order"),
                    "resModel": model,
                    "mode": kind,
                },
                "searchParams": {
                    "comparison": None,
                    "context": widget.get("context") or {},
                    "domain": _as_domain(widget.get("domain")),
                    "groupBy": plain_group_by,
                    "orderBy": [],
                },
                "dataSets": [{}],
                "stacked": bool(widget.get("stacked", False)),
                "fieldMatching": self.matcher.match(
                    model,
                    widget.get("filters"),
                    date_hint=widget.get("date_field") or _first_date(group_by),
                ),
            },
            width=int(widget.get("width", DASHBOARD_WIDTH)),
            height=height,
        )
        self._advance(_px_to_rows(height + 2 * FIGURE_GAP))

    def add_pivot(self, widget: Dict[str, Any]) -> None:
        model = _require(widget, "model")
        rows = [_dimension(r) for r in (widget.get("rows") or [])]
        columns = [_dimension(c) for c in (widget.get("columns") or [])]
        measures = widget.get("measures") or [widget.get("measure", "__count")]
        limit = int(widget.get("limit", 10))
        sort_by = widget.get("sort_by")

        sorted_column = None
        if sort_by:
            sorted_column = {
                "measure": _measure_formula_id(sort_by),
                "order": widget.get("sort_order", "desc"),
                "domain": [],
            }

        pivot_id = self._add_pivot_definition(
            model=model,
            name=widget.get("title") or model,
            domain=_as_domain(widget.get("domain")),
            context=widget.get("context") or {},
            rows=rows,
            columns=columns,
            measures=measures,
            filters=widget.get("filters"),
            date_hint=widget.get("date_field"),
            sorted_column=sorted_column,
        )

        self._heading(widget.get("title"))
        ref = _cell(0, self._row)
        include_total = "TRUE" if widget.get("include_total", False) else "FALSE"
        self.dash_cells[ref] = f"=PIVOT({pivot_id}, {limit}, {include_total}, FALSE)"
        self.dash_styles[ref] = LABEL_STYLE_ID
        # Header row plus one row per record, plus breathing room.
        self._advance(limit + 3)
        self._max_col = max(self._max_col, len(columns) + len(measures) + 2)

    def add_list(self, widget: Dict[str, Any]) -> None:
        model = _require(widget, "model")
        columns = widget.get("columns") or []
        if not columns:
            raise DashboardSpecError(f"list {widget.get('title') or model!r} needs columns")
        limit = int(widget.get("limit", 10))

        order_by = []
        for entry in widget.get("order") or []:
            if isinstance(entry, dict):
                order_by.append(entry)
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                order_by.append({"name": entry[0], "asc": str(entry[1]).lower() != "desc"})
            else:
                name, _, direction = str(entry).partition(" ")
                order_by.append({"name": name, "asc": direction.lower() != "desc"})

        list_id = str(self._next_list)
        self._next_list += 1
        self.lists[list_id] = {
            "id": list_id,
            "name": widget.get("title") or model,
            "model": model,
            "columns": [_list_field(c) for c in columns],
            "domain": _as_domain(widget.get("domain")),
            "context": widget.get("context") or {},
            "orderBy": order_by,
            "fieldMatching": self.matcher.match(
                model, widget.get("filters"), date_hint=widget.get("date_field")
            ),
        }

        self._heading(widget.get("title"))
        header_row = self._row
        for col_index, column in enumerate(columns):
            name = _list_field(column)
            label = column.get("string") if isinstance(column, dict) else None
            ref = _cell(col_index, header_row)
            self.dash_cells[ref] = (
                f'=ODOO.LIST.HEADER({list_id},"{name}","{label}")'
                if label
                else f'=ODOO.LIST.HEADER({list_id},"{name}")'
            )
            self.dash_styles[ref] = LABEL_STYLE_ID
            for line in range(1, limit + 1):
                self.dash_cells[_cell(col_index, header_row + line)] = (
                    f'=ODOO.LIST({list_id},{line},"{name}")'
                )
        self._advance(limit + 3)
        self._max_col = max(self._max_col, len(columns) + 1)

    def add_text(self, widget: Dict[str, Any]) -> None:
        text = widget.get("text") or widget.get("title") or ""
        ref = _cell(0, self._row)
        self.dash_cells[ref] = text
        self.dash_styles[ref] = TITLE_STYLE_ID if widget.get("heading", True) else LABEL_STYLE_ID
        self._advance(2)

    # -- internals ------------------------------------------------------

    def _add_pivot_definition(
        self,
        model: str,
        name: str,
        domain: List[Any],
        context: Dict[str, Any],
        rows: List[Dict[str, Any]],
        columns: List[Dict[str, Any]],
        measures: Sequence[Any],
        filters: Any,
        date_hint: Optional[str],
        sorted_column: Optional[Dict[str, Any]] = None,
    ) -> str:
        pivot_id = str(self._next_pivot)
        self._next_pivot += 1
        hint = date_hint
        if hint is None:
            hint = _first_date([r.get("fieldName", "") for r in rows + columns])
        self.pivots[pivot_id] = {
            "type": "ODOO",
            "id": pivot_id,
            "formulaId": pivot_id,
            "name": name,
            "model": model,
            "domain": domain,
            "context": context,
            "rows": rows,
            "columns": columns,
            "measures": [_measure(m) for m in measures],
            "sortedColumn": sorted_column,
            "fieldMatching": self.matcher.match(model, filters, date_hint=hint),
        }
        return pivot_id

    def _add_data_row(self, label: str, formula: str, humanize: bool) -> str:
        """Park a KPI formula on the Data sheet and return its display cell."""
        row = self._next_data_row
        self._next_data_row += 1
        self.data_cells[_cell(0, row)] = label
        self.data_cells[_cell(1, row)] = formula
        display_col = 1
        if humanize:
            self.data_cells[_cell(2, row)] = f"=FORMAT.LARGE.NUMBER({_cell(1, row)})"
            display_col = 2
        return f"{DATA_SHEET_NAME}!{_cell(display_col, row)}"

    # -- output ---------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        dashboard_rows = max(self._row + 6, 40)
        return {
            "version": DATA_VERSION,
            "revisionId": "START_REVISION",
            "uniqueFigureIds": True,
            "sheets": [
                {
                    "id": self.dashboard_sheet_id,
                    "name": DASHBOARD_SHEET_NAME,
                    "colNumber": max(self._max_col, 7),
                    "rowNumber": dashboard_rows,
                    "cells": self.dash_cells,
                    "styles": self.dash_styles,
                    "formats": {},
                    "borders": {},
                    "merges": [],
                    "cols": {},
                    "rows": {},
                    "figures": self.figures,
                    "tables": [],
                    "conditionalFormats": [],
                    "dataValidationRules": [],
                    "headerGroups": {"ROW": [], "COL": []},
                    "comments": {},
                    "areGridLinesVisible": False,
                    "isVisible": True,
                },
                {
                    "id": self.data_sheet_id,
                    "name": DATA_SHEET_NAME,
                    "colNumber": 6,
                    "rowNumber": max(self._next_data_row + 5, 20),
                    "cells": self.data_cells,
                    "styles": {},
                    "formats": {},
                    "borders": {},
                    "merges": [],
                    "cols": {},
                    "rows": {},
                    "figures": [],
                    "tables": [],
                    "conditionalFormats": [],
                    "dataValidationRules": [],
                    "headerGroups": {"ROW": [], "COL": []},
                    "comments": {},
                    "areGridLinesVisible": True,
                    "isVisible": False,
                },
            ],
            "styles": STYLES,
            "formats": {},
            "borders": {},
            "customTableStyles": {},
            "pivots": self.pivots,
            "pivotNextId": self._next_pivot,
            "lists": self.lists,
            "listNextId": self._next_list,
            "globalFilters": self.filters,
            "chartOdooMenusReferences": {},
            "settings": {"locale": self.locale},
        }


def _list_field(column: Any) -> str:
    """The field name of a list column.

    A list's stored ``columns`` is a flat array of field names. The richer
    ``{name, string}`` form only arrived in a later o-spreadsheet version than
    the one we emit, and the migration that runs on load rejects an object
    here with "Invalid path: [object Object]". A column label therefore lives
    only in the ODOO.LIST.HEADER formula, which has always taken one.
    """
    if isinstance(column, dict):
        if "name" not in column:
            raise DashboardSpecError(f"list column needs a name: {column!r}")
        return str(column["name"])
    return str(column)


def _first_date(field_specs: Iterable[str]) -> Optional[str]:
    """Pick the field a date filter should latch onto, from a group_by list."""
    for spec in field_specs:
        name, granularity = _split_granularity(str(spec))
        if granularity or re.search(r"(^|_)date(_|$)", name):
            return name
    return None


def _require(widget: Dict[str, Any], key: str) -> Any:
    value = widget.get(key)
    if not value:
        raise DashboardSpecError(
            f"widget {widget.get('title') or widget.get('type')!r} needs {key}"
        )
    return value


def _normalise_filter(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a terse filter spec into a full o-spreadsheet GlobalFilter."""
    if not isinstance(spec, dict):
        raise DashboardSpecError(f"a filter must be an object, got {spec!r}")
    kind = spec.get("type", "date")
    if kind not in FILTER_TYPES:
        raise DashboardSpecError(
            f"unknown filter type {kind!r}; pick one of {sorted(FILTER_TYPES)}"
        )
    out: Dict[str, Any] = {
        "id": spec.get("id") or _uid(),
        "type": kind,
        "label": spec.get("label") or kind.capitalize(),
    }
    default = spec.get("default", spec.get("defaultValue"))
    if kind == "relation":
        model = spec.get("model") or spec.get("modelName")
        if not model:
            raise DashboardSpecError(f"relation filter {out['label']!r} needs a model")
        out["modelName"] = model
        out["defaultValueDisplayNames"] = []
        # Only emitted when asked for: the key is newer than the format version
        # we write, and the migration on load has no reason to see it otherwise.
        if spec.get("include_children"):
            out["includeChildren"] = True
    elif kind == "selection":
        out["resModel"] = _require(spec, "model")
        out["selectionField"] = _require(spec, "field")
    if default not in (None, "", False):
        out["defaultValue"] = default
    return out


def collect_date_hints(spec: Dict[str, Any]) -> Dict[str, str]:
    """The date field each model should be filtered on, taken from the spec.

    A widget's own ``date_field`` wins; otherwise the date field it already
    groups by is the one the user clearly cares about. The first widget to
    name one decides for every other widget on the same model.
    """
    hints: Dict[str, str] = {}
    for widget in spec.get("widgets") or []:
        model = widget.get("model")
        if not model or model in hints:
            continue
        hint = widget.get("date_field") or _first_date(
            [str(g) for g in (widget.get("group_by") or [])]
        )
        if hint:
            hints[model] = hint
    return hints


def collect_models(spec: Dict[str, Any]) -> List[str]:
    """Every Odoo model the spec touches, so the caller can fetch their fields."""
    models = set()
    for widget in spec.get("widgets") or []:
        model = widget.get("model")
        if model:
            models.add(model)
        for sub in widget.get("kpis") or []:
            if sub.get("model"):
                models.add(sub["model"])
    return sorted(models)


def build_dashboard(
    spec: Dict[str, Any],
    fields_by_model: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Turn a dashboard spec into the o-spreadsheet document Odoo stores.

    Args:
        spec: ``{title, locale, filters, widgets}``. Consecutive ``kpi``
            widgets are laid out side by side as one band of scorecards.
        fields_by_model: ``fields_get`` output per model, used to wire global
            filters automatically. Optional, but without it a filter only
            reaches a widget that names its field explicitly.

    Returns:
        The document, ready to be JSON-encoded into ``spreadsheet_data``.
    """
    if not isinstance(spec, dict):
        raise DashboardSpecError("the dashboard spec must be an object")
    widgets = spec.get("widgets") or []
    if not isinstance(widgets, list):
        raise DashboardSpecError("`widgets` must be a list")

    builder = DashboardBuilder(
        title=spec.get("title"),
        filters=spec.get("filters") or [],
        locale=spec.get("locale"),
        fields_by_model=fields_by_model,
        date_hints=collect_date_hints(spec),
    )

    index = 0
    while index < len(widgets):
        widget = widgets[index]
        if not isinstance(widget, dict):
            raise DashboardSpecError(f"widget #{index} must be an object")
        kind = widget.get("type", "chart")
        if kind not in WIDGET_TYPES:
            raise DashboardSpecError(
                f"unknown widget type {kind!r}; pick one of {sorted(WIDGET_TYPES)}"
            )
        if kind == "kpi":
            band = []
            while index < len(widgets) and widgets[index].get("type") == "kpi":
                band.append(widgets[index])
                index += 1
            builder.add_kpis(band)
            continue
        if kind == "chart":
            builder.add_chart(widget)
        elif kind == "pivot":
            builder.add_pivot(widget)
        elif kind == "list":
            builder.add_list(widget)
        elif kind == "text":
            builder.add_text(widget)
        index += 1

    return builder.to_dict()


def summarize_dashboard(document: Dict[str, Any]) -> Dict[str, Any]:
    """Describe a dashboard document without returning the whole thing.

    A generated document runs to tens of kilobytes, most of it layout. This
    keeps what a caller actually needs to check: which data sources it reads,
    what each one measures, and which filters are on it.
    """
    figures = [f for sheet in document.get("sheets", []) for f in sheet.get("figures", [])]
    charts = [
        {
            "title": (f["data"].get("title") or {}).get("text") or "",
            "type": f["data"].get("type"),
            "model": (f["data"].get("metaData") or {}).get("resModel"),
            "measure": (f["data"].get("metaData") or {}).get("measure"),
            "group_by": (f["data"].get("metaData") or {}).get("groupBy"),
        }
        for f in figures
        if f.get("tag") == "chart" and str(f["data"].get("type", "")).startswith("odoo_")
    ]
    scorecards = [
        (f["data"].get("title") or {}).get("text") or ""
        for f in figures
        if f.get("tag") == "chart" and f["data"].get("type") == "scorecard"
    ]
    pivots = [
        {
            "name": p.get("name"),
            "model": p.get("model"),
            "rows": [r.get("fieldName") for r in p.get("rows") or []],
            "columns": [c.get("fieldName") for c in p.get("columns") or []],
            "measures": [m.get("fieldName") for m in p.get("measures") or []],
        }
        for p in (document.get("pivots") or {}).values()
        # A KPI card is backed by a row- and column-less pivot; listing those
        # as pivots too would count every card twice.
        if p.get("rows") or p.get("columns")
    ]
    lists = [
        {
            "name": lst.get("name"),
            "model": lst.get("model"),
            "columns": list(lst.get("columns") or []),
        }
        for lst in (document.get("lists") or {}).values()
    ]
    return {
        "version": document.get("version"),
        "widgets": len(charts) + len(scorecards) + len(pivots) + len(lists),
        "scorecards": scorecards,
        "charts": charts,
        "pivots": pivots,
        "lists": lists,
        "filters": [
            {"label": f.get("label"), "type": f.get("type"), "model": f.get("modelName")}
            for f in document.get("globalFilters") or []
        ],
    }
