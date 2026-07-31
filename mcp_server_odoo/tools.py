"""MCP tool handlers for Odoo operations.

This module implements MCP tools for performing operations on Odoo data.
Tools are different from resources - they can have side effects and perform
actions like creating, updating, or deleting records.
"""

import asyncio
import base64
import json
import re
import xmlrpc.client
from ast import literal_eval as _parse_python_literal
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .access_control import (
    AccessControlError,
    AccessController,
    AccessControlUnavailableError,
)
from .config import OdooConfig
from .error_handling import (
    NotFoundError,
    ValidationError,
)
from .error_sanitizer import ErrorSanitizer
from .export_utils import execute_export
from .logging_config import get_logger, perf_logger
from .odoo_connection import OdooConnection, OdooConnectionError
from .schemas import (
    AggregateResult,
    CallModelMethodResult,
    CreateResult,
    DeleteResult,
    ExportAccessDeniedError,
    ExportBlockedExceedsLimitError,
    ExportDisabledError,
    ExportFileError,
    ExportResult,
    ExportSuccessResult,
    FieldSelectionMetadata,
    ModelsResult,
    PostMessageResult,
    RecordResult,
    ResourceTemplatesResult,
    SearchResult,
    UpdateResult,
)

logger = get_logger(__name__)

# Public Odoo method = Python identifier not starting with "_".
_PUBLIC_METHOD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# Refuse JSON strings larger than this on the parse path — bounds memory and
# guards against pathological inputs.
_MAX_JSON_PARAM_BYTES = 1_000_000


