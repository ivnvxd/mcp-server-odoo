"""MCP tool handlers for Odoo operations.

This module implements MCP tools for performing operations on Odoo data.
Tools are different from resources - they can have side effects and perform
actions like creating, updating, or deleting records.
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .access_control import AccessControlError, AccessController
from .config import OdooConfig
from .error_handling import (
    NotFoundError,
    ValidationError,
)
from .error_sanitizer import ErrorSanitizer
from .logging_config import get_logger, perf_logger
from .odoo_connection import OdooConnection, OdooConnectionError
from .schema_guard import (
    SchemaGuardError,
    domain_field_names,
    normalize_domain,
    validate_fields,
)
from .schemas import (
    AccessCheckResult,
    AggregateResult,
    CallMethodResult,
    CreateResult,
    DefaultsResult,
    DeleteResult,
    FieldSelectionMetadata,
    FieldsResult,
    ModelsResult,
    PostMessageResult,
    RecordResult,
    ResourceTemplatesResult,
    SearchResult,
    UpdateResult,
)

logger = get_logger(__name__)

# Methods callable via the `call_method` tool, keyed by model.
#
# This is a deliberately tight allowlist: `call_method` can invoke an arbitrary
# model method, so only vetted, side-effect-safe helpers are exposed. Anything
# not listed here is rejected before reaching Odoo.
#
# The bpm.process helpers below are read-only or creation-only — they never
# enable or promote a workflow (foxlogik_bpmn_workflow's bpm.process.write guard
# enforces draft/disabled server-side regardless).
METHOD_CALL_ALLOWLIST: Dict[str, set] = {
    "bpm.process": {
        "validate_bpmn_xml",
        "get_builder_reference",
        "create_draft_process_from_spec",
    },
}

# Minimum model operation each allowlisted method requires, for access-control
# validation (the model must be MCP-enabled for this operation). Defaults to
# "read" when a (model, method) pair is absent.
METHOD_REQUIRED_OPERATION: Dict[tuple, str] = {
    ("bpm.process", "validate_bpmn_xml"): "read",
    ("bpm.process", "get_builder_reference"): "read",
    ("bpm.process", "create_draft_process_from_spec"): "create",
}

# Compact default attribute set for get_fields — full fields_get output is very
# large; these cover what a model needs to build a create/write proposal.
DEFAULT_FIELD_ATTRIBUTES = [
    "string",
    "type",
    "required",
    "readonly",
    "relation",
    "selection",
    "help",
    "store",
]

# Aggregate functions that need a specific field type. Odoo checks only the
# function name, so name:sum reaches SQL and fails there as an internal error.
# The other functions (max, min, count, array_agg, ...) take any column.
_TYPED_AGGREGATES = {
    "sum": ("integer", "float", "monetary"),
    "avg": ("integer", "float", "monetary"),
    "bool_and": ("boolean",),
    "bool_or": ("boolean",),
}

# Metadata keys always preserved in normalized read_group output. Anything else
# not in the requested groupby/aggregates is filtered out — read_group with an
# empty ``fields=`` defaults to ALL aggregator fields on the model.
_READ_GROUP_META_KEYS = frozenset({"__count", "__extra_domain", "__range", "__fold"})


def _odoo16_group_order(order: str, aggregates: List[str]) -> str:
    """Rewrite an aggregate ``order`` for Odoo 16's ``read_group``.

    Odoo 16 names an aggregate column by its bare field, so it refuses
    ``list_price:sum desc`` as an invalid field and takes ``list_price desc``.
    It cannot order by the group count at all. Odoo 17 takes both forms.
    """
    terms = []
    for term in order.split(","):
        parts = term.split()
        if not parts:
            continue
        if parts[0] == "__count":
            raise ValidationError(
                "Odoo 16 cannot order groups by __count. Order by a groupby key or an "
                "aggregate, or sort the returned groups yourself."
            )
        if parts[0] in aggregates:
            parts[0] = parts[0].split(":", 1)[0]
        terms.append(" ".join(parts))
    return ", ".join(terms)


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

    def _effective_user_id(self, user_id: Optional[int]) -> Optional[int]:
        """Resolve the user id every tool call actually executes as.

        When the session is pinned to an end-user (ODOO_ACT_AS_UID), that id is
        authoritative and CLAMPS the call: any user_id the model supplied is
        ignored, so prompt-injection cannot escalate to another user or to the
        admin service account. When not pinned, the model-supplied user_id (if
        any) is used unchanged — the trusted-automation default.
        """
        pinned = self.config.act_as_uid
        if pinned is not None:
            if user_id is not None and user_id != pinned:
                logger.warning(
                    "MCP session pinned to uid=%s; ignoring model-supplied user_id=%s",
                    pinned, user_id,
                )
            return pinned
        return user_id

    def _resolve_record_id(
        self, record_id: Optional[int], res_id: Optional[int]
    ) -> int:
        """Accept res_id as an alias for record_id.

        Callers that have just been reading chatter carry Odoo's own naming
        across: mail.message, ir.attachment and every other pointer model spell
        this res_id, so the alias arrives on get_record often enough to be worth
        honouring. Rejected in production by argument validation, before the call
        reached Odoo at all, so accepting it changes no successful behaviour.
        """
        if record_id is not None and res_id is not None and record_id != res_id:
            raise ValidationError(
                f"record_id ({record_id}) and res_id ({res_id}) disagree. "
                f"Pass one; res_id is accepted as an alias for record_id."
            )
        resolved = record_id if record_id is not None else res_id
        if resolved is None:
            raise ValidationError("record_id is required (res_id is accepted as an alias).")
        return resolved

    def _schema_for(self, model: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Cached field definitions for *model*, or None when unavailable.

        Never raises: the guards that consume this fail open, because an
        unreadable schema is not evidence that the caller got the field wrong.
        """
        try:
            return self._fields_get_for(model, user_id)
        except Exception:  # noqa: BLE001 — advisory only, must never fail a call
            logger.debug("Field definitions unavailable for %s; skipping field validation", model)
            return None

    def _guard_domain(self, model: str, domain: Any) -> Any:
        """Normalize a domain, converting a guard rejection into ValidationError."""
        try:
            return normalize_domain(domain, model=model)
        except SchemaGuardError as e:
            raise ValidationError(str(e)) from e

    def _guard_fields(
        self,
        model: str,
        requested: Optional[List[str]],
        user_id: Optional[int] = None,
        context: str = "fields",
        extra: Optional[List[str]] = None,
    ) -> None:
        """Reject unknown field names before the RPC, naming the closest real ones."""
        names = list(requested or [])
        if extra:
            names += extra
        if not names:
            return
        schema = self._schema_for(model, user_id=user_id)
        try:
            validate_fields(model, names, schema, context=context)
        except SchemaGuardError as e:
            raise ValidationError(str(e)) from e

    @staticmethod
    def _access_hint(model: str, fields: Any, message: str) -> str:
        """Append the missing next step to an Odoo access refusal.

        Odoo names the groups but not the remedy, and when the refusal came from
        a relational field pulled in by ``fields=["__all__"]`` it names a model
        the caller never asked for — which reads as "this record is unreadable"
        when the record is perfectly readable with an explicit field list.
        """
        if "not allowed to access" not in message and "access" not in message.lower():
            return message

        wants_all = fields == ["__all__"] or fields == "__all__"
        blocked = re.search(r"\(([a-z_][a-z0-9_.]*\.[a-z0-9_.]+)\)", message)
        other_model = blocked.group(1) if blocked and blocked.group(1) != model else None

        if wants_all and other_model:
            return (
                f"{message} This refusal came from '{other_model}', pulled in by "
                f'fields=["__all__"] on {model}. Retry with an explicit field list to read '
                f"{model} without it."
            )
        return (
            f"{message} Retrying with the same arguments will fail identically — "
            f"request the missing group, or read a model you have access to."
        )

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

    def _fields_get_for(
        self, model: str, user_id: Optional[int] = None, attributes: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """fields_get, computed for a specific user when one is given.

        The per-user path matters: Odoo drops fields protected by ``groups=``
        the user lacks from THEIR fields_get. Deriving smart defaults from the
        admin connection instead would select protected fields and make the
        subsequent pinned read fail with AccessError for normal users.
        """
        if user_id is not None:
            kwargs: Dict[str, Any] = {}
            if attributes:
                kwargs["attributes"] = attributes
            return self.connection.execute_kw_as_user(
                user_id, model, "fields_get", [], kwargs
            )
        return self.connection.fields_get(model, attributes=attributes)

    def _get_smart_default_fields(
        self, model: str, user_id: Optional[int] = None
    ) -> Optional[List[str]]:
        """Get smart default fields for a model using field importance scoring.

        Args:
            model: The Odoo model name
            user_id: When set, score only the fields visible to this user

        Returns:
            List of field names to include by default, or None if unable to determine
        """
        try:
            # Get all field definitions (user-scoped when pinned/impersonating)
            fields_info = self._fields_get_for(model, user_id)

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
            limit: int = 10,
            offset: int = 0,
            order: Optional[str] = None,
            user_id: Optional[int] = None,
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
                    - ["__all__"] or '["__all__"]': Returns ALL fields (warning: may cause serialization errors)
                limit: Maximum number of records to return
                offset: Number of records to skip
                order: Sort order (e.g., 'name asc')
                user_id: Optional Odoo user ID. When provided the search runs
                    under that user's security context (record rules and access
                    rights are enforced for that user). Requires the
                    foxlogik_mcp_proxy module to be installed.

            Returns:
                Search results with records, total count, and pagination info
            """
            result = await self._handle_search_tool(
                model, domain, fields, limit, offset, order, ctx,
                user_id=self._effective_user_id(user_id),
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
            record_id: Optional[int] = None,
            fields: Optional[List[str]] = None,
            user_id: Optional[int] = None,
            res_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> RecordResult:
            """Get a specific record by ID with smart field selection.

            This tool supports selective field retrieval to optimize performance and response size.
            By default, returns a smart selection of commonly-used fields based on the model's field metadata.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID
                res_id: Accepted as an alias for record_id (Odoo names this field
                    res_id on mail.message and other pointer models, and callers
                    working with chatter routinely carry that name across)
                fields: Field selection options:
                    - None (default): Returns smart selection of common fields
                    - ["field1", "field2", ...]: Returns only specified fields
                    - ["__all__"]: Returns ALL fields (warning: can be very large)
                user_id: Optional Odoo user ID. When provided the read runs
                    under that user's security context. Requires the
                    foxlogik_mcp_proxy module to be installed.

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
            return await self._handle_get_record_tool(
                model,
                self._resolve_record_id(record_id, res_id),
                fields,
                ctx,
                user_id=self._effective_user_id(user_id),
            )

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
            user_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> CreateResult:
            """Create a new record in an Odoo model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                values: Field values for the new record
                user_id: Optional Odoo user ID. When provided the create runs
                    under that user's security context. Requires the
                    foxlogik_mcp_proxy module to be installed.

            Returns:
                Created record details with ID, URL, and confirmation.
            """
            result = await self._handle_create_record_tool(
                model, values, ctx, user_id=self._effective_user_id(user_id)
            )
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
            values: Dict[str, Any],
            record_id: Optional[int] = None,
            user_id: Optional[int] = None,
            res_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> UpdateResult:
            """Update an existing record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                values: Field values to update
                record_id: The record ID to update
                res_id: Accepted as an alias for record_id
                user_id: Optional Odoo user ID. When provided the write runs
                    under that user's security context. Requires the
                    foxlogik_mcp_proxy module to be installed.

            Returns:
                Updated record details with confirmation.
            """
            result = await self._handle_update_record_tool(
                model,
                self._resolve_record_id(record_id, res_id),
                values,
                ctx,
                user_id=self._effective_user_id(user_id),
            )
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
            record_id: Optional[int] = None,
            user_id: Optional[int] = None,
            res_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> DeleteResult:
            """Delete a record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to delete
                res_id: Accepted as an alias for record_id
                user_id: Optional Odoo user ID. When provided the delete runs
                    under that user's security context. Requires the
                    foxlogik_mcp_proxy module to be installed.

            Returns:
                Deletion confirmation with the deleted record's name and ID.
            """
            result = await self._handle_delete_record_tool(
                model,
                self._resolve_record_id(record_id, res_id),
                ctx,
                user_id=self._effective_user_id(user_id),
            )
            return DeleteResult(**result)

        @self.app.tool(
            title="Post Message",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def post_message(
            model: str,
            body: str,
            record_id: Optional[int] = None,
            res_id: Optional[int] = None,
            subtype: str = "comment",
            message_type: str = "comment",
            subject: Optional[str] = None,
            partner_ids: Optional[List[int]] = None,
            attachment_ids: Optional[List[int]] = None,
            author_id: Optional[int] = None,
            user_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> PostMessageResult:
            """Post a message to a record's chatter.

            Use this rather than creating a mail.message directly: this goes
            through message_post, so followers are notified, @mentions resolve
            and the bus event fires — a hand-built mail.message does none of
            that. Returns the new message's ID.

            Args:
                model: Model of the thread to post on (e.g. 'project.task').
                body: Message body. HTML is rendered, not escaped.
                record_id: ID of the record to post on.
                res_id: Accepted as an alias for record_id.
                subtype: 'comment' (default, notifies followers) or 'note'
                    (internal). A full xmlid such as 'mail.mt_comment' also works.
                message_type: 'comment' (default) or 'notification'.
                subject: Optional message subject.
                partner_ids: Partner IDs to notify — the @mention recipients.
                attachment_ids: IDs of existing ir.attachment records to attach.
                author_id: Partner ID to post as. Defaults to the calling user's
                    partner. Posting under another partner's name requires the
                    connection user to be an Odoo system administrator.
                user_id: Optional Odoo user ID to post as. Requires the
                    foxlogik_mcp_proxy module, and a system-administrator
                    connection user.

            Returns:
                Confirmation carrying the created message's ID.
            """
            result = await self._handle_post_message_tool(
                model,
                self._resolve_record_id(record_id, res_id),
                body,
                subtype=subtype,
                message_type=message_type,
                subject=subject,
                partner_ids=partner_ids,
                attachment_ids=attachment_ids,
                author_id=author_id,
                ctx=ctx,
                user_id=self._effective_user_id(user_id),
            )
            return PostMessageResult(**result)

        @self.app.tool(
            title="Call Method",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
        async def call_method(
            model: str,
            method: str,
            args: Optional[Any] = None,
            kwargs: Optional[Any] = None,
            user_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> CallMethodResult:
            """Call an allowlisted model method on Odoo.

            For bespoke model methods that are not plain CRUD. Only methods on the
            server-side allowlist can be invoked; anything else is rejected.

            Currently allowed:
                - bpm.process.get_builder_reference(model_name)
                - bpm.process.validate_bpmn_xml(xml_str, model_name=None)
                - bpm.process.create_draft_process_from_spec(spec)

            Args:
                model: The Odoo model name (e.g. 'bpm.process').
                method: The method to call. Must be allowlisted for this model.
                args: Positional arguments — a list, or a JSON string encoding a
                    list (e.g. '["<xml>", "project.task"]'). Defaults to [].
                kwargs: Keyword arguments — a dict, or a JSON string encoding a
                    dict. Defaults to {}.
                user_id: Optional Odoo user ID. When provided the call runs under
                    that user's security context. Requires the
                    foxlogik_mcp_proxy module.

            Returns:
                The raw return value of the method, wrapped with success/message.
            """
            result = await self._handle_call_method_tool(
                model, method, args, kwargs, ctx,
                user_id=self._effective_user_id(user_id),
            )
            return CallMethodResult(**result)

        @self.app.tool(
            title="Get Fields",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def get_fields(
            model: str,
            attributes: Optional[List[str]] = None,
            user_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> FieldsResult:
            """Get field definitions for a model (types, labels, required, relations).

            The per-user metadata channel: in a pinned session (or with user_id)
            the definitions are computed for that user, so fields protected by
            groups= the user lacks are absent — what you see is exactly what a
            subsequent read/create as that user may touch. Use this to discover
            which fields exist, which are required, and what selection values
            are allowed before proposing a create or update.

            Args:
                model: The Odoo model name (e.g. 'account.analytic.line')
                attributes: Field attributes to return. Defaults to a compact
                    set (string, type, required, readonly, relation, selection,
                    help, store). Pass explicitly for others.
                user_id: Optional Odoo user ID to compute the definitions for.
                    Ignored (clamped) when the session is pinned.

            Returns:
                Field definitions keyed by field name.
            """
            result = await self._handle_get_fields_tool(
                model, attributes, ctx, user_id=self._effective_user_id(user_id)
            )
            return FieldsResult(**result)

        @self.app.tool(
            title="Get Defaults",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def get_defaults(
            model: str,
            fields: List[str],
            user_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> DefaultsResult:
            """Get the default values a new record of this model would receive.

            Runs default_get for the requested fields. In a pinned session (or
            with user_id) defaults are computed as that user — e.g. a
            timesheet's employee/date defaults come out prefilled for them.

            Args:
                model: The Odoo model name (e.g. 'account.analytic.line')
                fields: Field names to fetch defaults for
                user_id: Optional Odoo user ID to compute defaults for.
                    Ignored (clamped) when the session is pinned.

            Returns:
                Default values keyed by field name (fields with no default
                are absent).
            """
            result = await self._handle_get_defaults_tool(
                model, fields, ctx, user_id=self._effective_user_id(user_id)
            )
            return DefaultsResult(**result)

        @self.app.tool(
            title="Check Access",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def check_access(
            model: str,
            operation: str,
            user_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> AccessCheckResult:
            """Check whether an operation on a model is permitted.

            Runs check_access_rights(raise_exception=False). In a pinned
            session (or with user_id) the check is for that user — use it to
            confirm e.g. 'can this user create a timesheet?' BEFORE building a
            proposal, instead of failing midway.

            Args:
                model: The Odoo model name (e.g. 'account.analytic.line')
                operation: One of 'read', 'write', 'create', 'unlink'
                user_id: Optional Odoo user ID to check for. Ignored (clamped)
                    when the session is pinned.

            Returns:
                Whether the operation is allowed for the effective user.
            """
            result = await self._handle_check_access_tool(
                model, operation, ctx, user_id=self._effective_user_id(user_id)
            )
            return AccessCheckResult(**result)

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
            groupby: Optional[Any] = None,
            aggregates: Optional[Any] = None,
            domain: Optional[Any] = None,
            order: Optional[str] = None,
            limit: Optional[int] = None,
            offset: int = 0,
            user_id: Optional[int] = None,
            ctx: Optional[Context] = None,
        ) -> AggregateResult:
            """Aggregate records server-side via Odoo's grouping methods.

            Use this tool whenever the question is "totals/counts/groupings",
            not "list of records". It pushes the aggregation down to Odoo
            instead of pulling raw records and reducing client-side.

            Dispatches by Odoo version: ``formatted_read_group`` on 19+,
            ``read_group`` on older versions with response-shape
            normalization. Callers see the same response shape on every
            supported version.

            Args:
                model: Odoo model name (e.g. 'sale.order')
                groupby: Group expressions (list or JSON string). Field names,
                    optionally with a granularity suffix for date/datetime
                    fields (day, week, month, quarter, year):
                    ``["date_order:month"]``, ``["partner_id"]``,
                    ``["partner_id", "date_order:year"]``. Omit (or pass
                    ``[]``) for a single overall-aggregate row — e.g. a
                    filtered count via the default ``__count`` aggregate.
                aggregates: Aggregate expressions of the form ``"field:operator"``
                    (sum, avg, min, max, count, count_distinct, array_agg, ...).
                    Examples: ``["amount_total:sum"]``, ``["__count"]``.
                    Before Odoo 19, only one aggregate per field (not both
                    ``amount_total:sum`` and ``amount_total:avg``), and not
                    over a field that is also a groupby key.
                    If omitted or empty, defaults to ``["__count"]``. Pass
                    ``["__count", "amount_total:sum"]`` to get both.
                domain: Odoo domain filter (list or JSON string), or None for
                    every record.
                order: Sort expression over groupby keys / aggregates,
                    e.g. ``"date_order:month"`` or ``"amount_total:sum desc"``.
                limit: Maximum number of groups. Omitted or 0, it is
                    ``ODOO_MCP_DEFAULT_LIMIT``; capped at ``ODOO_MCP_MAX_LIMIT``.
                offset: Number of groups to skip.
                user_id: Optional Odoo user ID. When provided the aggregation
                    runs under that user's security context (access rights and
                    record rules apply, so totals only cover records that user
                    can read). Ignored (clamped) when the session is pinned.

            Drilldown: AND a group's ``__extra_domain`` with the ``domain``
                you passed — ``search_records(model, domain=[*your_domain,
                *group["__extra_domain"]])``.

            Returns:
                ``groups`` (each with the groupby keys, ``__count``, requested
                aggregates and ``__extra_domain``), the echoed ``model``,
                ``groupby`` and ``aggregates``, plus ``has_more`` and
                ``next_hint`` for paging.

            Examples:
                aggregate_records("sale.order", groupby=["date_order:month"],
                    aggregates=["amount_total:sum"],
                    domain=[["state", "in", ["sale", "done"]]])
                aggregate_records("res.partner", groupby=["country_id"])
                aggregate_records("res.partner", domain=[["is_company", "=", True]])
            """
            result = await self._handle_aggregate_records_tool(
                model,
                groupby,
                aggregates,
                domain,
                order,
                limit,
                offset,
                ctx,
                user_id=self._effective_user_id(user_id),
            )
            return AggregateResult(**result)

    @staticmethod
    def _parse_domain_input(domain: Optional[Any]) -> List[Any]:
        """Coerce a domain argument (list, JSON string or Python literal) to a list."""
        parsed_domain = []
        if domain is not None:
            if isinstance(domain, str):
                # Parse string to list
                try:
                    # First try standard JSON parsing
                    parsed_domain = json.loads(domain)
                except json.JSONDecodeError:
                    # If that fails, try converting single quotes to double quotes
                    # This handles Python-style domain strings
                    try:
                        # Replace single quotes with double quotes for valid JSON
                        # But be careful not to replace quotes inside string values
                        json_domain = domain.replace("'", '"')
                        # Also need to ensure Python True/False are lowercase for JSON
                        json_domain = json_domain.replace("True", "true").replace("False", "false")
                        parsed_domain = json.loads(json_domain)
                    except json.JSONDecodeError as e:
                        # If both attempts fail, try evaluating as Python literal
                        try:
                            import ast

                            parsed_domain = ast.literal_eval(domain)
                        except (ValueError, SyntaxError):
                            raise ValidationError(
                                f"Invalid domain parameter. Expected JSON array or Python list, got: {domain[:100]}..."
                            ) from e

                if not isinstance(parsed_domain, list):
                    raise ValidationError(
                        f"Domain must be a list, got {type(parsed_domain).__name__}"
                    )
                logger.debug(f"Parsed domain from string: {parsed_domain}")
            else:
                # Already a list
                parsed_domain = domain
        return parsed_domain

    async def _handle_search_tool(
        self,
        model: str,
        domain: Optional[Any],
        fields: Optional[Any],
        limit: int,
        offset: int,
        order: Optional[str],
        ctx=None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle search tool request."""
        try:
            with perf_logger.track_operation("tool_search", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Searching {model}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Handle domain parameter - can be string or list
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

                # Validate locally, before the RPC. Odoo answers a malformed leaf
                # with an unpack ValueError and an unknown field with a stack
                # trace; neither names the fix, so the caller repeats the mistake.
                parsed_domain = self._guard_domain(model, parsed_domain)
                self._guard_fields(
                    model,
                    parsed_fields if isinstance(parsed_fields, list) else None,
                    user_id=user_id,
                    extra=domain_field_names(parsed_domain),
                )

                # Set defaults
                if limit <= 0 or limit > self.config.max_limit:
                    limit = self.config.default_limit

                # Get total count
                if user_id is not None:
                    total_count = self.connection.execute_kw_as_user(
                        user_id, model, "search_count", [parsed_domain], {}
                    )
                else:
                    total_count = self.connection.search_count(model, parsed_domain)
                await self._ctx_progress(ctx, 1, 3, f"Found {total_count} records")

                # Search for records
                search_kwargs: Dict[str, Any] = {}
                if limit > 0:
                    search_kwargs["limit"] = limit
                if offset:
                    search_kwargs["offset"] = offset
                if order:
                    search_kwargs["order"] = order
                if user_id is not None:
                    record_ids = self.connection.execute_kw_as_user(
                        user_id, model, "search", [parsed_domain], search_kwargs
                    )
                else:
                    record_ids = self.connection.search(
                        model, parsed_domain, limit=limit, offset=offset, order=order
                    )

                # Determine which fields to fetch
                fields_to_fetch = parsed_fields
                if parsed_fields is None:
                    # Use smart field selection to avoid serialization issues
                    fields_to_fetch = self._get_smart_default_fields(model, user_id=user_id)
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
                    if user_id is not None:
                        records = self.connection.execute_kw_as_user(
                            user_id, model, "read", [record_ids, fields_to_fetch], {}
                        )
                    else:
                        records = self.connection.read(model, record_ids, fields_to_fetch)
                    # Process datetime fields in each record
                    records = [self._process_record_dates(record, model) for record in records]
                await self._ctx_progress(ctx, 3, 3, f"Returning {len(records)} records")

                return {
                    "records": records,
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "model": model,
                }

        except AccessControlError as e:
            raise ValidationError(self._access_hint(model, fields, f"Access denied: {e}")) from e
        except OdooConnectionError as e:
            raise ValidationError(self._access_hint(model, fields, f"Connection error: {e}")) from e
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
        user_id: Optional[int] = None,
    ) -> RecordResult:
        """Handle get record tool request."""
        try:
            with perf_logger.track_operation("tool_get_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Getting {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Determine which fields to fetch
                fields_to_fetch = fields
                use_smart_defaults = False
                total_fields = None
                field_selection_method = "explicit"

                if fields is None:
                    # Use smart field selection
                    fields_to_fetch = self._get_smart_default_fields(model, user_id=user_id)
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
                    self._guard_fields(model, fields, user_id=user_id)
                    logger.debug(f"Fetching specific fields for {model}: {fields}")

                # Read the record
                if user_id is not None:
                    records = self.connection.execute_kw_as_user(
                        user_id, model, "read", [[record_id], fields_to_fetch], {}
                    )
                else:
                    records = self.connection.read(model, [record_id], fields_to_fetch)

                if not records:
                    raise ValidationError(f"Record not found: {model} with ID {record_id}")

                # Process datetime fields in the record
                record = self._process_record_dates(records[0], model)

                # Build metadata when using smart defaults
                metadata = None
                if use_smart_defaults:
                    try:
                        all_fields_info = self._fields_get_for(model, user_id)
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
        except AccessControlError as e:
            raise ValidationError(self._access_hint(model, fields, f"Access denied: {e}")) from e
        except OdooConnectionError as e:
            raise ValidationError(self._access_hint(model, fields, f"Connection error: {e}")) from e
        except Exception as e:
            logger.error(f"Error in get_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get record: {sanitized_msg}") from e

    async def _handle_get_fields_tool(
        self,
        model: str,
        attributes: Optional[List[str]],
        ctx=None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle get_fields tool request."""
        try:
            with perf_logger.track_operation("tool_get_fields", model=model):
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Getting field definitions for {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                attrs = attributes or DEFAULT_FIELD_ATTRIBUTES
                fields_info = self._fields_get_for(model, user_id, attributes=attrs)
                return {
                    "model": model,
                    "fields": fields_info,
                    "total": len(fields_info),
                    "user_scoped": user_id is not None,
                }
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_fields tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get fields: {sanitized_msg}") from e

    async def _handle_get_defaults_tool(
        self,
        model: str,
        fields: List[str],
        ctx=None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle get_defaults tool request."""
        try:
            with perf_logger.track_operation("tool_get_defaults", model=model):
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Getting defaults for {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")
                if not fields or not isinstance(fields, list):
                    raise ValidationError("fields must be a non-empty list of field names")

                if user_id is not None:
                    defaults = self.connection.execute_kw_as_user(
                        user_id, model, "default_get", [fields], {}
                    )
                else:
                    defaults = self.connection.execute_kw(model, "default_get", [fields], {})
                return {
                    "model": model,
                    "defaults": defaults or {},
                    "user_scoped": user_id is not None,
                }
        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_defaults tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get defaults: {sanitized_msg}") from e

    async def _handle_check_access_tool(
        self,
        model: str,
        operation: str,
        ctx=None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle check_access tool request."""
        try:
            with perf_logger.track_operation("tool_check_access", model=model):
                valid_ops = {"read", "write", "create", "unlink"}
                if operation not in valid_ops:
                    raise ValidationError(
                        f"Invalid operation {operation!r}. Must be one of: "
                        f"{', '.join(sorted(valid_ops))}"
                    )
                # Model must at least be MCP-visible; the actual permission
                # answer comes from Odoo below.
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Checking {operation} access on {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                kwargs = {"raise_exception": False}
                if user_id is not None:
                    allowed = self.connection.execute_kw_as_user(
                        user_id, model, "check_access_rights", [operation], kwargs
                    )
                else:
                    allowed = self.connection.execute_kw(
                        model, "check_access_rights", [operation], kwargs
                    )
                return {
                    "model": model,
                    "operation": operation,
                    "allowed": bool(allowed),
                    "user_scoped": user_id is not None,
                }
        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in check_access tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to check access: {sanitized_msg}") from e

    async def _handle_list_models_tool(self, ctx=None) -> Dict[str, Any]:
        """Handle list models tool request with permissions."""
        try:
            with perf_logger.track_operation("tool_list_models"):
                await self._ctx_info(ctx, "Listing available models...")
                # Check if YOLO mode is enabled
                if self.config.is_yolo_enabled:
                    # Query actual models from ir.model in YOLO mode
                    try:
                        # Exclude transient models and less useful system models
                        domain = [
                            "&",
                            ("transient", "=", False),
                            "|",
                            "|",
                            ("model", "not like", "ir.%"),
                            ("model", "not like", "base.%"),
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
                        ]

                        # Query models from database. In a pinned session, run
                        # as the pinned user so model-name disclosure respects
                        # their access rights instead of the admin account's.
                        if self.config.is_user_pinned:
                            model_records = self.connection.execute_kw_as_user(
                                self.config.act_as_uid,
                                "ir.model",
                                "search_read",
                                [domain],
                                {
                                    "fields": ["model", "name"],
                                    "order": "name ASC",
                                    "limit": 200,
                                },
                            )
                        else:
                            model_records = self.connection.search_read(
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
                models = self.access_controller.get_enabled_models()

                # Enrich with permissions for each model
                enriched_models = []
                for i, model_info in enumerate(models):
                    await self._ctx_progress(ctx, i + 1, len(models))
                    model_name = model_info["model"]
                    try:
                        # Get permissions for this model
                        permissions = self.access_controller.get_model_permissions(model_name)
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
            # Get list of enabled models that can be used with resources
            enabled_models = self.access_controller.get_enabled_models()
            model_names = [m["model"] for m in enabled_models if m.get("read", True)]

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
            return {
                "templates": templates,
                "enabled_models": model_names[:10],  # Show first 10 as examples
                "total_models": len(model_names),
                "note": "Resource URIs do not support query parameters. Use tools (search_records, get_record) for advanced operations with filtering, pagination, and field selection.",
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
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle create record tool request."""
        try:
            with perf_logger.track_operation("tool_create_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "create")
                await self._ctx_info(ctx, f"Creating record in {model}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate required fields
                if not values:
                    raise ValidationError("No values provided for record creation")

                # Reject unknown field names before the write, naming the real ones.
                self._guard_fields(model, list(values.keys()), user_id=user_id, context="value fields")

                # Create the record
                if user_id is not None:
                    record_id = self.connection.execute_kw_as_user(
                        user_id, model, "create", [values], {}
                    )
                else:
                    record_id = self.connection.create(model, values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                # Only use universally available fields (not all models have 'name')
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                if user_id is not None:
                    records = self.connection.execute_kw_as_user(
                        user_id, model, "read", [[record_id], essential_fields], {}
                    )
                else:
                    records = self.connection.read(model, [record_id], essential_fields)
                if not records:
                    raise ValidationError(
                        f"Failed to read created record: {model} with ID {record_id}"
                    )

                # Process dates in the minimal record
                record = self._process_record_dates(records[0], model)

                record_url = self.connection.build_record_url(model, record_id)

                return {
                    "success": True,
                    "record": record,
                    "url": record_url,
                    "message": f"Successfully created {model} record with ID {record_id}",
                }

        except ValidationError:
            raise
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
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle update record tool request."""
        try:
            with perf_logger.track_operation("tool_update_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "write")
                await self._ctx_info(ctx, f"Updating {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not values:
                    raise ValidationError("No values provided for record update")

                # Reject unknown field names before the write, naming the real ones.
                self._guard_fields(model, list(values.keys()), user_id=user_id, context="value fields")

                # Check if record exists (only fetch ID to verify existence)
                if user_id is not None:
                    existing = self.connection.execute_kw_as_user(
                        user_id, model, "read", [[record_id], ["id"]], {}
                    )
                else:
                    existing = self.connection.read(model, [record_id], ["id"])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Update the record
                if user_id is not None:
                    success = self.connection.execute_kw_as_user(
                        user_id, model, "write", [[record_id], values], {}
                    )
                else:
                    success = self.connection.write(model, [record_id], values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                # Only use universally available fields (not all models have 'name')
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                if user_id is not None:
                    records = self.connection.execute_kw_as_user(
                        user_id, model, "read", [[record_id], essential_fields], {}
                    )
                else:
                    records = self.connection.read(model, [record_id], essential_fields)
                if not records:
                    raise ValidationError(
                        f"Failed to read updated record: {model} with ID {record_id}"
                    )

                # Process dates in the minimal record
                record = self._process_record_dates(records[0], model)

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
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle delete record tool request."""
        try:
            with perf_logger.track_operation("tool_delete_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "unlink")
                await self._ctx_info(ctx, f"Deleting {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Check if record exists and get display info
                if user_id is not None:
                    existing = self.connection.execute_kw_as_user(
                        user_id, model, "read", [[record_id], ["id", "display_name"]], {}
                    )
                else:
                    existing = self.connection.read(model, [record_id], ["id", "display_name"])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Store some info about the record before deletion
                record_name = existing[0].get("display_name", f"ID {record_id}")

                # Delete the record
                if user_id is not None:
                    success = self.connection.execute_kw_as_user(
                        user_id, model, "unlink", [[record_id]], {}
                    )
                else:
                    success = self.connection.unlink(model, [record_id])

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
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in delete_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to delete record: {sanitized_msg}") from e

    @staticmethod
    def _coerce_json(value: Any, default: Any, name: str, expect: type) -> Any:
        """Normalize an args/kwargs value that may arrive as a JSON string.

        Accepts a native list/dict, a JSON string encoding one, or None (→ default).
        Raises ValidationError if the value is the wrong shape.
        """
        if value is None:
            return default
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValidationError(f"'{name}' is not valid JSON: {e}") from e
        if not isinstance(value, expect):
            raise ValidationError(
                f"'{name}' must be a {expect.__name__} (got {type(value).__name__})"
            )
        return value

    async def _handle_post_message_tool(
        self,
        model: str,
        record_id: int,
        body: str,
        subtype: str = "comment",
        message_type: str = "comment",
        subject: Optional[str] = None,
        partner_ids: Optional[List[int]] = None,
        attachment_ids: Optional[List[int]] = None,
        author_id: Optional[int] = None,
        ctx=None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle post_message tool request.

        Routed through res.users.mcp_post_message rather than calling
        message_post directly: message_post returns a mail.message recordset,
        which XML-RPC cannot marshal, so a direct call commits the message and
        then fails the response — an error the caller is invited to retry,
        duplicating the post. The proxy method returns the message's ID.
        """
        try:
            with perf_logger.track_operation("tool_post_message", model=model):
                # Posting writes to the thread, so it needs write access to it.
                self.access_controller.validate_model_access(model, "write")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                if not body or not body.strip():
                    raise ValidationError("Cannot post an empty message body.")

                values: Dict[str, Any] = {
                    "body": body,
                    "message_type": message_type,
                    "subtype_xmlid": subtype,
                }
                if subject:
                    values["subject"] = subject
                if partner_ids:
                    values["partner_ids"] = list(partner_ids)
                if attachment_ids:
                    values["attachment_ids"] = list(attachment_ids)
                if author_id:
                    values["author_id"] = author_id

                await self._ctx_info(ctx, f"Posting to {model} {record_id}...")

                message_id = self.connection.execute_kw(
                    "res.users",
                    "mcp_post_message",
                    [model, record_id, values],
                    {"user_id": user_id} if user_id is not None else {},
                )

                return {
                    "success": True,
                    "model": model,
                    "record_id": record_id,
                    "message_id": message_id,
                    "url": self.connection.build_record_url(model, record_id),
                    "message": f"Posted message {message_id} to {model} {record_id}",
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            # The helper ships with foxlogik_mcp_proxy. Without it Odoo answers
            # with a bare attribute error, which says nothing about the fix.
            if "mcp_post_message" in str(e):
                raise ValidationError(
                    "post_message needs the foxlogik_mcp_proxy module (>= 17.0.1.3.0) "
                    "installed on this database. Install or upgrade it, or post by "
                    "creating a mail.message with create_record."
                ) from e
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in post_message tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to post message: {sanitized_msg}") from e

    async def _handle_call_method_tool(
        self,
        model: str,
        method: str,
        args: Any = None,
        kwargs: Any = None,
        ctx=None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle call_method tool request (allowlisted model methods only)."""
        try:
            with perf_logger.track_operation("tool_call_method", model=model):
                # Allowlist gate — reject any non-vetted (model, method) before Odoo.
                allowed = METHOD_CALL_ALLOWLIST.get(model, set())
                if method not in allowed:
                    raise ValidationError(
                        f"Method '{method}' is not callable on '{model}' via call_method. "
                        f"Allowed methods for this model: {sorted(allowed) or 'none'}."
                    )

                # Access control — the model must be MCP-enabled for the operation
                # this method implies (read for inspectors, create for the builder).
                required_op = METHOD_REQUIRED_OPERATION.get((model, method), "read")
                self.access_controller.validate_model_access(model, required_op)

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                call_args = self._coerce_json(args, default=[], name="args", expect=list)
                call_kwargs = self._coerce_json(kwargs, default={}, name="kwargs", expect=dict)

                await self._ctx_info(ctx, f"Calling {model}.{method}()...")

                if user_id is not None:
                    result = self.connection.execute_kw_as_user(
                        user_id, model, method, call_args, call_kwargs
                    )
                else:
                    result = self.connection.execute_kw(model, method, call_args, call_kwargs)

                return {
                    "success": True,
                    "model": model,
                    "method": method,
                    "result": result,
                    "message": f"Successfully called {model}.{method}()",
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in call_method tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to call method: {sanitized_msg}") from e

    # ------------------------------------------------------------------ #
    # aggregate_records                                                    #
    # ------------------------------------------------------------------ #

    def _execute_for(
        self,
        user_id: Optional[int],
        model: str,
        method: str,
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> Any:
        """Run a model method as ``user_id`` (pinned path) or as the service account."""
        if user_id is not None:
            return self.connection.execute_kw_as_user(user_id, model, method, args, kwargs)
        return self.connection.execute_kw(model, method, args, kwargs)

    @staticmethod
    def _parse_string_list(value: Optional[Any], name: str) -> List[str]:
        """Coerce a list-of-strings argument (list, JSON string or single name)."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError:
                    try:
                        import ast

                        value = ast.literal_eval(stripped)
                    except (ValueError, SyntaxError) as e:
                        raise ValidationError(
                            f"Invalid {name} parameter. Expected a JSON array of strings, "
                            f"got: {value[:100]}"
                        ) from e
            else:
                value = [stripped]
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            raise ValidationError(f"{name} must be a list of strings")
        return [v for v in value if v]

    def _check_aggregate_types(
        self, model: str, aggregates: List[str], user_id: Optional[int] = None
    ) -> None:
        """Refuse an aggregate whose function does not fit the field type.

        Unknown fields and functions are left to Odoo (or the field guard).
        """
        typed = [a for a in aggregates if a.partition(":")[2] in _TYPED_AGGREGATES]
        if not typed:
            return
        fields_info = self._schema_for(model, user_id=user_id) or {}
        for spec in typed:
            name, _, function = spec.partition(":")
            field_type = (fields_info.get(name) or {}).get("type")
            allowed = _TYPED_AGGREGATES[function]
            if field_type is not None and field_type not in allowed:
                raise ValidationError(
                    f"Aggregate '{spec}' needs a field of type {', '.join(allowed)}. "
                    f"'{name}' is a {field_type} field."
                )

    def _call_read_group_normalized(
        self,
        model: str,
        domain: List[Any],
        groupby: List[str],
        aggregates: List[str],
        order: Optional[str],
        limit: int,
        offset: int,
        user_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Call legacy ``read_group`` (Odoo < 19) and normalize to the v19 shape.

        * ``__domain`` → ``__extra_domain``. Legacy ``read_group`` gives the
          FULL domain (caller's AND group condition) where v19 gives only the
          group condition; re-ANDing the caller's domain is idempotent, so the
          documented drilldown contract holds on both.
        * Aggregate keys: read_group keys values by the bare field name;
          rename back to ``"field:op"``.
        * Keep only requested groupby keys, aggregates and metadata keys.

        Runs as ``user_id`` through the proxy when set (``read_group`` is on
        foxlogik_mcp_proxy's allow-list), so Odoo's access check and record
        rules apply to that user.
        """
        # __count is implicit in read_group (lazy=False). Alone it is sent as
        # the only field: fields=[] aggregates every numeric field (and breaks
        # Odoo 16 without a groupby); fields=["__count"] works on 16-18.
        fields_kwarg = [a for a in aggregates if a != "__count"]

        # read_group keys aggregates by BARE field name, so an aggregate over a
        # groupby field collides with the group key and corrupts every bucket.
        groupby_field_names = {g.split(":", 1)[0] for g in groupby}
        collisions = sorted(
            {a for a in fields_kwarg if ":" in a and a.split(":", 1)[0] in groupby_field_names}
        )
        if collisions:
            raise ValidationError(
                f"Cannot aggregate {', '.join(collisions)} over a field that is also a "
                "groupby key on Odoo < 19: read_group returns both under the same key. "
                "Drop the aggregate (the groupby key already identifies each group) or "
                "aggregate a different field."
            )

        # Same root cause between two aggregates on one field.
        seen_fields: Dict[str, str] = {}
        for spec in fields_kwarg:
            bare = spec.split(":", 1)[0]
            if bare in seen_fields:
                raise ValidationError(
                    f"Cannot request both '{seen_fields[bare]}' and '{spec}' on Odoo < 19: "
                    f"read_group returns both under the bare key '{bare}', so one would be "
                    "dropped and the other mislabeled. Request one aggregate per field, or "
                    "make a second call."
                )
            seen_fields[bare] = spec

        kwargs: Dict[str, Any] = {
            "fields": fields_kwarg or ["__count"],
            "groupby": groupby,
            "limit": limit,
            "offset": offset,
            "lazy": False,
        }
        if order is not None:
            major = self.connection.get_major_version()
            if isinstance(major, int) and major < 17:
                order = _odoo16_group_order(order, fields_kwarg)
            kwargs["orderby"] = order

        groups = self._execute_for(user_id, model, "read_group", [domain], kwargs)
        if groups is None:
            # Odoo 16 cannot marshal the None in the total row of an empty
            # ungrouped read_group; Odoo 17 answers the row below.
            if groupby:
                return []
            return [{"__count": 0, "__extra_domain": [], **dict.fromkeys(fields_kwarg, False)}]

        agg_renames = [(a.split(":", 1)[0], a) for a in fields_kwarg if ":" in a]
        allowed_keys = _READ_GROUP_META_KEYS | set(groupby) | set(fields_kwarg)

        normalized: List[Dict[str, Any]] = []
        for bucket in groups:
            if "__domain" in bucket:
                bucket["__extra_domain"] = bucket.pop("__domain")
            elif not groupby:
                bucket["__extra_domain"] = []
            for bare, full in agg_renames:
                if bare in bucket and full != bare:
                    bucket[full] = bucket.pop(bare)
            normalized.append({k: v for k, v in bucket.items() if k in allowed_keys})
        return normalized

    @staticmethod
    def _aggregate_error_message(model: str, user_id: Optional[int], message: str) -> str:
        """Turn an Odoo refusal on the aggregate path into an actionable message."""
        if "not permitted via MCP impersonation" in message:
            return (
                f"Cannot aggregate {model} as user {user_id}: the Odoo proxy module "
                "(foxlogik_mcp_proxy) does not allow this grouping method for "
                "impersonated calls. Upgrade foxlogik_mcp_proxy on the server."
            )
        lowered = message.lower()
        if "not allowed to access" in lowered or "accesserror" in lowered:
            who = f"user {user_id}" if user_id is not None else "the service account"
            return (
                f"Access denied: {who} cannot read {model}, so it cannot be aggregated. "
                f"{message} Retrying with the same arguments will fail identically — "
                "request the missing group, or aggregate a model you have access to."
            )
        return message

    async def _handle_aggregate_records_tool(
        self,
        model: str,
        groupby: Optional[Any],
        aggregates: Optional[Any],
        domain: Optional[Any],
        order: Optional[str],
        limit: Optional[int],
        offset: int,
        ctx=None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle aggregate_records tool request."""
        try:
            with perf_logger.track_operation("tool_aggregate_records", model=model):
                # Same gate as search_records: model must be MCP-readable.
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Aggregating {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                groupby_list = self._parse_string_list(groupby, "groupby")
                aggregate_list = self._parse_string_list(aggregates, "aggregates")
                # Default to ['__count'] so each group carries a count.
                effective_aggregates = aggregate_list or ["__count"]

                parsed_domain = self._guard_domain(model, self._parse_domain_input(domain))
                self._guard_fields(
                    model,
                    [g.split(":", 1)[0] for g in groupby_list],
                    user_id=user_id,
                    context="groupby fields",
                    extra=[a.split(":", 1)[0] for a in effective_aggregates if a != "__count"]
                    + domain_field_names(parsed_domain),
                )
                self._check_aggregate_types(model, effective_aggregates, user_id=user_id)

                if limit is not None and limit < 0:
                    raise ValidationError(f"limit must be 0 or more, got {limit}")
                if not limit:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit
                if offset is None or offset < 0:
                    raise ValidationError(f"offset must be 0 or more, got {offset}")

                # Peek one group past the page to detect truncation.
                peek_limit = limit + 1

                # Version dispatch: formatted_read_group is Odoo 19+ only.
                # Unknown version (None) → assume newer and let a fault surface.
                major = self.connection.get_major_version()
                if major is not None and major < 19:
                    if major < 17:
                        id_aggregates = sorted(
                            {
                                a
                                for a in effective_aggregates
                                if a != "__count" and a.split(":", 1)[0] == "id"
                            }
                        )
                        if id_aggregates:
                            raise ValidationError(
                                f"Cannot aggregate {', '.join(id_aggregates)} on Odoo "
                                f"{major}: read_group drops the 'id' key before returning. "
                                "Use '__count' for a row count."
                            )
                    groups = self._call_read_group_normalized(
                        model,
                        parsed_domain,
                        groupby_list,
                        effective_aggregates,
                        order,
                        peek_limit,
                        offset,
                        user_id=user_id,
                    )
                else:
                    kwargs: Dict[str, Any] = {
                        "groupby": groupby_list,
                        "aggregates": effective_aggregates,
                        "limit": peek_limit,
                        "offset": offset,
                    }
                    if order is not None:
                        kwargs["order"] = order
                    groups = self._execute_for(
                        user_id, model, "formatted_read_group", [parsed_domain], kwargs
                    )
                groups = list(groups or [])

                has_more = len(groups) > limit
                if has_more:
                    groups = groups[:limit]
                next_hint = (
                    f"aggregate_records with offset={offset + limit}, limit={limit}"
                    if has_more
                    else None
                )
                await self._ctx_info(ctx, f"Returning {len(groups)} groups")

                return {
                    "groups": groups,
                    "model": model,
                    "groupby": groupby_list,
                    "aggregates": effective_aggregates,
                    "has_more": has_more,
                    "next_hint": next_hint,
                    "user_scoped": user_id is not None,
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(
                self._aggregate_error_message(model, user_id, f"Connection error: {e}")
            ) from e
        except Exception as e:
            logger.error(f"Error in aggregate_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(
                self._aggregate_error_message(
                    model, user_id, f"Aggregation failed: {sanitized_msg}"
                )
            ) from e


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
