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
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Union

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .access_control import (
    AccessControlError,
    AccessController,
    AccessControlUnavailableError,
    access_denied_message,
    attachment_scope_domain,
    check_domain_balance,
)
from .config import OdooConfig, max_offset_for
from .error_handling import (
    MCPPermissionError,
    NotFoundError,
    ValidationError,
)
from .error_sanitizer import ErrorSanitizer
from .field_security import is_sensitive_field_name, strip_sensitive_fields, withheld_note
from .formatters import MAX_RELATED_ITEMS
from .logging_config import get_logger, perf_logger
from .odoo_connection import (
    XMLRPC_MAX_INT,
    OdooConnection,
    OdooConnectionError,
    OdooValidationFault,
)
from .schemas import (
    AggregateResult,
    CallModelMethodResult,
    CompanyInfo,
    CreateResult,
    CurrentContextResult,
    DeleteResult,
    FieldInfo,
    FieldSelectionMetadata,
    FieldsResult,
    ModelsResult,
    PostMessageResult,
    RecordResult,
    RelatedSummary,
    ResourceTemplatesResult,
    SearchResult,
    UpdateResult,
)
from .uri_schema import BINARY_FIELD_TYPES, URIValidationError, build_binary_uri
from .user_context import (
    context_unavailable_text,
    format_user_context,
    get_user_context_data,
)

logger = get_logger(__name__)

# Public Odoo method = Python identifier not starting with "_".
_PUBLIC_METHOD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# Models whose public methods are self-elevating and are refused by
# call_model_method regardless of the opt-in flags: ir.actions.server.run()
# executes server-action code as superuser and ir.cron.method_direct_trigger
# runs a cron job as its (often privileged) owner — either would escalate past
# the authenticated user's ACLs, which XML-RPC otherwise enforces. Scoped to
# these prefixes on purpose — other ir.* models (ir.attachment, ...) stay
# callable; no blanket ir.% block.
_BLOCKED_METHOD_CALL_MODELS = ("ir.actions", "ir.cron")

# ORM CRUD / data-access primitives call_model_method refuses even under full
# YOLO — the business-method hatch must not silently become generic CRUD;
# those operations go through the dedicated tools. In-process introspection
# (mapped-operation gating, a hasattr(BaseModel, ...) check) cannot be
# replicated over XML-RPC, so this denylist is the remote approximation.
_BLOCKED_METHOD_CALLS = frozenset(
    {
        "create",
        "write",
        "unlink",
        "read",
        "search",
        "search_read",
        "search_count",
        "search_fetch",
        "fetch",
        "read_group",
        "formatted_read_group",
        "formatted_read_grouping_sets",
        "read_progress_bar",
        "name_search",
        "search_panel_select_range",
        "search_panel_select_multi_range",
        "copy",
        "browse",
        "_write",
        "sudo",
        "with_user",
        "with_env",
        "with_context",
        "fields_get",
        "default_get",
        "exists",
        "load",
        "export_data",
        "name_create",
        # Aliases of the primitives above that Odoo still accepts over
        # execute_kw: copy_data returns every copy=True field (a read by
        # another name), update is write (15-18; @api.private only on 19),
        # copy_multi is copy (17), and get_view/get_views expose the same
        # metadata as fields_get (16-19).
        "copy_data",
        "copy_multi",
        "update",
        "get_view",
        "get_views",
    }
)

# Self-escalating method names, banned on EVERY model on purpose
# (defense-in-depth): the model-level block in _BLOCKED_METHOD_CALL_MODELS
# covers ir.actions.*/ir.cron themselves, but other models commonly proxy
# or delegate to them (e.g. a run() that forwards to an ir.actions.server
# record), and those would slip past a model-name check. Refusing a
# legitimately named run() on an unrelated model is an accepted cost for a
# privilege-escalation backstop. Kept separate from _BLOCKED_METHOD_CALLS
# so the rejection message states the actual reason instead of calling
# these ORM data-access primitives.
_BLOCKED_PRIVILEGED_METHOD_NAMES = frozenset({"run", "method_direct_trigger"})

# List results from call_model_method are truncated to this many items
# (matches the search max limit) so a method returning a huge list cannot
# blow up the response.
MAX_METHOD_RESULT_ITEMS = 100

# Per-record ceiling on how many x2many fields get their display names
# resolved. Each qualifying field costs an access check plus a read RPC, and
# a rich record has many of them (res.users carries 15 on stock Odoo 19), so
# an uncapped sweep turns one get_record into dozens of serialized round
# trips. Fields are resolved in record order and the rest keep their ids.
MAX_RELATED_SUMMARY_FIELDS = 8

# Context-flood guard for YOLO list_models on Studio-heavy databases: the
# listing is capped here, with an explicit truncation note carrying the real
# total (from search_count) so the cap is never silent.
MAX_LISTED_MODELS = 500

# Refuse JSON strings larger than this on the parse path — bounds memory and
# guards against pathological inputs.
_MAX_JSON_PARAM_BYTES = 1_000_000

# Deeply nested parameter strings are refused on their own shape, never on the
# parser happening to fail: CPython 3.12 raised the JSON scanner's recursion
# ceiling, so `json.loads` raises RecursionError on 3.11 and earlier but
# accepts 1000-deep input on 3.12+. Relying on that difference made the same
# request an "invalid parameter" on one interpreter and a stack-exhausting
# success on another. A byte cap does not help — 2 KB nests 1000 deep.
#
# Real domains sit at 2-4 levels (`[("id", "in", [1, 2])]` is 3), so this
# ceiling is far above anything legitimate.
_MAX_PARAM_NESTING = 32


def _nesting_depth(raw: str) -> int:
    """Deepest bracket nesting in `raw`, ignoring brackets inside strings.

    Scanned character-wise rather than by parsing, so nothing large is built
    and no recursion happens. Quote-aware (with escapes) so a value that
    merely contains a bracket — a URL, a JSON blob in a char field — does not
    inflate the depth and trip the guard.
    """
    depth = deepest = 0
    quote: Optional[str] = None
    escaped = False
    for char in raw:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _check_param_nesting(raw: str, label: str) -> None:
    """Refuse a parameter string nested deeper than `_MAX_PARAM_NESTING`."""
    if _nesting_depth(raw) > _MAX_PARAM_NESTING:
        raise ValidationError(
            f"Invalid {label} parameter: nested deeper than {_MAX_PARAM_NESTING} levels."
        )


# Compact attribute set get_fields returns when the caller does not request
# specific attributes — enough to discover a model's schema without the noise.
CURATED_FIELD_ATTRIBUTES = (
    "type",
    "string",
    "required",
    "readonly",
    "relation",
    "selection",
)


def _withheld_fields_note(withheld: List[str]) -> str:
    """Note explaining that credential-like fields were withheld from a bulk read.

    Wording comes from field_security.withheld_note (shared with the resource
    surface); this wrapper only adds the tools-side 'fields' parameter hint.
    """
    return f"{withheld_note(withheld)} (use the 'fields' parameter)."