def _json_safe(value: Any) -> Any:
    """Coerce XML-RPC return types Pydantic can't serialize (Binary, DateTime)."""
    if isinstance(value, xmlrpc.client.Binary):
        return base64.b64encode(value.data).decode("ascii")
    if isinstance(value, xmlrpc.client.DateTime):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class OdooToolHandler:
    """Handles MCP tool requests for Odoo operations."""

    def __init__(
        self,
        app: FastMCP,
        connection: OdooConnection,
        access_controller: AccessController,
        config: OdooConfig,
    ):
        """Initialize tool handler.

        Args:
            app: FastMCP application instance
            connection: Odoo connection instance
            access_controller: Access control instance
            config: Odoo configuration instance
        """
        self.app = app
        self.connection = connection
        self.access_controller = access_controller
        self.config = config

        # Register tools
        self._register_tools()

    def _format_datetime(self, value: str) -> str:
        """Format datetime values to ISO 8601 with timezone."""
        if not value or not isinstance(value, str):
            return value

        # Handle Odoo's compact datetime format (YYYYMMDDTHH:MM:SS)
        if len(value) == 17 and "T" in value and "-" not in value:
            try:
                dt = datetime.strptime(value, "%Y%m%dT%H:%M:%S")
                return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            except ValueError:
                pass

        # Handle standard Odoo datetime format (YYYY-MM-DD HH:MM:SS)
        if " " in value and len(value) == 19:
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            except ValueError:
                pass

        return value

    def _process_record_dates(self, record: Dict[str, Any], model: str) -> Dict[str, Any]:
        """Process datetime fields in a record to ensure proper formatting."""
        # Common datetime field names in Odoo
        known_datetime_fields = {
            "create_date",
            "write_date",
            "date",
            "datetime",
            "date_start",
            "date_end",
            "date_from",
            "date_to",
            "date_order",
            "date_invoice",
            "date_due",
            "last_update",
            "last_activity",
            "activity_date_deadline",
        }

        # First try to get field metadata
        fields_info = None
        try:
            fields_info = self.connection.fields_get(model)
        except Exception:
            # Field metadata unavailable, will use fallback detection
            pass

        # Process each field in the record
        for field_name, field_value in record.items():
            if not isinstance(field_value, str):
                continue

            should_format = False

            # Check if field is identified as datetime from metadata
            if fields_info and isinstance(fields_info, dict) and field_name in fields_info:
                field_type = fields_info[field_name].get("type")
                if field_type == "datetime":
                    should_format = True

            # Check if field name suggests it's a datetime field
            if not should_format and field_name in known_datetime_fields:
                should_format = True

            # Check if field name ends with common datetime suffixes
            if not should_format and any(
                field_name.endswith(suffix) for suffix in ["_date", "_datetime", "_time"]
            ):
                should_format = True

            # Pattern-based detection for datetime-like strings
            if not should_format and (
                (
                    len(field_value) == 17 and "T" in field_value and "-" not in field_value
                )  # 20250607T21:55:52
                or (
                    len(field_value) == 19 and " " in field_value and field_value.count("-") == 2
                )  # 2025-06-07 21:55:52
            ):
                should_format = True

            # Apply formatting if needed
            if should_format:
                formatted = self._format_datetime(field_value)
                if formatted != field_value:
                    record[field_name] = formatted

        return record

    def _score_field_importance(self, field_name: str, field_info: Dict[str, Any]) -> int:
        """Score field importance for smart default selection.

        Args:
            field_name: Name of the field
            field_info: Field metadata from fields_get()

        Returns:
            Importance score (higher = more important)
        """
        # Tier 1: Essential fields (always included)
        if field_name in {"id", "name", "display_name", "active"}:
            return 1000

        # Exclude system/technical fields by prefix
        exclude_prefixes = ("_", "message_", "activity_", "website_message_")
        if field_name.startswith(exclude_prefixes):
            return 0

        # Exclude specific technical fields
        exclude_fields = {
            "write_date",
            "create_date",
            "write_uid",
            "create_uid",
            "__last_update",
            "access_token",
            "access_warning",
            "access_url",
        }
        if field_name in exclude_fields:
            return 0

        score = 0

        # Tier 2: Required fields are very important
        if field_info.get("required"):
            score += 500

        # Tier 3: Field type importance
        field_type = field_info.get("type", "")
        type_scores = {
            "char": 200,
            "boolean": 180,
            "selection": 170,
            "integer": 160,
            "float": 160,
            "monetary": 140,
            "date": 150,
            "datetime": 150,
            "many2one": 120,  # Relations useful but not primary
            "text": 80,
            "one2many": 40,
            "many2many": 40,  # Heavy relations
            "binary": 10,
            "html": 10,
            "image": 10,  # Heavy content
        }
        score += type_scores.get(field_type, 50)

        # Tier 4: Storage and searchability bonuses
        if field_info.get("store", True):
            score += 80
        if field_info.get("searchable", True):
            score += 40

        # Tier 5: Business-relevant field patterns (bonus)
        business_patterns = [
            "state",
            "status",
            "stage",
            "priority",
            "company",
            "currency",
            "amount",
            "total",
            "date",
            "user",
            "partner",
            "email",
            "phone",
            "address",
            "street",
            "city",
            "country",
            "code",
            "ref",
            "number",
        ]
        if any(pattern in field_name.lower() for pattern in business_patterns):
            score += 60

        # Exclude expensive computed fields (non-stored)
        if field_info.get("compute") and not field_info.get("store", True):
            score = min(score, 30)  # Cap computed fields at low score

        # Exclude large field types completely
        if field_type in ("binary", "image", "html"):
            return 0

        # Exclude one2many and many2many fields (can be large)
        if field_type in ("one2many", "many2many"):
            return 0

        return max(score, 0)

    def _get_smart_default_fields(self, model: str) -> Optional[List[str]]:
        """Get smart default fields for a model using field importance scoring.

        Args:
            model: The Odoo model name

        Returns:
            List of field names to include by default, or None if unable to determine
        """
        try:
            # Get all field definitions
            fields_info = self.connection.fields_get(model)

            # Score all fields by importance
            field_scores = []
            for field_name, field_info in fields_info.items():
                score = self._score_field_importance(field_name, field_info)
                if score > 0:  # Only include fields with positive scores
                    field_scores.append((field_name, score))

            # Sort by score (highest first)
            field_scores.sort(key=lambda x: x[1], reverse=True)

            # Select top N fields based on configuration
            max_fields = self.config.max_smart_fields
            selected_fields = [field_name for field_name, _ in field_scores[:max_fields]]

            # Ensure essential fields are always included
            essential_fields = ["id", "name", "display_name", "active"]
            for field in essential_fields:
                if field in fields_info and field not in selected_fields:
                    selected_fields.append(field)

            # Remove duplicates while preserving order
            final_fields = []
            seen = set()
            for field in selected_fields:
                if field not in seen:
                    final_fields.append(field)
                    seen.add(field)

            # Ensure we have at least essential fields
            if not final_fields:
                final_fields = [f for f in essential_fields if f in fields_info]

            logger.debug(
                f"Smart default fields for {model}: {len(final_fields)} of {len(fields_info)} fields "
                f"(max configured: {max_fields})"
            )
            return final_fields

        except Exception as e:
            logger.warning(f"Could not determine default fields for {model}: {e}")
            # Return None to indicate we should get all fields
            return None

    def _parse_domain_input(self, domain: Optional[Any]) -> List[Any]:
        """Coerce a domain parameter into an Odoo domain list.

        Accepts a list (passed through), a JSON string, a Python-literal
        string with single quotes / ``True``/``False`` capitalization, or
        ``None`` (returns ``[]``). Raises ``ValidationError`` on anything
        that doesn't yield a list.
        """
        if domain is None:
            return []
        if not isinstance(domain, str):
            if not isinstance(domain, list):
                raise ValidationError(f"Domain must be a list, got {type(domain).__name__}")
            return domain

        try:
            parsed = json.loads(domain)
        except json.JSONDecodeError as e:
            # literal_eval handles single quotes and True/False natively,
            # without corrupting those substrings inside quoted values.
            try:
                parsed = _parse_python_literal(domain)
            except (ValueError, SyntaxError):
                raise ValidationError(
                    f"Invalid domain parameter. Expected JSON array or Python list, "
                    f"got: {domain[:100]}..."
                ) from e

        if not isinstance(parsed, list):
            raise ValidationError(f"Domain must be a list, got {type(parsed).__name__}")

        logger.debug(f"Parsed domain from string: {parsed}")
        return parsed

    async def _ctx_info(self, ctx, message: str):
        """Send info to MCP client context if available."""
        if ctx:
            try:
                await ctx.info(message)
            except Exception:
                logger.debug(f"Failed to send ctx info: {message}")

    async def _ctx_warning(self, ctx, message: str):
        """Send warning to MCP client context if available."""
        if ctx:
            try:
                await ctx.warning(message)
            except Exception:
                logger.debug(f"Failed to send ctx warning: {message}")

    async def _ctx_progress(self, ctx, progress: float, total: float, message: str = ""):
        """Report progress to MCP client context if available."""
        if ctx:
            try:
                await ctx.report_progress(progress, total, message)
            except Exception:
                logger.debug(f"Failed to report progress: {progress}/{total}")

    def _register_tools(self):
        """Register all tool handlers with FastMCP."""

        @self.app.tool(
            title="Search Records",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
        async def search_records(
            model: str,
            domain: Optional[Any] = None,
            fields: Optional[Any] = None,
            limit: Optional[int] = None,
            offset: int = 0,
            order: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> SearchResult:
            """Search for records in an Odoo model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                domain: Odoo domain filter - can be:
                    - A list: [['is_company', '=', True]]
                    - A JSON string: "[['is_company', '=', true]]"
                    - None: returns all records (default)
                fields: Field selection options - can be:
                    - None (default): Returns smart selection of common fields
                    - A list: ["field1", "field2", ...] - Returns only specified fields
                    - A JSON string: '["field1", "field2"]' - Parsed to list
                    - An empty list []: Treated like None (smart defaults)
                    - ["__all__"] or '["__all__"]': Returns ALL fields (warning: may be slow)
                limit: Maximum number of records to return. Omit to use the
                    server-configured default (ODOO_MCP_DEFAULT_LIMIT). Capped
                    at ODOO_MCP_MAX_LIMIT.
                offset: Number of records to skip
                order: Sort order (e.g., 'name asc')

            Returns:
                Search results with records, total count, and pagination info
            """
            result = await self._handle_search_tool(
                model, domain, fields, limit, offset, order, ctx
            )
            return SearchResult(**result)

        @self.app.tool(
            title="Get Record",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def get_record(
            model: str,
            record_id: int,
            fields: Optional[List[str]] = None,
            ctx: Optional[Context] = None,
        ) -> RecordResult:
            """Get a specific record by ID with smart field selection.

            This tool supports selective field retrieval to optimize performance and response size.
            By default, returns a smart selection of commonly-used fields based on the model's field metadata.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID
                fields: Field selection options:
                    - None (default): Returns smart selection of common fields
                    - ["field1", "field2", ...]: Returns only specified fields
                    - An empty list []: Treated like None (smart defaults)
                    - ["__all__"]: Returns ALL fields (warning: can be very large)

            Workflow for field discovery:
            1. To see all available fields for a model, use the resource:
               read("odoo://res.partner/fields")
            2. Then request specific fields:
               get_record("res.partner", 1, fields=["name", "email", "phone"])

            Examples:
                # Get smart defaults (recommended)
                get_record("res.partner", 1)

                # Get specific fields only
                get_record("res.partner", 1, fields=["name", "email", "phone"])

                # Get ALL fields (use with caution)
                get_record("res.partner", 1, fields=["__all__"])

            Returns:
                Record data with requested fields. When using smart defaults,
                includes metadata with field statistics.
            """
            return await self._handle_get_record_tool(model, record_id, fields, ctx)

        @self.app.tool(
            title="List Models",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def list_models(ctx: Optional[Context] = None) -> ModelsResult:
            """List all models enabled for MCP access with their allowed operations.

            Returns:
                List of models with their technical names, display names,
                and allowed operations (read, write, create, unlink).
            """
            result = await self._handle_list_models_tool(ctx)
            return ModelsResult(**result)

        @self.app.tool(
            title="List Resource Templates",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def list_resource_templates(ctx: Optional[Context] = None) -> ResourceTemplatesResult:
            """List available resource URI templates.

            Since MCP resources with parameters are registered as templates,
            they don't appear in the standard resource list. This tool provides
            information about available resource patterns you can use.

            Returns:
                Resource template definitions with examples and enabled models.
            """
            result = await self._handle_list_resource_templates_tool(ctx)
            return ResourceTemplatesResult(**result)

        @self.app.tool(
            title="Create Record",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
        async def create_record(
            model: str,
            values: Dict[str, Any],
            ctx: Optional[Context] = None,
        ) -> CreateResult:
            """Create a new record in an Odoo model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                values: Field values for the new record

            Returns:
                Created record details with ID, URL, and confirmation.
            """
            result = await self._handle_create_record_tool(model, values, ctx)
            return CreateResult(**result)

        @self.app.tool(
            title="Update Record",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
        async def update_record(
            model: str,
            record_id: int,
            values: Dict[str, Any],
            ctx: Optional[Context] = None,
        ) -> UpdateResult:
            """Update an existing record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to update
                values: Field values to update

            Returns:
                Updated record details with confirmation.
            """
            result = await self._handle_update_record_tool(model, record_id, values, ctx)
            return UpdateResult(**result)

        @self.app.tool(
            title="Delete Record",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def delete_record(
            model: str,
            record_id: int,
            ctx: Optional[Context] = None,
        ) -> DeleteResult:
            """Delete a record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to delete

            Returns:
                Deletion confirmation with the deleted record's name and ID.
            """
            result = await self._handle_delete_record_tool(model, record_id, ctx)
            return DeleteResult(**result)

        @self.app.tool(
            title="Post Message",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
        async def post_message(
            model: str,
            record_id: int,
            body: str,
            subtype: Literal["note", "comment"] = "note",
            message_type: Literal["comment", "notification"] = "comment",
            partner_ids: Optional[List[int]] = None,
            attachment_ids: Optional[List[int]] = None,
            body_is_html: bool = False,
            ctx: Optional[Context] = None,
        ) -> PostMessageResult:
            """Post a message to an Odoo record's chatter (mail.thread).

            ``subtype="note"`` (default) is an internal log; ``subtype="comment"``
            notifies followers. Set ``body_is_html=True`` for HTML markup
            (Odoo 17+ escapes str bodies otherwise).

            Args:
                model: Odoo model name (e.g., 'res.partner')
                record_id: Record ID to post to
                body: Message body (plain text by default; HTML if body_is_html=True)
                subtype: 'note' (internal, default) or 'comment' (notifies followers)
                message_type: 'comment' (default) or 'notification'
                partner_ids: Optional list of res.partner IDs to additionally notify
                attachment_ids: Optional list of existing ir.attachment IDs to link
                body_is_html: Treat body as HTML rather than plain text (Odoo 17+)

            Returns:
                Confirmation with the new mail.message ID.
            """
            result = await self._handle_post_message_tool(
                model,
                record_id,
                body,
                subtype,
                message_type,
                partner_ids,
                attachment_ids,
                body_is_html,
                ctx,
            )
            return PostMessageResult(**result)

        @self.app.tool(
            title="Aggregate Records",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
        async def aggregate_records(
            model: str,
            groupby: List[str],
            aggregates: Optional[List[str]] = None,
            domain: Optional[Any] = None,
            order: Optional[str] = None,
            limit: Optional[int] = None,
            offset: int = 0,
            ctx: Optional[Context] = None,
        ) -> AggregateResult:
            """Aggregate records server-side via Odoo's grouping methods.

            Use this tool whenever the question is "totals/counts/groupings",
            not "list of records". It pushes the aggregation down to Odoo
            instead of pulling raw records and reducing client-side.

            Dispatches by Odoo version: ``formatted_read_group`` on 19+
            (the new dedicated method), falls back to ``read_group`` on
            older versions with response-shape normalization. Callers see
            the same response shape on every supported version.

            Args:
                model: Odoo model name (e.g. 'sale.order')
                groupby: One or more group expressions. Field names, optionally
                    with a granularity suffix for date/datetime fields:
                    ``["date_order:month"]``, ``["partner_id"]``,
                    ``["partner_id", "date_order:year"]``.
                aggregates: Aggregate expressions of the form ``"field:operator"``
                    (sum, avg, min, max, count, count_distinct, array_agg, ...).
                    Examples: ``["amount_total:sum"]``, ``["id:count"]``.
                    If omitted or empty, defaults to ``["__count"]`` so each
                    group carries a count. Pass ``["__count", "amount_total:sum"]``
                    to get both.
                domain: Odoo domain filter — list, JSON string, or None.
                order: Sort expression over groupby keys / aggregates,
                    e.g. ``"date_order:month"`` or ``"amount_total:sum desc"``.
                limit: Maximum number of groups. Defaults to
                    ``ODOO_MCP_DEFAULT_LIMIT``; capped at ``ODOO_MCP_MAX_LIMIT``.
                offset: Number of groups to skip.

            Returns:
                ``AggregateResult`` with ``groups`` (list of dicts; each contains
                the groupby keys, ``__count``, and any requested aggregates),
                plus the echoed ``model``, ``groupby``, and ``aggregates``.

            Examples:
                # Sales by month
                aggregate_records(
                    "sale.order",
                    groupby=["date_order:month"],
                    aggregates=["amount_total:sum"],
                    domain=[["state", "in", ["sale", "done"]]],
                )

                # Partner count by country
                aggregate_records("res.partner", groupby=["country_id"])
            """
            result = await self._handle_aggregate_records_tool(
                model, groupby, aggregates, domain, order, limit, offset, ctx
            )
            return AggregateResult(**result)

        # Export tool: conditional on export_enabled flag.
        if self.config.export_enabled:

            @self.app.tool(
                title="Export Records",
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=True,
                ),
            )
            async def export_records(
                model: str,
                domain: Optional[Any] = None,
                fields: Optional[List[str]] = None,
                ctx: Optional[Context] = None,
            ) -> ExportResult:
                """Export filtered Odoo records to a CSV file on disk.

                Returns a file path, row count, file size, and 10-line preview.
                Bounded by ODOO_MCP_MAX_EXPORT_ROWS (default 10000).
                Use this for large data exports to avoid flooding the chat context.
                Use aggregate_records for summary statistics instead of exporting.

                Args:
                    model: Odoo model name (e.g., "res.partner")
                    domain: Odoo domain filter as a list of tuples/lists
                        (e.g., [["active", "=", True]])
                    fields: List of field names to export
                        (default: ["id", "display_name"])

                Returns:
                    ExportSuccessResult on success with file metadata.
                    Error variants on failure: ExportBlockedExceedsLimitError,
                    ExportAccessDeniedError, ExportDisabledError, ExportFileError.
                """
                return await self._handle_export_records_tool(model, domain, fields, ctx)

        # Two-key opt-in: invisible to the client unless both flags are set.
        if self.config.is_write_allowed and self.config.enable_method_calls:
            logger.info("call_model_method tool ENABLED (full YOLO + ODOO_MCP_ENABLE_METHOD_CALLS)")

            @self.app.tool(
                title="Call Model Method",
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
            )
            async def call_model_method(
                model: str,
                method: str,
                arguments: Optional[Union[List[Any], str]] = None,
                keyword_arguments: Optional[Union[Dict[str, Any], str]] = None,
                ctx: Optional[Context] = None,
            ) -> CallModelMethodResult:
                """Call a public Odoo model method via XML-RPC execute_kw.

                Workflow escape hatch for actions not covered by CRUD: posting an
                invoice (``account.move.action_post``), confirming a sale order
                (``sale.order.action_confirm``), validating a picking, etc.

                Available ONLY when the server runs with full YOLO and
                ``ODOO_MCP_ENABLE_METHOD_CALLS=true``. Odoo still enforces record
                rules and model ACLs for the authenticated user.

                Args:
                    model: Technical model name (e.g. ``account.move``).
                    method: Public Python identifier. Dotted, dashed, whitespace,
                        and ``_``-prefixed names are rejected.
                    arguments: Positional argument list for ``execute_kw``, as a
                        list or JSON-string. For recordset methods, the first
                        element is typically the list of ids: ``[[42]]`` runs on
                        id 42. Defaults to ``[]``.
                    keyword_arguments: Optional dict (or JSON-object string) of
                        keyword arguments for ``execute_kw`` (e.g. ``{"context": {...}}``).

                Returns:
                    ``CallModelMethodResult`` with the raw method return value in
                    ``result`` (bool/dict/list/None depending on the method).

                Prefer ``create_record`` / ``update_record`` / ``delete_record``
                when sufficient.
                """
                result = await self._handle_call_model_method_tool(
                    model, method, arguments, keyword_arguments, ctx
                )
                return CallModelMethodResult(**result)

    async def _handle_search_tool(
        self,
        model: str,
        domain: Optional[Any],
        fields: Optional[Any],
        limit: Optional[int],
        offset: int,
        order: Optional[str],
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle search tool request."""
        try:
            with perf_logger.track_operation("tool_search", model=model):
                # Check model access
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await self._ctx_info(ctx, f"Searching {model}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                parsed_domain = self._parse_domain_input(domain)

                # Handle fields parameter - can be string or list
                parsed_fields = fields
                if fields is not None and isinstance(fields, str):
                    # Parse string to list
                    try:
                        parsed_fields = json.loads(fields)
                        if not isinstance(parsed_fields, list):
                            raise ValidationError(
                                f"Fields must be a list, got {type(parsed_fields).__name__}"
                            )
                    except json.JSONDecodeError:
                        # Try Python literal eval as fallback
                        try:
                            import ast

                            parsed_fields = ast.literal_eval(fields)
                            if not isinstance(parsed_fields, list):
                                raise ValidationError(
                                    f"Fields must be a list, got {type(parsed_fields).__name__}"
                                )
                        except (ValueError, SyntaxError) as e:
                            raise ValidationError(
                                f"Invalid fields parameter. Expected JSON array or Python list, got: {fields[:100]}..."
                            ) from e

                # Set defaults
                if limit is None or limit <= 0:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                if offset < 0:
                    raise ValidationError(f"offset must be >= 0, got {offset}")

                # Get total count
                total_count = await asyncio.to_thread(
                    self.connection.search_count, model, parsed_domain
                )
                await self._ctx_progress(ctx, 1, 3, f"Found {total_count} records")

                # Search for records
                record_ids = await asyncio.to_thread(
                    self.connection.search,
                    model,
                    parsed_domain,
                    limit=limit,
                    offset=offset,
                    order=order,
                )

                # Determine which fields to fetch. An empty list means
                # "minimal/default" — Odoo would interpret [] as ALL fields,
                # so treat it like None (smart defaults).
                fields_to_fetch = parsed_fields
                if parsed_fields is None or parsed_fields == []:
                    # Use smart field selection to avoid serialization issues
                    fields_to_fetch = await asyncio.to_thread(self._get_smart_default_fields, model)
                    await self._ctx_info(ctx, f"Using smart field defaults for {model}")
                    logger.debug(
                        f"Using smart defaults for {model} search: {len(fields_to_fetch) if fields_to_fetch else 'all'} fields"
                    )
                elif parsed_fields == ["__all__"]:
                    # Explicit request for all fields
                    fields_to_fetch = None  # Odoo interprets None as all fields
                    await self._ctx_warning(
                        ctx,
                        f"Fetching ALL fields for {model} — may be slow or cause serialization errors",
                    )
                    logger.debug(f"Fetching all fields for {model} search")

                # Read records
                records = []
                if record_ids:
                    records = await asyncio.to_thread(
                        self.connection.read, model, record_ids, fields_to_fetch
                    )
                    # Process datetime fields in each record
                    records = await asyncio.to_thread(
                        lambda: [self._process_record_dates(record, model) for record in records]
                    )
                    # Coerce XML-RPC types (Binary, DateTime) Pydantic can't serialize
                    records = [_json_safe(record) for record in records]
                await self._ctx_info(ctx, f"Returning {len(records)} records")

                return {
                    "records": records,
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "model": model,
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in search_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Search failed: {sanitized_msg}") from e

    async def _handle_get_record_tool(
        self,
        model: str,
        record_id: int,
        fields: Optional[List[str]],
        ctx=None,
    ) -> RecordResult:
        """Handle get record tool request."""
        try:
            with perf_logger.track_operation("tool_get_record", model=model):
                # Check model access
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await self._ctx_info(ctx, f"Getting {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Determine which fields to fetch
                fields_to_fetch = fields
                use_smart_defaults = False
                total_fields = None
                field_selection_method = "explicit"

                if fields is None or fields == []:
                    # Use smart field selection. An empty list means
                    # "minimal/default" — Odoo would interpret [] as ALL fields.
                    fields_to_fetch = await asyncio.to_thread(self._get_smart_default_fields, model)
                    use_smart_defaults = True
                    field_selection_method = "smart_defaults"
                    logger.debug(
                        f"Using smart defaults for {model}: {len(fields_to_fetch) if fields_to_fetch else 'all'} fields"
                    )
                elif fields == ["__all__"]:
                    # Explicit request for all fields
                    fields_to_fetch = None  # Odoo interprets None as all fields
                    field_selection_method = "all"
                    logger.debug(f"Fetching all fields for {model}")
                else:
                    # Specific fields requested
                    logger.debug(f"Fetching specific fields for {model}: {fields}")

                # Read the record
                records = await asyncio.to_thread(
                    self.connection.read, model, [record_id], fields_to_fetch
                )

                if not records:
                    raise ValidationError(f"Record not found: {model} with ID {record_id}")

                # Process datetime fields in the record
                record = await asyncio.to_thread(self._process_record_dates, records[0], model)
                # Coerce XML-RPC types (Binary, DateTime) Pydantic can't serialize
                record = _json_safe(record)

                # Build metadata when using smart defaults
                metadata = None
                if use_smart_defaults:
                    try:
                        all_fields_info = await asyncio.to_thread(self.connection.fields_get, model)
                        total_fields = len(all_fields_info)
                    except Exception:
                        pass

                    metadata = FieldSelectionMetadata(
                        fields_returned=len(record),
                        field_selection_method=field_selection_method,
                        total_fields_available=total_fields,
                        note=f"Limited fields returned for performance. Use fields=['__all__'] for all fields or see odoo://{model}/fields for available fields.",
                    )

                return RecordResult(record=record, metadata=metadata)

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get record: {sanitized_msg}") from e

    async def _handle_list_models_tool(self, ctx=None) -> Dict[str, Any]:
        """Handle list models tool request with permissions."""
        try:
            with perf_logger.track_operation("tool_list_models"):
                await self._ctx_info(ctx, "Listing available models...")
                # Check if YOLO mode is enabled
                if self.config.is_yolo_enabled:
                    # Query actual models from ir.model in YOLO mode
                    try:
                        # Exclude transient models and system models (ir.%/base.%),
                        # except a small whitelist of useful ir.* models.
                        domain = [
                            "&",
                            ("transient", "=", False),
                            "|",
                            (
                                "model",
                                "in",
                                [
                                    "ir.attachment",
                                    "ir.model",
                                    "ir.model.fields",
                                    "ir.config_parameter",
                                ],
                            ),
                            "&",
                            ("model", "not like", "ir.%"),
                            ("model", "not like", "base.%"),
                        ]

                        # Query models from database
                        model_records = await asyncio.to_thread(
                            self.connection.search_read,
                            "ir.model",
                            domain,
                            ["model", "name"],
                            order="name ASC",
                            limit=200,  # Reasonable limit for practical use
                        )

                        # Prepare response with YOLO mode metadata
                        mode_desc = (
                            "READ-ONLY" if self.config.yolo_mode == "read" else "FULL ACCESS"
                        )
                        await self._ctx_info(
                            ctx,
                            f"YOLO mode ({mode_desc}): found {len(model_records)} models",
                        )

                        # Create metadata about YOLO mode
                        yolo_metadata = {
                            "enabled": True,
                            "level": self.config.yolo_mode,  # "read" or "true"
                            "description": mode_desc,
                            "warning": "🚨 All models accessible without MCP security!",
                            "operations": {
                                "read": True,
                                "write": self.config.yolo_mode == "true",
                                "create": self.config.yolo_mode == "true",
                                "unlink": self.config.yolo_mode == "true",
                            },
                        }

                        # Process actual models (clean data without permissions)
                        models_list = []
                        for record in model_records:
                            model_entry = {
                                "model": record["model"],
                                "name": record["name"] or record["model"],
                            }
                            models_list.append(model_entry)

                        logger.info(
                            f"YOLO mode ({mode_desc}): Listed {len(model_records)} models from database"
                        )

                        return {
                            "yolo_mode": yolo_metadata,
                            "models": models_list,
                            "total": len(models_list),
                        }

                    except Exception as e:
                        logger.error(f"Failed to query models in YOLO mode: {e}")
                        # Return error in consistent structure
                        mode_desc = (
                            "READ-ONLY" if self.config.yolo_mode == "read" else "FULL ACCESS"
                        )
                        return {
                            "yolo_mode": {
                                "enabled": True,
                                "level": self.config.yolo_mode,
                                "description": mode_desc,
                                "warning": f"⚠️ Error querying models: {str(e)}",
                                "operations": {
                                    "read": False,
                                    "write": False,
                                    "create": False,
                                    "unlink": False,
                                },
                            },
                            "models": [],
                            "total": 0,
                            "error": str(e),
                        }

                # Standard mode: Get models from MCP access controller
                models = await asyncio.to_thread(self.access_controller.get_enabled_models)

                # Enrich with permissions for each model
                if models:
                    await self._ctx_info(ctx, f"Enriching {len(models)} models...")
                enriched_models = []
                for model_info in models:
                    model_name = model_info["model"]
                    try:
                        # Get permissions for this model
                        permissions = await asyncio.to_thread(
                            self.access_controller.get_model_permissions, model_name
                        )
                        enriched_model = {
                            "model": model_name,
                            "name": model_info["name"],
                            "operations": {
                                "read": permissions.can_read,
                                "write": permissions.can_write,
                                "create": permissions.can_create,
                                "unlink": permissions.can_unlink,
                            },
                        }
                        enriched_models.append(enriched_model)
                    except Exception as e:
                        # If we can't get permissions for a model, include it with all operations false
                        logger.warning(f"Failed to get permissions for {model_name}: {e}")
                        enriched_model = {
                            "model": model_name,
                            "name": model_info["name"],
                            "operations": {
                                "read": False,
                                "write": False,
                                "create": False,
                                "unlink": False,
                            },
                        }
                        enriched_models.append(enriched_model)

                # Return proper JSON structure with enriched models array
                return {"models": enriched_models}
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error in list_models tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to list models: {sanitized_msg}") from e

    async def _handle_list_resource_templates_tool(self, ctx=None) -> Dict[str, Any]:
        """Handle list resource templates tool request."""
        try:
            await self._ctx_info(ctx, "Listing resource templates...")
            # Get list of enabled models that can be used with resources.
            # In YOLO mode get_enabled_models() returns [] as an
            # "all models allowed" sentinel — report that explicitly
            # instead of claiming zero models are usable.
            if self.config.is_yolo_enabled:
                model_names = None
            else:
                enabled_models = await asyncio.to_thread(self.access_controller.get_enabled_models)
                model_names = [m["model"] for m in enabled_models]

            # Define the resource templates
            templates = [
                {
                    "uri_template": "odoo://{model}/record/{record_id}",
                    "description": "Get a specific record by ID",
                    "parameters": {
                        "model": "Odoo model name (e.g., res.partner)",
                        "record_id": "Record ID (e.g., 10)",
                    },
                    "example": "odoo://res.partner/record/10",
                },
                {
                    "uri_template": "odoo://{model}/search",
                    "description": "Basic search returning first 10 records",
                    "parameters": {
                        "model": "Odoo model name",
                    },
                    "example": "odoo://res.partner/search",
                    "note": "Query parameters are not supported. Use search_records tool for advanced queries.",
                },
                {
                    "uri_template": "odoo://{model}/count",
                    "description": "Count all records in a model",
                    "parameters": {
                        "model": "Odoo model name",
                    },
                    "example": "odoo://res.partner/count",
                    "note": "Query parameters are not supported. Use search_records tool for filtered counts.",
                },
                {
                    "uri_template": "odoo://{model}/fields",
                    "description": "Get field definitions for a model",
                    "parameters": {"model": "Odoo model name"},
                    "example": "odoo://res.partner/fields",
                },
            ]

            # Return the resource template information
            base_note = (
                "Resource URIs do not support query parameters. Use tools "
                "(search_records, get_record) for advanced operations with "
                "filtering, pagination, and field selection."
            )
            if model_names is None:
                return {
                    "templates": templates,
                    "enabled_models": [],
                    "total_models": None,
                    "note": f"YOLO mode: ALL models are available with these templates. {base_note}",
                }
            return {
                "templates": templates,
                "enabled_models": model_names[:10],  # Show first 10 as examples
                "total_models": len(model_names),
                "note": base_note,
            }

        except Exception as e:
            logger.error(f"Error in list_resource_templates tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to list resource templates: {sanitized_msg}") from e

    async def _handle_create_record_tool(
        self,
        model: str,
        values: Dict[str, Any],
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle create record tool request."""
        try:
            with perf_logger.track_operation("tool_create_record", model=model):
                # Check model access
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "create"
                )
                await self._ctx_info(ctx, f"Creating record in {model}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate required fields
                if not values:
                    raise ValidationError("No values provided for record creation")

                # Create the record
                record_id = await asyncio.to_thread(self.connection.create, model, values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                # Only use universally available fields (not all models have 'name')
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                records = await asyncio.to_thread(
                    self.connection.read, model, [record_id], essential_fields
                )
                if not records:
                    raise ValidationError(
                        f"Failed to read created record: {model} with ID {record_id}"
                    )

                # Process dates in the minimal record
                record = await asyncio.to_thread(self._process_record_dates, records[0], model)

                record_url = self.connection.build_record_url(model, record_id)

                return {
                    "success": True,
                    "record": record,
                    "url": record_url,
                    "message": f"Successfully created {model} record with ID {record_id}",
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in create_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to create record: {sanitized_msg}") from e

    async def _handle_update_record_tool(
        self,
        model: str,
        record_id: int,
        values: Dict[str, Any],
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle update record tool request."""
        try:
            with perf_logger.track_operation("tool_update_record", model=model):
                # Check model access
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Updating {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not values:
                    raise ValidationError("No values provided for record update")

                # Check if record exists (only fetch ID to verify existence)
                existing = await asyncio.to_thread(self.connection.read, model, [record_id], ["id"])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Update the record
                success = await asyncio.to_thread(self.connection.write, model, [record_id], values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                # Only use universally available fields (not all models have 'name')
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                records = await asyncio.to_thread(
                    self.connection.read, model, [record_id], essential_fields
                )
                if not records:
                    raise ValidationError(
                        f"Failed to read updated record: {model} with ID {record_id}"
                    )

                # Process dates in the minimal record
                record = await asyncio.to_thread(self._process_record_dates, records[0], model)

                record_url = self.connection.build_record_url(model, record_id)

                return {
                    "success": success,
                    "record": record,
                    "url": record_url,
                    "message": f"Successfully updated {model} record with ID {record_id}",
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to update record: {sanitized_msg}") from e

    async def _handle_delete_record_tool(
        self,
        model: str,
        record_id: int,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle delete record tool request."""
        try:
            with perf_logger.track_operation("tool_delete_record", model=model):
                # Check model access
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "unlink"
                )
                await self._ctx_info(ctx, f"Deleting {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Check if record exists and get display info
                existing = await asyncio.to_thread(
                    self.connection.read, model, [record_id], ["id", "display_name"]
                )
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Store some info about the record before deletion.
                # Odoo returns False (not a missing key) for records without
                # a display name (e.g. mail.message) — falling back via
                # .get's default would leave False and break DeleteResult.
                record_name = existing[0].get("display_name") or f"ID {record_id}"

                # Delete the record
                success = await asyncio.to_thread(self.connection.unlink, model, [record_id])

                return {
                    "success": success,
                    "deleted_id": record_id,
                    "deleted_name": record_name,
                    "message": f"Successfully deleted {model} record '{record_name}' (ID: {record_id})",
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in delete_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to delete record: {sanitized_msg}") from e

    async def _handle_post_message_tool(
        self,
        model: str,
        record_id: int,
        body: str,
        subtype: str,
        message_type: str,
        partner_ids: Optional[List[int]],
        attachment_ids: Optional[List[int]],
        body_is_html: bool,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle post message tool request."""
        subtype_xmlid_map = {
            "note": "mail.mt_note",
            "comment": "mail.mt_comment",
        }
        try:
            with perf_logger.track_operation("tool_post_message", model=model):
                # Check model access — message_post mutates the record
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Posting message to {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate body before any XML-RPC call
                if not body or not body.strip():
                    raise ValidationError("body must not be empty")

                # Build kwargs — omit partner_ids/attachment_ids when None
                # (empty list means "clear all" in some Odoo contexts)
                kwargs: Dict[str, Any] = {
                    "body": body,
                    "message_type": message_type,
                    "subtype_xmlid": subtype_xmlid_map[subtype],
                }
                if partner_ids is not None:
                    kwargs["partner_ids"] = partner_ids
                if attachment_ids is not None:
                    kwargs["attachment_ids"] = attachment_ids
                if body_is_html:
                    # Odoo 19 escapes any plain str body — opt-in flag preserves HTML
                    kwargs["body_is_html"] = True

                # Call message_post; translate the "no mail.thread" error before
                # the outer ladder turns it into a generic "Connection error".
                try:
                    raw = await asyncio.to_thread(
                        self.connection.execute_kw, model, "message_post", [record_id], kwargs
                    )
                except OdooConnectionError as e:
                    err_msg = str(e)
                    if "message_post" in err_msg and (
                        "has no attribute" in err_msg
                        or "AttributeError" in err_msg
                        or "does not exist" in err_msg
                    ):
                        raise ValidationError(
                            f"Model '{model}' does not support chatter "
                            "(no mail.thread inheritance)."
                        ) from e
                    raise

                # Coerce return value to int message_id
                if isinstance(raw, bool) or raw is None:
                    raise ValidationError(f"Unexpected return from message_post: {raw!r}")
                if isinstance(raw, int):
                    message_id = raw
                elif isinstance(raw, list) and raw and isinstance(raw[0], int):
                    message_id = raw[0]
                else:
                    raise ValidationError(f"Unexpected return from message_post: {raw!r}")

                return {
                    "success": True,
                    "message_id": message_id,
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in post_message tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to post message: {sanitized_msg}") from e

    # Metadata keys we always preserve in normalized read_group output.
    # Anything else not in the requested groupby/aggregates is filtered
    # out — read_group with empty ``fields=`` defaults to ALL aggregator
    # fields on the model, which leaks unrelated numeric fields.
    _READ_GROUP_META_KEYS = frozenset({"__count", "__extra_domain", "__range", "__fold"})

    def _call_read_group_normalized(
        self,
        model: str,
        domain: List[Any],
        groupby: List[str],
        aggregates: List[str],
        order: Optional[str],
        limit: int,
        offset: int,
    ) -> List[Dict[str, Any]]:
        """Call legacy ``read_group`` and normalize its response shape.

        Odoo < 19 doesn't have ``formatted_read_group``. ``read_group`` is
        the long-standing alternative; with ``lazy=False`` its response is
        already close to the v19 shape. Three normalizations:

        * ``__domain`` → ``__extra_domain`` (key rename, per v19 convention).
        * Aggregate keys: read_group emits aggregate values keyed by the
          bare field name (e.g. ``"id:count"`` is returned as ``"id"``);
          rename back to ``"field:op"`` to match v19.
        * Bucket key whitelist: drop fields the caller didn't request.
          read_group with empty ``fields=`` returns all aggregator fields
          on the model (e.g. ``message_bounce``, ``partner_latitude``);
          formatted_read_group never does that. Filter to keep only what
          the caller asked for plus metadata keys (``__count``, etc.).

        Translates kwargs:
            * ``aggregates`` → ``fields`` (drop ``__count``; read_group emits
              it implicitly when ``lazy=False``).
            * ``order`` → ``orderby`` (omit entirely when ``None`` so
              read_group uses its default).
        """
        # __count is implicit in read_group; passing it as a field raises a fault.
        fields_kwarg = [a for a in aggregates if a != "__count"]

        kwargs: Dict[str, Any] = {
            "fields": fields_kwarg,
            "groupby": groupby,
            "limit": limit,
            "offset": offset,
            "lazy": False,
        }
        if order is not None:
            kwargs["orderby"] = order

        groups = self.connection.execute_kw(model, "read_group", [domain], kwargs)

        # Aggregate key rename: build a list of (bare_field, full_expr)
        # pairs to restore after read_group strips the operator suffix.
        # Skip aggregates whose bare field collides with a groupby key —
        # the groupby value already lives under that key.
        groupby_field_names = {g.split(":", 1)[0] for g in groupby}
        agg_renames = [
            (a.split(":", 1)[0], a)
            for a in fields_kwarg
            if ":" in a and a.split(":", 1)[0] not in groupby_field_names
        ]

        # Whitelist of keys allowed in the final bucket: groupby specs +
        # requested aggregates (post-rename) + known metadata keys.
        allowed_keys = self._READ_GROUP_META_KEYS | set(groupby) | set(fields_kwarg)

        normalized: List[Dict[str, Any]] = []
        for bucket in groups:
            if "__domain" in bucket:
                bucket["__extra_domain"] = bucket.pop("__domain")
            for bare, full in agg_renames:
                if bare in bucket and full != bare:
                    bucket[full] = bucket.pop(bare)
            normalized.append({k: v for k, v in bucket.items() if k in allowed_keys})
        return normalized

    async def _handle_aggregate_records_tool(
        self,
        model: str,
        groupby: List[str],
        aggregates: Optional[List[str]],
        domain: Optional[Any],
        order: Optional[str],
        limit: Optional[int],
        offset: int,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle aggregate_records tool request."""
        try:
            with perf_logger.track_operation("tool_aggregate_records", model=model):
                # Access check (read permission — same as search_records)
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await self._ctx_info(ctx, f"Aggregating {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate groupby — empty groupby collapses to a single
                # bucket, which search_count already covers.
                if not groupby:
                    raise ValidationError(
                        "groupby must not be empty (use search_count for an unfiltered total)."
                    )

                parsed_domain = self._parse_domain_input(domain)

                # Limit defaults & capping (mirror search_records)
                if limit is None or limit <= 0:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                if offset < 0:
                    raise ValidationError(f"offset must be >= 0, got {offset}")

                # Default to ['__count'] when caller omits aggregates —
                # otherwise formatted_read_group returns only the groupby
                # keys with no quantitative data, which defeats the tool.
                effective_aggregates = aggregates if aggregates else ["__count"]

                # Version dispatch: formatted_read_group is Odoo 19+ only;
                # fall back to read_group with response normalization on
                # older versions. When the version is unknown (None), assume
                # newer and let the XML-RPC fault surface — the caller can
                # set ODOO_DB or check the connection log.
                major = await asyncio.to_thread(self.connection.get_major_version)
                if major is not None and major < 19:
                    groups = await asyncio.to_thread(
                        self._call_read_group_normalized,
                        model,
                        parsed_domain,
                        groupby,
                        effective_aggregates,
                        order,
                        limit,
                        offset,
                    )
                else:
                    kwargs: Dict[str, Any] = {
                        "groupby": groupby,
                        "aggregates": effective_aggregates,
                        "limit": limit,
                        "offset": offset,
                    }
                    if order is not None:
                        kwargs["order"] = order
                    groups = await asyncio.to_thread(
                        self.connection.execute_kw,
                        model,
                        "formatted_read_group",
                        [parsed_domain],
                        kwargs,
                    )

                await self._ctx_info(ctx, f"Returning {len(groups)} groups")

                return {
                    "groups": groups,
                    "model": model,
                    "groupby": groupby,
                    "aggregates": effective_aggregates,
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in aggregate_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Aggregation failed: {sanitized_msg}") from e

    async def _handle_export_records_tool(
        self,
        model: str,
        domain: Optional[Any],
        fields: Optional[List[str]],
        ctx=None,
    ) -> ExportResult:
        """Handle export_records tool request."""
        try:
            with perf_logger.track_operation("tool_export_records", model=model):
                # Feature flag check
                if not self.config.export_enabled:
                    return ExportDisabledError(
                        message="export_records is disabled (ODOO_MCP_EXPORT_ENABLED=false)."
                    )

                # Authentication check
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                await self._ctx_info(ctx, f"Exporting {model}...")

                # Parse domain
                parsed_domain = self._parse_domain_input(domain)

                # Resolve default fields when None or empty
                if fields is None or fields == []:
                    fields = await asyncio.to_thread(self._get_smart_default_fields, model)

                # Execute export in thread (heavy I/O, XML-RPC + filesystem)
                # Note: execute_export expects OdooConfig, not ExportConfig
                result = await asyncio.to_thread(
                    execute_export,
                    model=model,
                    domain=parsed_domain,
                    fields=fields,
                    config=self.config,
                    odoo_connection=self.connection,
                    access_controller=self.access_controller,
                )

                # Result is already a typed Pydantic model (success or error variant)
                await self._ctx_info(
                    ctx,
                    f"Export completed: {result.row_count if hasattr(result, 'row_count') else 'error'}",
                )
                return result

        except ValidationError:
            raise
        except OdooConnectionError as e:
            raise ValidationError(
                f"Export failed: {ErrorSanitizer.sanitize_message(str(e))}"
            ) from e
        except Exception as e:
            logger.error(f"Error in export_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Export failed: {sanitized_msg}") from e

    @staticmethod
    def _parse_execute_kw_arguments(value: Optional[Any]) -> List[Any]:
        """Coerce the ``arguments`` parameter to a list (JSON-only)."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            if len(value) > _MAX_JSON_PARAM_BYTES:
                raise ValidationError(
                    f"arguments JSON-string exceeds {_MAX_JSON_PARAM_BYTES} bytes"
                )
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValidationError(
                    f"Invalid arguments parameter. Expected JSON array, got: {value[:100]}"
                ) from e
            if not isinstance(parsed, list):
                raise ValidationError(f"arguments must be a list, got {type(parsed).__name__}")
            return parsed
        raise ValidationError(
            f"arguments must be a list or JSON-string, got {type(value).__name__}"
        )

    @staticmethod
    def _parse_execute_kw_kwargs(value: Optional[Any]) -> Dict[str, Any]:
        """Coerce the ``keyword_arguments`` parameter to a dict (JSON-only)."""
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            if len(value) > _MAX_JSON_PARAM_BYTES:
                raise ValidationError(
                    f"keyword_arguments JSON-string exceeds {_MAX_JSON_PARAM_BYTES} bytes"
                )
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValidationError(
                    f"Invalid keyword_arguments parameter. Expected JSON object, got: {value[:100]}"
                ) from e
            if not isinstance(parsed, dict):
                raise ValidationError(
                    f"keyword_arguments must be a dict, got {type(parsed).__name__}"
                )
            return parsed
        raise ValidationError(
            f"keyword_arguments must be a dict or JSON-string, got {type(value).__name__}"
        )

    async def _handle_call_model_method_tool(
        self,
        model: str,
        method: str,
        arguments: Optional[Any],
        keyword_arguments: Optional[Any],
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle call_model_method tool request."""
        try:
            with perf_logger.track_operation("tool_call_model_method", model=model):
                model = (model or "").strip()
                method = (method or "").strip()
                if not model:
                    raise ValidationError("model must not be empty")
                if not method:
                    raise ValidationError("method must not be empty")
                if not _PUBLIC_METHOD_RE.fullmatch(method):
                    raise ValidationError(
                        f"Refusing to call '{method}': only public ASCII Python "
                        "identifiers are accepted; dotted, dashed, whitespace, "
                        "non-ASCII, and _-prefixed names are rejected."
                    )

                # No-op under full YOLO; placeholder if the gate ever loosens.
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Calling {model}.{method}(...)")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                args_list = self._parse_execute_kw_arguments(arguments)
                kwargs_dict = self._parse_execute_kw_kwargs(keyword_arguments)

                # Audit only what was called, not the values — kwargs may carry PII.
                logger.info(
                    "call_model_method invoked: model=%s method=%s args_len=%d kwargs_keys=%s",
                    model,
                    method,
                    len(args_list),
                    sorted(kwargs_dict.keys()),
                )

                rpc_result = await asyncio.to_thread(
                    self.connection.execute_kw, model, method, args_list, kwargs_dict
                )

                return {
                    "success": True,
                    "result": _json_safe(rpc_result),
                    "message": f"Successfully called {model}.{method}",
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in call_model_method tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to call model method: {sanitized_msg}") from e


def register_tools(
    app: FastMCP,
    connection: OdooConnection,
    access_controller: AccessController,
    config: OdooConfig,
) -> OdooToolHandler:
    """Register all Odoo tools with the FastMCP app.

    Args:
        app: FastMCP application instance
        connection: Odoo connection instance
        access_controller: Access control instance
        config: Odoo configuration instance

    Returns:
        The tool handler instance
    """
    handler = OdooToolHandler(app, connection, access_controller, config)
    logger.info("Registered Odoo MCP tools")
    return handler