def _validate_record_id(record_id: int, label: str = "record ID") -> None:
    """Reject ids outside the XML-RPC 32-bit range before any RPC call.

    XML-RPC marshals ints as 32-bit — an oversized id would raise
    OverflowError mid-request; ids below 1 can never exist in Odoo.
    """
    if record_id < 1 or record_id > XMLRPC_MAX_INT:
        raise ValidationError(
            f"Invalid {label} {record_id}: must be between 1 and {XMLRPC_MAX_INT}"
        )


def _validate_method_call(model: str, method: str) -> None:
    """Reject models/methods call_model_method must never touch.

    See _BLOCKED_METHOD_CALLS for the rationale.
    """
    if any(
        model == blocked or model.startswith(blocked + ".")
        for blocked in _BLOCKED_METHOD_CALL_MODELS
    ):
        raise ValidationError(
            f"Method calls on '{model}' are not permitted via MCP: its methods "
            "run with elevated privileges (server actions / scheduled jobs)."
        )
    if method.startswith("web_"):
        raise ValidationError(
            f"Method '{method}' belongs to the web_* data-access family; use the "
            "dedicated search/CRUD tools instead. call_model_method is for "
            "business methods."
        )
    if method in _BLOCKED_METHOD_CALLS:
        raise ValidationError(
            f"Method '{method}' is an ORM data-access primitive; use the dedicated "
            "tools (search_records, get_record, create_record, update_record, "
            "delete_record) instead. call_model_method is for business methods."
        )
    if method in _BLOCKED_PRIVILEGED_METHOD_NAMES:
        raise ValidationError(
            f"Method '{method}' is blocked on every model because it can trigger "
            "privileged server actions or scheduled jobs (ir.actions.server / "
            "ir.cron), directly or via a delegating model."
        )


def _check_xmlrpc_int_bounds(value: Any, path: str = "arguments") -> None:
    """Reject ints outside the signed-32-bit XML-RPC marshalling range.

    Recursively walks lists/tuples/dicts so an oversized int anywhere in the
    positional arguments or keyword_arguments fails cleanly before any RPC —
    xmlrpc.client would otherwise raise OverflowError mid-marshal. bools are
    exempt (bool subclasses int; xmlrpc marshals them as <boolean>). The
    raised message names the offending path and value.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if not (-XMLRPC_MAX_INT - 1 <= value <= XMLRPC_MAX_INT):
            raise ValidationError(
                f"Integer argument {value} at {path} is outside the XML-RPC "
                f"32-bit marshalling range [{-XMLRPC_MAX_INT - 1}, {XMLRPC_MAX_INT}]"
            )
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_xmlrpc_int_bounds(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_xmlrpc_int_bounds(item, f"{path}[{key!r}]")


def _validate_offset(offset: int, limit: int) -> None:
    """Reject negative or excessively deep pagination offsets.

    Postgres walks (and discards) every skipped row, so an unbounded
    offset is query-cost amplification even with a capped limit.
    """
    if offset < 0:
        raise ValidationError(f"offset must be >= 0, got {offset}")
    max_offset = max_offset_for(limit)
    if offset > max_offset:
        raise ValidationError(
            f"offset {offset} exceeds the maximum of {max_offset} for "
            f"limit {limit} — narrow the domain or use 'order' to bring "
            "the target records into earlier pages"
        )


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

        # Never auto-surface obviously-sensitive fields to the LLM. The exact-name
        # blocklist above misses custom fields like `openai_api_key` or
        # `webhook_secret`, and the business-pattern bonus below could otherwise
        # even boost them.
        if is_sensitive_field_name(field_name):
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

        # Cap non-stored fields: reading them triggers per-row compute. Note
        # fields_get() never returns a `compute` key, so gate on `store` —
        # store=False means computed or related. Related fields (fields_get
        # exposes `related`) are exempt: they resolve via cheap joins — incl.
        # `_inherits` delegation, e.g. most business fields on product.product
        # — not per-row compute. Deliberate divergence from the reference
        # in-process implementation.
        if not field_info.get("store", True) and not field_info.get("related"):
            score = min(score, 30)  # Cap non-stored fields at low score

        # Exclude large field types completely
        if field_type in (*BINARY_FIELD_TYPES, "html"):
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

            field_scores.sort(key=lambda x: x[1], reverse=True)

            # Select top N fields based on configuration
            max_fields = self.config.max_smart_fields
            selected_fields = [field_name for field_name, _ in field_scores[:max_fields]]

            # Ensure essential fields are always included
            essential_fields = ["id", "name", "display_name", "active"]
            for field in essential_fields:
                if field in fields_info and field not in selected_fields:
                    selected_fields.append(field)

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

    def _binary_field_names(self, model: str) -> Set[str]:
        """Names of binary/image fields on ``model``.

        Empty set when field metadata is unavailable — callers then skip the
        binary→URI swap and values pass through unchanged. Since reads use
        ``bin_size=True``, a populated binary field then surfaces as its size
        placeholder (e.g. ``"12.5 KB"``) instead of a resource URI — logged as
        a warning because the degradation is caller-visible.

        Deliberately does NOT retry. ``fields_get`` failures are rarely
        transient (missing model, denied access), an immediate re-dial cannot
        outlast a hung socket, and doubling the timeout on a path whose
        failure is already graceful is the wrong trade. The sibling
        ``_get_smart_default_fields`` call in the same request does not retry
        either; the result is cached per model, so a healthy path pays once.
        """
        try:
            fields_info = self.connection.fields_get(model)
            return {
                name
                for name, meta in fields_info.items()
                if (meta or {}).get("type") in BINARY_FIELD_TYPES
            }
        except Exception as e:
            logger.warning(
                f"Could not get binary field names for {model} "
                f"(binary values will not be swapped for resource URIs): {e}"
            )
            return set()

    @staticmethod
    def _replace_binary_values(
        model: str,
        record: Dict[str, Any],
        binary_names: Set[str],
        record_id: Optional[int] = None,
    ) -> None:
        """Swap populated binary values for ``odoo://`` URIs in place.

        Reads pass ``bin_size=True`` so populated binaries arrive as truthy
        size placeholders (e.g. ``"12.5 KB"``) — the full bytes are fetched
        only on ``resources/read`` of the swapped URI. Empty binaries stay
        ``False``. ``ir.attachment.datas`` gets the attachment-specific
        ``odoo://attachment/{id}`` URI so its stored mimetype and
        ``type='url'`` handling apply on read.

        Only keys already present in ``record`` are touched — a caller that
        requested ``fields=['name', 'type']`` must never gain an unrequested
        ``datas`` key. A ``type='url'`` attachment stores its payload as a
        URL, so ``datas`` is ``False``; that falsy ``datas`` is still swapped,
        but only when the record carries BOTH ``type`` and ``datas`` keys and
        ``type == 'url'`` — the attachment resource serves the URL as
        ``text/uri-list``. Empty binary attachments (``type='binary'``,
        ``datas=False``) correctly stay ``False``; when ``type`` was not read,
        the url-vs-empty split is unknowable, so a falsy ``datas`` is left
        as-is.
        """
        rid = record_id if record_id is not None else record.get("id")
        if not isinstance(rid, int) or rid <= 0:
            return
        url_attachment = (
            model == "ir.attachment"
            and "type" in record
            and "datas" in record
            and record.get("type") == "url"
        )
        for name in binary_names:
            if name not in record:
                continue
            value = record[name]
            # Only an actual binary payload becomes a URI. Odoo declares
            # several non-stored "widget" fields as Binary while returning a
            # dict (sale.order.tax_totals, account.move.invoice_payments_widget,
            # needed_terms, payment_term_details, ...); bin_size does not
            # apply to those, so they arrive as the dict itself. Swapping one
            # for a URI would both drop data the caller explicitly asked for
            # and advertise a URI whose read fails ("Unexpected binary value
            # type: dict"), so any non-string payload passes through untouched.
            if not (isinstance(value, str) and value) and not (name == "datas" and url_attachment):
                continue
            try:
                record[name] = build_binary_uri(model, rid, name)
            except URIValidationError:
                # A field name the URI grammar rejects (leading underscore,
                # non-ASCII) has no servable URI — leave the value as Odoo
                # returned it. Raising here would abort the entire
                # get_record/search_records call over one odd field.
                logger.debug(f"No binary URI for {model}.{name}; leaving value unchanged")

    def _resolve_related_summaries(
        self, model: str, record: Dict[str, Any]
    ) -> Optional[Dict[str, List[RelatedSummary]]]:
        """Resolve display names for small x2many collections (inline preview).

        For each one2many/many2many field in ``record`` holding between 1 and
        ``MAX_RELATED_ITEMS`` ids, check ``read`` access on the relation and
        read the related records' ``display_name`` (one read per field). A
        field whose relation cannot be read is silently skipped — the ids in
        ``record`` stay untouched either way. Larger collections are skipped
        so the extra reads stay cheap and the output short.

        At most ``MAX_RELATED_SUMMARY_FIELDS`` fields are ATTEMPTED per record
        (a failed access check or read still counts — it cost a round trip):
        every one costs an access check plus a read RPC, so an all-fields read
        of a relation-heavy model would otherwise fan out into dozens of
        serialized round trips inside a single tool call.
        """
        try:
            fields_info = self.connection.fields_get(model)
        except Exception as e:
            logger.debug(f"Could not get field metadata for related summaries: {e}")
            return None
        summaries: Dict[str, List[RelatedSummary]] = {}
        # Counts fields we SPEND round trips on, not fields we successfully
        # resolve: a relation the caller cannot read still costs an access
        # check (and, in standard mode, an HTTP call) before it fails, so
        # budgeting on successes would let a record full of unreadable
        # relations fan out without bound.
        attempted = 0
        for name, value in record.items():
            if attempted >= MAX_RELATED_SUMMARY_FIELDS:
                break
            meta = fields_info.get(name) or {}
            if meta.get("type") not in ("one2many", "many2many"):
                continue
            relation = meta.get("relation")
            if not relation or not isinstance(value, list):
                continue
            if not 0 < len(value) <= MAX_RELATED_ITEMS:
                continue
            ids = [item for item in value if isinstance(item, int)]
            if len(ids) != len(value):
                continue
            attempted += 1
            try:
                self.access_controller.validate_model_access(relation, "read")
                related = self.connection.read(relation, ids, ["display_name"])
            except Exception as e:
                logger.debug(f"Skipping related summary for {model}.{name}: {e}")
                continue
            summaries[name] = [
                RelatedSummary(
                    id=rec["id"],
                    display_name=rec.get("display_name") or f"id {rec['id']}",
                )
                for rec in related
            ]
        return summaries or None

    async def _gate_attachment_target(self, res_model: Any, label: str) -> None:
        """Refuse an ir.attachment operation aimed at an inaccessible model."""
        if not res_model or res_model == "ir.attachment":
            return
        try:
            await asyncio.to_thread(self.access_controller.validate_model_access, res_model, "read")
        except AccessControlUnavailableError:
            # Checked before AccessControlError, its base: "could not verify"
            # is an outage, not a denial, and must stay retryable.
            raise
        except AccessControlError as e:
            raise MCPPermissionError(
                f"Access denied: {label} '{res_model}', which is not accessible via MCP"
            ) from e

    async def _gate_attachment_records(self, record_ids: Sequence[int]) -> None:
        """Refuse ir.attachment rows whose res_model is not accessible.

        The row carries `url` and `index_content` (the extracted document
        text), so metadata reads need the same gate the payload readers use.

        Applied to WRITES as well as reads. Ungated, `update_record` on an
        attachment could repoint `res_model` from an excluded model to an
        allowed one and then read it back — a full bypass of the read gate,
        not merely an inconsistency — while `delete_record` would reach
        documents hanging off models deliberately left out of the allowlist.
        """
        if not record_ids:
            return
        rows = await asyncio.to_thread(
            self.connection.search_read,
            "ir.attachment",
            [["id", "in", list(record_ids)]],
            ["res_model"],
            context={"active_test": False},
        )
        for row in rows:
            await self._gate_attachment_target(
                row.get("res_model"), f"attachment {row.get('id')} belongs to"
            )

    def _parse_domain_input(self, domain: Optional[Any]) -> List[Any]:
        """Coerce a domain parameter into an Odoo domain list.

        Accepts a list (passed through), a JSON string, a Python-literal
        string with single quotes / ``True``/``False`` capitalization, or
        ``None`` (returns ``[]``). Raises ``ValidationError`` on anything
        that doesn't yield a list, and on a list whose prefix operators are
        unbalanced — see ``check_domain_balance``, which callers appending
        an internal scope depend on.
        """
        if domain is None:
            return []
        if not isinstance(domain, str):
            if not isinstance(domain, list):
                raise ValidationError(f"Domain must be a list, got {type(domain).__name__}")
            # Same guard the write paths use: an oversized int anywhere in the
            # domain would raise OverflowError mid-marshal and surface as a
            # transport-flavoured "Connection error", which is exactly the
            # message this check exists to replace for plain bad input.
            _check_xmlrpc_int_bounds(domain, "domain")
            check_domain_balance(domain)
            return domain

        _check_param_nesting(domain, "domain")
        try:
            parsed = json.loads(domain)
        except (json.JSONDecodeError, RecursionError) as e:
            # RecursionError, not just a decode error: json.loads recurses per
            # nesting level, so a 2 KB '[[[[...]]]]' string blows the stack and
            # would otherwise leave this parser as an unexpected failure —
            # logged at ERROR and surfaced as a generic sanitized message
            # instead of the clean "invalid domain" below. A byte cap does not
            # help; the input is tiny.
            # literal_eval handles single quotes and True/False natively,
            # without corrupting those substrings inside quoted values.
            try:
                parsed = _parse_python_literal(domain)
            except (ValueError, SyntaxError, RecursionError):
                raise ValidationError(
                    f"Invalid domain parameter. Expected JSON array or Python list, "
                    f"got: {domain[:100]}..."
                ) from e

        if not isinstance(parsed, list):
            raise ValidationError(f"Domain must be a list, got {type(parsed).__name__}")
        _check_xmlrpc_int_bounds(parsed, "domain")
        check_domain_balance(parsed)

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
                offset: Number of records to skip (capped at 1000 pages of
                    `limit`, min 10000 — narrow the domain or use `order`
                    instead of paging that deep)
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
            field_names: Optional[List[str]] = None,
            attributes: Optional[List[str]] = None,
            ctx: Optional[Context] = None,
        ) -> FieldsResult:
            """Describe a model's fields: type, label, required/readonly,
            relation target, and selection options. Use it to discover a
            model's schema before reading or writing records.

            Args:
                model: Technical model name (e.g. 'res.partner').
                field_names: Restrict the result to these field names.
                    Omit to describe every field on the model. An empty
                    list [] is treated like omitting it (all fields).
                attributes: Which field attributes to return. Omit for the
                    curated default set (type, string, required, readonly,
                    relation, selection); an empty list [] is treated like
                    omitting it. An explicit list REPLACES the curated
                    default set — include the defaults in your list if you
                    still need them (e.g. ["type", "string", "help", "store"]).

            Returns:
                Field definitions sorted by name, with the total count.
            """
            return await self._handle_get_fields_tool(model, field_names, attributes, ctx)

        @self.app.tool(
            title="Get Current Context",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def get_current_context(ctx: Optional[Context] = None) -> CurrentContextResult:
            """Return the current session context: the connected user, their
            timezone, the active company plus any other allowed companies, and
            UTC datetime-handling guidance. Call it when unsure which user or
            company a request runs as, or how to interpret datetimes.
            Spec-compliant clients also receive this via the initialize
            response.

            Returns:
                Structured user/company/timezone context plus the formatted
                text block.
            """
            return await self._handle_get_current_context_tool(ctx)

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
            subject: Optional[str] = None,
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
                subject: Optional message subject line

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
                subject,
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
            groupby: Optional[List[str]] = None,
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
                groupby: Group expressions. Field names, optionally
                    with a granularity suffix for date/datetime fields:
                    ``["date_order:month"]``, ``["partner_id"]``,
                    ``["partner_id", "date_order:year"]``. Omit (or pass
                    ``[]``) for a single overall-aggregate row — e.g. a
                    filtered count via the default ``__count`` aggregate.
                aggregates: Aggregate expressions of the form ``"field:operator"``
                    (sum, avg, min, max, count, count_distinct, array_agg, ...).
                    Examples: ``["amount_total:sum"]``, ``["__count"]``.
                    ``["id:count"]`` works on Odoo 17+ only — use
                    ``__count`` for a row count on every version.
                    If omitted or empty, defaults to ``["__count"]`` so each
                    group carries a count. Pass ``["__count", "amount_total:sum"]``
                    to get both.
                domain: Odoo domain filter — list, JSON string, or None.
                order: Sort expression over groupby keys / aggregates,
                    e.g. ``"date_order:month"`` or ``"amount_total:sum desc"``.
                limit: Maximum number of groups. Defaults to
                    ``ODOO_MCP_DEFAULT_LIMIT``; capped at ``ODOO_MCP_MAX_LIMIT``.
                offset: Number of groups to skip (capped at 1000 pages of
                    ``limit``, min 10000).

            Drilldown: AND a group's ``__extra_domain`` with the ``domain``
                you passed — ``search_records(model, domain=[*your_domain,
                *group["__extra_domain"]])``. On Odoo 19 it is only the
                group's own condition; on older servers it is already the
                full domain (your filter included). Re-ANDing is idempotent,
                so the same call is correct on every version.

            Returns:
                ``AggregateResult`` with ``groups`` (list of dicts; each contains
                the groupby keys, ``__count``, and any requested aggregates),
                the echoed ``model``, ``groupby``, and ``aggregates``, plus
                ``has_more`` (more groups exist beyond this page) and
                ``next_hint`` (suggested follow-up call when ``has_more``).

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

                # Filtered total (no grouping): one row with __count
                aggregate_records("res.partner", domain=[["is_company", "=", True]])
            """
            result = await self._handle_aggregate_records_tool(
                model, groupby, aggregates, domain, order, limit, offset, ctx
            )
            return AggregateResult(**result)

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

                Guardrails: methods on ``ir.actions.*``/``ir.cron`` (their methods
                self-elevate past the user's ACLs), the ``web_*`` data-access
                family, and ORM CRUD/data-access primitives (``create``, ``read``,
                ``search_read``, ...) are refused — use the dedicated CRUD/search
                tools. List results are truncated to 100 items.

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
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await self._ctx_info(ctx, f"Searching {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                parsed_domain = self._parse_domain_input(domain)
                if model == "ir.attachment":
                    # Scope to accessible res_models — an attachment row
                    # carries url and index_content (the extracted document
                    # text), so the allowlist gate must cover metadata, not
                    # only payloads. Appended, not prefixed with an explicit
                    # "&": Odoo normalizes a flat sequence of expressions
                    # with implicit ANDs, whereas a hand-written "&" would
                    # bind only the first term of a multi-leaf domain. That
                    # normalization only holds for a balanced caller domain,
                    # which _parse_domain_input has already enforced — an
                    # unbalanced one would capture this scope as an operand.
                    scope = await asyncio.to_thread(
                        attachment_scope_domain, self.config, self.access_controller
                    )
                    if scope:
                        parsed_domain = list(parsed_domain) + scope

                # Handle fields parameter - can be string or list
                parsed_fields = fields
                if fields is not None and isinstance(fields, str):
                    # Parse string to list
                    _check_param_nesting(fields, "fields")
                    try:
                        parsed_fields = json.loads(fields)
                        if not isinstance(parsed_fields, list):
                            raise ValidationError(
                                f"Fields must be a list, got {type(parsed_fields).__name__}"
                            )
                    except (json.JSONDecodeError, RecursionError):
                        # RecursionError: see _parse_domain_input — deeply
                        # nested input exhausts the stack inside json.loads.
                        # Try Python literal eval as fallback
                        try:
                            import ast

                            parsed_fields = ast.literal_eval(fields)
                            if not isinstance(parsed_fields, list):
                                raise ValidationError(
                                    f"Fields must be a list, got {type(parsed_fields).__name__}"
                                )
                        except (ValueError, SyntaxError, RecursionError) as e:
                            raise ValidationError(
                                f"Invalid fields parameter. Expected JSON array or Python list, got: {fields[:100]}..."
                            ) from e

                # Set defaults
                if limit is None or limit <= 0:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                _validate_offset(offset, limit)

                # Search for records
                record_ids = await asyncio.to_thread(
                    self.connection.search,
                    model,
                    parsed_domain,
                    limit=limit,
                    offset=offset,
                    order=order,
                )

                # Always count. Inferring "a short page holds every match"
                # is wrong for models whose _search post-filters access in
                # Python AFTER the SQL limit — mail.message does exactly that
                # for any non-superuser, so a limit=10 search returns 5 rows
                # while 85 match. That inference undercounted `total` and
                # stopped pagination early; search_count returns the true
                # accessible count (verified: 86 == len(unlimited search)).
                total_count = await asyncio.to_thread(
                    self.connection.search_count, model, parsed_domain
                )
                # No progress notifications — see CLAUDE.md "MCP context conventions".
                await self._ctx_info(ctx, f"Found {total_count} records")

                # Determine which fields to fetch. An empty list means
                # "minimal/default" — Odoo would interpret [] as ALL fields,
                # so treat it like None (smart defaults).
                fields_to_fetch = parsed_fields
                if parsed_fields is None or parsed_fields == []:
                    # Use smart field selection to avoid serialization issues
                    fields_to_fetch = await asyncio.to_thread(self._get_smart_default_fields, model)
                    # See _handle_get_record_tool: a falsy field list makes
                    # Odoo read every field, so it must take the None branch.
                    if not fields_to_fetch:
                        fields_to_fetch = None
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

                # Read records. bin_size: binary fields come back as size
                # placeholders instead of full base64 blobs — populated ones
                # are swapped for odoo:// resource URIs below.
                records = []
                withheld_fields: Set[str] = set()
                if record_ids:
                    records = await asyncio.to_thread(
                        self.connection.read,
                        model,
                        record_ids,
                        fields_to_fetch,
                        {"bin_size": True},
                    )
                    if fields_to_fetch is None:
                        # Bulk all-fields read (["__all__"] or smart-default
                        # fallback): strip credential-like fields; an explicit
                        # field list is honored — see strip_sensitive_fields.
                        # Off the event loop: the name scan runs per record
                        # over potentially wide all-fields rows.
                        def _strip_all_records() -> Set[str]:
                            withheld: Set[str] = set()
                            for record in records:
                                withheld.update(strip_sensitive_fields(record))
                            return withheld

                        withheld_fields = await asyncio.to_thread(_strip_all_records)
                    # Swap populated binary values for resource URIs (never
                    # inline base64; empty binaries stay False)
                    binary_names = await asyncio.to_thread(self._binary_field_names, model)
                    if binary_names:
                        for record in records:
                            self._replace_binary_values(model, record, binary_names)
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
                    "note": (
                        _withheld_fields_note(sorted(withheld_fields)) if withheld_fields else None
                    ),
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
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
                _validate_record_id(record_id)

                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                if model == "ir.attachment":
                    await self._gate_attachment_records([record_id])
                await self._ctx_info(ctx, f"Getting {model}/{record_id}...")

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
                    # Normalize an empty selection to None: Odoo reads ALL
                    # fields for a falsy field list (check_field_access_rights
                    # replaces it with every readable field), so [] and None
                    # are the same read and must take the same bulk-read
                    # branch — credential strip on, metadata not claiming a
                    # limited set.
                    if not fields_to_fetch:
                        fields_to_fetch = None
                    use_smart_defaults = True
                    # None means smart selection failed and ALL fields are
                    # read — the metadata must not claim a limited set.
                    field_selection_method = (
                        "smart_defaults" if fields_to_fetch is not None else "all_fields_fallback"
                    )
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

                # Read the record. bin_size: binary fields come back as size
                # placeholders instead of full base64 blobs — populated ones
                # are swapped for odoo:// resource URIs below.
                records = await asyncio.to_thread(
                    self.connection.read, model, [record_id], fields_to_fetch, {"bin_size": True}
                )

                if not records:
                    raise ValidationError(f"Record not found: {model} with ID {record_id}")

                record = records[0]
                withheld_fields: List[str] = []
                if fields_to_fetch is None:
                    # Bulk all-fields read (["__all__"] or smart-default
                    # fallback): strip credential-like fields; an explicit
                    # field list is honored — see strip_sensitive_fields.
                    withheld_fields = strip_sensitive_fields(record)

                # Swap populated binary values for resource URIs (never
                # inline base64; empty binaries stay False)
                binary_names = await asyncio.to_thread(self._binary_field_names, model)
                if binary_names:
                    self._replace_binary_values(model, record, binary_names, record_id=record_id)

                # Inline preview: resolve display names for small x2many
                # collections (ids in the record stay untouched)
                related_summaries = await asyncio.to_thread(
                    self._resolve_related_summaries, model, record
                )

                # Process datetime fields in the record
                record = await asyncio.to_thread(self._process_record_dates, record, model)
                # Coerce XML-RPC types (Binary, DateTime) Pydantic can't serialize
                record = _json_safe(record)

                # Metadata accompanies a smart-default read and any bulk read
                # that withheld credential-like fields. Resolve the model's
                # field count for BOTH: an ["__all__"] read that withheld
                # something used to report total_fields_available: null.
                # fields_get is cached for unfiltered calls (and already warm
                # from the binary-name lookup above), so this is not an extra
                # round trip.
                metadata = None
                if use_smart_defaults or withheld_fields:
                    try:
                        all_fields_info = await asyncio.to_thread(self.connection.fields_get, model)
                        total_fields = len(all_fields_info)
                    except Exception:
                        pass

                if use_smart_defaults:
                    if field_selection_method == "all_fields_fallback":
                        note = "All fields returned (smart field selection unavailable)."
                    else:
                        note = f"Limited fields returned for performance. Use fields=['__all__'] for all fields or see odoo://{model}/fields for available fields."
                    metadata = FieldSelectionMetadata(
                        fields_returned=len(record),
                        field_selection_method=field_selection_method,
                        total_fields_available=total_fields,
                        note=note,
                    )

                # Surface withheld credential-like fields (bulk paths only).
                # Local name deliberately differs from the module-level
                # field_security.withheld_note import — shadowing it here
                # would hide the helper for the rest of this function.
                if withheld_fields:
                    withheld_message = _withheld_fields_note(withheld_fields)
                    if metadata is not None:
                        metadata.note = (
                            f"{metadata.note} {withheld_message}"
                            if metadata.note
                            else withheld_message
                        )
                    else:
                        metadata = FieldSelectionMetadata(
                            fields_returned=len(record),
                            field_selection_method=field_selection_method,
                            total_fields_available=total_fields,
                            note=withheld_message,
                        )

                return RecordResult(
                    record=record, metadata=metadata, related_summaries=related_summaries
                )

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except MCPPermissionError as e:
            # _gate_attachment_records' denial. Without this it would reach the
            # generic handler below: logged as an unexpected failure and its
            # actionable "belongs to <model>" text replaced by a generic one.
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get record: {sanitized_msg}") from e

    async def _handle_get_fields_tool(
        self,
        model: str,
        field_names: Optional[List[str]],
        attributes: Optional[List[str]],
        ctx=None,
    ) -> FieldsResult:
        """Handle get_fields tool request."""
        try:
            with perf_logger.track_operation("tool_get_fields", model=model):
                # Check model access (read — same ladder as get_record)
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await self._ctx_info(ctx, f"Getting fields for {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Truthiness is deliberate: [] ≡ omitted, matching the
                # repo-wide field-list convention (see search_records /
                # get_record `fields`).
                selected_attributes = (
                    list(attributes) if attributes else list(CURATED_FIELD_ATTRIBUTES)
                )
                # field_names go server-side as fields_get's allfields
                # filter; unknown names are silently omitted by Odoo
                # ([] ≡ omitted here too: no filter, every field returned).
                fields_metadata = await asyncio.to_thread(
                    self.connection.fields_get,
                    model,
                    selected_attributes,
                    list(field_names) if field_names else None,
                )

                fields = [
                    FieldInfo(**{"name": name, **meta})
                    for name, meta in sorted(fields_metadata.items())
                ]
                return FieldsResult(model=model, fields=fields, total=len(fields))

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_fields tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get fields: {sanitized_msg}") from e

    async def _handle_get_current_context_tool(self, ctx=None) -> CurrentContextResult:
        """Handle get_current_context tool request.

        Deliberately NOT gated by the access controller: it exposes only the
        caller's own user/company info — no new data surface — and must work
        even when res.users is not an MCP-enabled model (standard mode). On
        any read failure it degrades to ``CONTEXT_UNAVAILABLE_TEXT`` (the UTC
        guidance plus a note naming the likely cause) with null structured
        fields instead of erroring.
        """
        with perf_logger.track_operation("tool_get_current_context"):
            await self._ctx_info(ctx, "Reading current session context...")
            try:
                data = await asyncio.to_thread(get_user_context_data, self.connection)
            except Exception as e:
                logger.warning(f"Could not read user context, returning UTC guidance only: {e}")
                return CurrentContextResult(text=context_unavailable_text(str(e)))
            allowed = [CompanyInfo(**company) for company in data["allowed_companies"]]
            return CurrentContextResult(
                user_name=data["user_name"],
                login=data["login"],
                timezone=data["timezone"],
                company_id=data["company_id"],
                company_name=data["company_name"],
                allowed_companies=allowed or None,
                text=format_user_context(data),
            )

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
                            # '=like' is prefix-anchored; plain 'like' wraps the
                            # pattern as %ir.%% and matches a SUBSTRING, which
                            # silently drops every model merely CONTAINING
                            # 'ir.' or 'base.' (repair.order, ...) — and
                            # search_count undercounts identically, hiding it.
                            # Negated with the '!' prefix operator rather than
                            # 'not =like': that operator only exists on Odoo 19
                            # (odoo/orm/domains.py), and on 15-18 an unknown
                            # operator raises ValueError server-side, which
                            # would make this the only YOLO discovery tool that
                            # returns nothing on every supported older version.
                            "!",
                            ("model", "=like", "ir.%"),
                            "!",
                            ("model", "=like", "base.%"),
                        ]

                        # Query models from database, capped at
                        # MAX_LISTED_MODELS (context-flood guard for
                        # Studio-heavy DBs). A full page triggers a
                        # search_count so the reported total is the real
                        # model count, with an explicit truncation note.
                        model_records = await asyncio.to_thread(
                            self.connection.search_read,
                            "ir.model",
                            domain,
                            ["model", "name"],
                            order="name ASC",
                            limit=MAX_LISTED_MODELS,
                        )

                        total_available = len(model_records)
                        truncation_note = None
                        if len(model_records) >= MAX_LISTED_MODELS:
                            total_available = await asyncio.to_thread(
                                self.connection.search_count, "ir.model", domain
                            )
                            if total_available > MAX_LISTED_MODELS:
                                truncation_note = (
                                    f"listing truncated to {MAX_LISTED_MODELS} of "
                                    f"{total_available} models — narrow with "
                                    "search_records on ir.model"
                                )

                        # Prepare response with YOLO mode metadata
                        mode_desc = (
                            "READ-ONLY" if self.config.yolo_mode == "read" else "FULL ACCESS"
                        )
                        await self._ctx_info(
                            ctx,
                            f"YOLO mode ({mode_desc}): found {len(model_records)} models",
                        )

                        # Global YOLO operation flags — apply to every model
                        yolo_operations = {
                            "read": True,
                            "write": self.config.yolo_mode == "true",
                            "create": self.config.yolo_mode == "true",
                            "unlink": self.config.yolo_mode == "true",
                        }

                        # Create metadata about YOLO mode
                        yolo_metadata = {
                            "enabled": True,
                            "level": self.config.yolo_mode,  # "read" or "true"
                            "description": mode_desc,
                            "warning": "🚨 All models accessible without MCP security!",
                            "operations": yolo_operations,
                        }

                        # Rows carry no per-model operations in YOLO mode: the
                        # flags are global and already reported once under
                        # yolo_mode.operations. Standard mode still stamps them
                        # per row, where they genuinely differ per model.
                        models_list = [
                            {
                                "model": record["model"],
                                "name": record["name"] or record["model"],
                            }
                            for record in model_records
                        ]

                        logger.info(
                            f"YOLO mode ({mode_desc}): Listed {len(model_records)} models from database"
                        )

                        return {
                            "yolo_mode": yolo_metadata,
                            "models": models_list,
                            # total counts what came back; total_available is
                            # the database count, which differs only when the
                            # listing was truncated. Both are always present
                            # so a caller never has to know which mode or
                            # which server produced the response.
                            "total": len(models_list),
                            "total_available": total_available,
                            "note": truncation_note,
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

                # Return proper JSON structure with enriched models array.
                # Standard mode lists every enabled model, so the returned
                # count and the available count are the same — both are still
                # emitted so the response shape matches YOLO mode.
                return {
                    "models": enriched_models,
                    "total": len(enriched_models),
                    "total_available": len(enriched_models),
                }
        except ValidationError:
            raise
        except AccessControlError as e:
            # A refusal explains itself ("...not a member of the MCP User
            # group"); the generic wrapper below buried that under "Failed to
            # list models", leaving the caller with nothing to act on.
            raise ValidationError(access_denied_message(e)) from e
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
                # Every template below is read-only, so a model the caller
                # cannot READ should not be advertised — following the hint
                # would only earn an access denial. Best-effort by design:
                # the flag is read from the "operations" block that newer
                # MCP modules include in /mcp/models. Modules that return
                # only {model, name} (which is why _handle_list_models_tool
                # resolves permissions per model instead) yield no flag, and
                # the default keeps the model listed rather than paying a
                # per-model permission request just to build this listing.
                model_names = [
                    m["model"]
                    for m in enabled_models
                    if (m.get("operations") or {}).get("read", True)
                ]

            # Define the resource templates.
            # Keep the descriptions in sync with the @app.resource
            # registrations in resources.py — those are what
            # resources/templates/list advertises.
            templates = [
                {
                    "uri_template": "odoo://{model}/record/{record_id}",
                    "description": "Retrieve a specific record from an Odoo model by ID",
                    "parameters": {
                        "model": "Odoo model name (e.g., res.partner)",
                        "record_id": "Record ID (e.g., 10)",
                    },
                    "example": "odoo://res.partner/record/10",
                },
                {
                    "uri_template": "odoo://{model}/search",
                    "description": "Search records with default settings (first 10 records)",
                    "parameters": {
                        "model": "Odoo model name",
                    },
                    "example": "odoo://res.partner/search",
                    "note": "Query parameters are not supported. Use search_records tool for advanced queries.",
                },
                {
                    "uri_template": "odoo://{model}/count",
                    "description": "Count all records in an Odoo model",
                    "parameters": {
                        "model": "Odoo model name",
                    },
                    "example": "odoo://res.partner/count",
                    "note": "Query parameters are not supported. Use search_records tool for filtered counts.",
                },
                {
                    "uri_template": "odoo://{model}/fields",
                    "description": "Get field definitions and metadata for an Odoo model",
                    "parameters": {"model": "Odoo model name"},
                    "example": "odoo://res.partner/fields",
                },
                {
                    "uri_template": "odoo://{model}/record/{record_id}/{field}",
                    "description": (
                        "Fetch a binary/image field from an Odoo record (e.g. an image "
                        "or stored document) instead of inlining base64"
                    ),
                    "parameters": {
                        "model": "Odoo model name (e.g., res.partner)",
                        "record_id": "Record ID (e.g., 10)",
                        "field": "Binary/image field name (e.g., image_128)",
                    },
                    "example": "odoo://res.partner/record/10/image_128",
                },
                {
                    "uri_template": "odoo://attachment/{attachment_id}",
                    "description": "Fetch an ir.attachment by ID",
                    "parameters": {"attachment_id": "ir.attachment record ID (e.g., 42)"},
                    "example": "odoo://attachment/42",
                },
            ]

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
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "create"
                )
                await self._ctx_info(ctx, f"Creating record in {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate required fields
                if not values:
                    raise ValidationError("No values provided for record creation")

                # Oversized ints anywhere in the values (incl. nested x2many
                # command tuples) would raise OverflowError mid-marshal —
                # fail cleanly before any RPC.
                _check_xmlrpc_int_bounds(values, "values")

                if model == "ir.attachment":
                    # Planting a document on a model left out of the allowlist
                    # is the write-side of the same sidestep the read gate
                    # closes.
                    await self._gate_attachment_target(
                        values.get("res_model"), "attachment would be attached to"
                    )

                record_id = await asyncio.to_thread(self.connection.create, model, values)

                # display_name only — universal and cheap; get_record for more.
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
        except MCPPermissionError as e:
            # Attachment-gate denial surfaced verbatim — see _handle_get_record_tool.
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
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
                _validate_record_id(record_id)

                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Updating {model}/{record_id}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not values:
                    raise ValidationError("No values provided for record update")

                # Oversized ints anywhere in the values (incl. nested x2many
                # command tuples) would raise OverflowError mid-marshal —
                # fail cleanly before any RPC.
                _check_xmlrpc_int_bounds(values, "values")

                if model == "ir.attachment":
                    # Both directions. Gating the CURRENT owner stops the
                    # escalation: repoint an excluded model's attachment at an
                    # allowed one and the read gate would then wave it through.
                    # Gating the NEW owner stops planting.
                    await self._gate_attachment_records([record_id])
                    if "res_model" in values:
                        await self._gate_attachment_target(
                            values["res_model"], "attachment would be moved to"
                        )

                # Check if record exists (only fetch ID to verify existence)
                existing = await asyncio.to_thread(self.connection.read, model, [record_id], ["id"])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Update the record
                success = await asyncio.to_thread(self.connection.write, model, [record_id], values)

                # display_name only — universal and cheap; get_record for more.
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
        except MCPPermissionError as e:
            # Attachment-gate denial surfaced verbatim — see _handle_get_record_tool.
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
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
                _validate_record_id(record_id)

                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "unlink"
                )
                await self._ctx_info(ctx, f"Deleting {model}/{record_id}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                if model == "ir.attachment":
                    # Destroying a document behind an excluded model is at
                    # least as serious as reading it.
                    await self._gate_attachment_records([record_id])

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
        except MCPPermissionError as e:
            # Attachment-gate denial surfaced verbatim — see _handle_get_record_tool.
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
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
        subject: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle post message tool request."""
        subtype_xmlid_map = {
            "note": "mail.mt_note",
            "comment": "mail.mt_comment",
        }
        try:
            with perf_logger.track_operation("tool_post_message", model=model):
                _validate_record_id(record_id)
                for partner_id in partner_ids or []:
                    _validate_record_id(partner_id, label="partner ID")
                for attachment_id in attachment_ids or []:
                    _validate_record_id(attachment_id, label="attachment ID")

                # Check model access — message_post mutates the record
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Posting message to {model}/{record_id}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate body before any XML-RPC call
                if not body or not body.strip():
                    raise ValidationError("body must not be empty")

                # message_post repoints the attachments it is handed onto the
                # thread record, so handing it an excluded model's attachment
                # would move that document somewhere readable.
                await self._gate_attachment_records(attachment_ids or [])

                # Build kwargs — omit partner_ids/attachment_ids when None
                # (empty list means "clear all" in some Odoo contexts)
                kwargs: Dict[str, Any] = {
                    "body": body,
                    "message_type": message_type,
                    "subtype_xmlid": subtype_xmlid_map[subtype],
                }
                if subject:
                    kwargs["subject"] = subject
                if partner_ids is not None:
                    kwargs["partner_ids"] = partner_ids
                if attachment_ids is not None:
                    kwargs["attachment_ids"] = attachment_ids
                if body_is_html:
                    # Odoo 17+ escapes any plain str body — opt-in flag preserves HTML
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
        except MCPPermissionError as e:
            # Attachment-gate denial surfaced verbatim — see _handle_get_record_tool.
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
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
          NOTE the two are not identical: legacy ``read_group`` sets
          ``__domain`` to AND(caller domain, group condition) — the FULL
          domain — while v19's ``formatted_read_group`` emits only the group
          condition. Both are correct under the documented contract ("AND it
          with the domain you passed"), since re-ANDing the caller's domain is
          idempotent; the <19 value is simply a redundant superset. Stripping
          the caller's domain back out of Odoo's normalized prefix-notation
          domain is not reliably possible, so the contract is what makes the
          two versions agree.
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
              read_group uses its default). Passed through verbatim; legacy
              read_group expects a bare field/groupby key, so ordering by a
              v19 aggregate expression (``"amount_total:sum desc"``) raises a
              fault here that surfaces cleanly as a ValidationError.
        """
        # __count is implicit in read_group; passing it as a field raises a fault.
        fields_kwarg = [a for a in aggregates if a != "__count"]

        # read_group returns every aggregate under its BARE field name, so an
        # aggregate over a field that is ALSO a groupby key collides with it:
        # Odoo builds each bucket by zipping keys to values, the aggregate
        # wins, and the bucket loses both its group identity and its drilldown
        # domain (every row comes back reading `partner_id: 1`). Odoo 19's
        # formatted_read_group keys aggregates separately and handles this
        # correctly, so rather than silently returning corrupted groups on
        # older servers, refuse the combination and say why.
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

        # Same root cause between two aggregates: read_group keys results by
        # the BARE field name, so amount_total:sum and amount_total:avg both
        # land on 'amount_total' — Odoo keeps the last one and the rename loop
        # relabels that single value with the FIRST spec. The second aggregate
        # silently disappears and the survivor carries the wrong operator.
        # v19's formatted_read_group keys them separately and is unaffected.
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
        # Collisions with a groupby key were refused above, so every rename
        # here lands on a key the groupby does not already own.
        agg_renames = [(a.split(":", 1)[0], a) for a in fields_kwarg if ":" in a]

        # Whitelist of keys allowed in the final bucket: groupby specs +
        # requested aggregates (post-rename) + known metadata keys.
        allowed_keys = self._READ_GROUP_META_KEYS | set(groupby) | set(fields_kwarg)

        normalized: List[Dict[str, Any]] = []
        for bucket in groups:
            if "__domain" in bucket:
                bucket["__extra_domain"] = bucket.pop("__domain")
            elif not groupby:
                # An overall-total row has no grouping condition, and Odoo
                # 15/16 omit __domain entirely for it. Emit the empty extra
                # domain explicitly so the key is present on every version
                # (the documented contract is to AND it with the caller's
                # domain, and ANDing nothing is a no-op).
                bucket["__extra_domain"] = []
            for bare, full in agg_renames:
                if bare in bucket and full != bare:
                    bucket[full] = bucket.pop(bare)
            normalized.append({k: v for k, v in bucket.items() if k in allowed_keys})
        return normalized

    async def _handle_aggregate_records_tool(
        self,
        model: str,
        groupby: Optional[List[str]],
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

                # Omitted/empty groupby collapses to a single overall row —
                # both dispatch paths support it natively (one bucket with
                # the requested aggregates), making this the tool for
                # filtered counts via the default __count.
                groupby = list(groupby) if groupby else []

                parsed_domain = self._parse_domain_input(domain)
                if model == "ir.attachment":
                    scope = await asyncio.to_thread(
                        attachment_scope_domain, self.config, self.access_controller
                    )
                    if scope:
                        # Appended, not "&"-prefixed — see the matching comment
                        # in _handle_search_tool.
                        parsed_domain = list(parsed_domain) + scope

                # Limit defaults & capping (mirror search_records)
                if limit is None or limit <= 0:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                _validate_offset(offset, limit)

                # Default to ['__count'] when caller omits aggregates —
                # otherwise formatted_read_group returns only the groupby
                # keys with no quantitative data, which defeats the tool.
                effective_aggregates = aggregates if aggregates else ["__count"]

                # Peek one group past the page: the grouping methods offer no
                # cheap "count of groups", so request limit+1 — an extra row
                # coming back means the page is truncated, and has_more is
                # signalled rather than passing off a partial "top N" as
                # complete.
                peek_limit = limit + 1

                # Version dispatch: formatted_read_group is Odoo 19+ only;
                # fall back to read_group with response normalization on
                # older versions. When the version is unknown (None), assume
                # newer and let the XML-RPC fault surface — the caller can
                # set ODOO_DB or check the connection log.
                major = await asyncio.to_thread(self.connection.get_major_version)
                if major is not None and major < 19:
                    # Odoo 15/16 alias an `id:<op>` aggregate to the bare key
                    # "id" and then delete it unconditionally
                    # (_read_group_format_result: `del data['id']`), so the
                    # aggregate silently vanishes. 17/18 keep it, which is
                    # why the docstring offers `id:count` for 17+ only.
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
                                f"{major}: read_group drops the 'id' key before returning, "
                                "so the value never arrives. Use '__count' for a row count."
                            )
                    groups = await asyncio.to_thread(
                        self._call_read_group_normalized,
                        model,
                        parsed_domain,
                        groupby,
                        effective_aggregates,
                        order,
                        peek_limit,
                        offset,
                    )
                else:
                    kwargs: Dict[str, Any] = {
                        "groupby": groupby,
                        "aggregates": effective_aggregates,
                        "limit": peek_limit,
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

                # Drop the peeked extra row; its presence means more groups
                # exist beyond this page.
                has_more = len(groups) > limit
                if has_more:
                    groups = groups[:limit]
                # Suppress the hint when the next page would overrun the
                # offset cap _validate_offset enforces — don't suggest a call
                # it will reject.
                next_offset = offset + limit
                if has_more and next_offset <= max_offset_for(limit):
                    next_hint = f"aggregate_records with offset={next_offset}, limit={limit}"
                else:
                    next_hint = None

                await self._ctx_info(ctx, f"Returning {len(groups)} groups")

                return {
                    "groups": groups,
                    "model": model,
                    "groupby": groupby,
                    "aggregates": effective_aggregates,
                    "has_more": has_more,
                    "next_hint": next_hint,
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in aggregate_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Aggregation failed: {sanitized_msg}") from e

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
                _validate_method_call(model, method)

                # No-op under full YOLO; placeholder if the gate ever loosens.
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Calling {model}.{method}(...)")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                args_list = self._parse_execute_kw_arguments(arguments)
                kwargs_dict = self._parse_execute_kw_kwargs(keyword_arguments)

                # The first positional argument is conventionally the recordset
                # ids, but a business method may legitimately pass 0/negatives
                # there — only the signed-32-bit XML-RPC marshalling bound
                # ([-2**31, 2**31-1]) is enforced, and the walk covers EVERY
                # positional argument and keyword_arguments value (recursing
                # into nested lists/dicts) so an out-of-range int anywhere
                # fails cleanly instead of raising OverflowError mid-marshal.
                _check_xmlrpc_int_bounds(args_list, "arguments")
                _check_xmlrpc_int_bounds(kwargs_dict, "keyword_arguments")

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

                result_value = _json_safe(rpc_result)
                message = f"Successfully called {model}.{method}"
                if isinstance(result_value, list) and len(result_value) > MAX_METHOD_RESULT_ITEMS:
                    total = len(result_value)
                    result_value = result_value[:MAX_METHOD_RESULT_ITEMS]
                    message += f" (result truncated to {MAX_METHOD_RESULT_ITEMS} of {total} items)"

                return {
                    "success": True,
                    "result": result_value,
                    "message": message,
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
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
