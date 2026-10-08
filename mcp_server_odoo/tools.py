"""MCP tool handlers for Odoo operations.

This module implements MCP tools for performing operations on Odoo data.
Tools are different from resources - they can have side effects and perform
actions like creating, updating, or deleting records.
"""

import asyncio
import base64
import binascii
import html
import json
import re
import time
import xmlrpc.client
from ast import literal_eval as _parse_python_literal
from datetime import datetime
from typing import (
    Annotated,
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BeforeValidator, WithJsonSchema

from .access_control import (
    AccessControlError,
    AccessController,
    AccessControlUnavailableError,
    access_denied_message,
    attachment_scope_domain,
    check_domain_balance,
)
from .binary_reads import read_without_binary_payloads, uses_odoo_20_binaries
from .config import OdooConfig, max_offset_for
from .error_handling import (
    MCPPermissionError,
    NotFoundError,
    ValidationError,
)
from .error_sanitizer import ErrorSanitizer
from .field_security import is_sensitive_field_name, strip_sensitive_fields, withheld_note
from .formatters import MAX_RELATED_ITEMS
from .json_values import scrub_json_fields
from .logging_config import get_logger, perf_logger
from .odoo_connection import (
    ACCESS_ERROR_FAULT_CODE,
    XMLRPC_MAX_INT,
    OdooConnection,
    OdooConnectionError,
    OdooRequestFault,
    OdooValidationFault,
)
from .resources import _is_text_mimetype
from .schemas import (
    AggregateResult,
    AttachmentListResult,
    BulkCreateResult,
    BulkUpdateResult,
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
    ReadAttachmentResult,
    RecordResult,
    RelatedSummary,
    ResourceTemplatesResult,
    SearchResult,
    UpdateResult,
    UploadAttachmentResult,
)
from .uri_schema import (
    ATTACHMENT_CONTENT_FIELDS,
    ATTACHMENT_URI_PATTERN,
    BINARY_FIELD_TYPES,
    BINARY_FIELD_URI_PATTERN,
    URIValidationError,
    build_attachment_uri,
    build_binary_uri,
    is_binary_payload_dict,
)
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


def _double_encoded_hint(parsed: Any) -> str:
    """Hint for a JSON string that decoded to another string: it was encoded twice."""
    if isinstance(parsed, str):
        return " (the value was JSON-encoded twice; send the list or object itself)"
    return ""


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

# get_fields without field_names returns the top value fields plus the
# structure fields, and cuts long selection lists (res.partner's tz has ~500
# values)
MAX_SCHEMA_FIELDS = 60
SELECTION_OPTIONS_CAP = 20
_STRUCTURE_FIELD_TYPES = ("one2many", "many2many", *BINARY_FIELD_TYPES, "html")

# Attributes the schema-relevance score reads; fetched for the curated view
# even when the caller did not ask for them, and removed again before returning
_SCHEMA_SCORING_ATTRIBUTES = ("type", "required", "store", "related")


def _skipped_fields_note(skipped: List[str]) -> str:
    """Note naming the fields a bulk read left out because Odoo refused them."""
    return f"Left out {len(skipped)} field(s) that you cannot read: {', '.join(skipped)}."


def _withheld_fields_note(withheld: List[str]) -> str:
    """Note explaining that credential-like fields were withheld from a bulk read.

    Wording comes from field_security.withheld_note (shared with the resource
    surface); this wrapper only adds the tools-side 'fields' parameter hint.
    """
    return f"{withheld_note(withheld)} (use the 'fields' parameter)."


MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 3 // 4 - 64 * 1024
"""Largest decoded file upload_attachment accepts. The SDK refuses HTTP request
bodies over 4 MiB (413) before any tool runs; base64 grows the payload by 4/3,
and 64 KiB is left for the JSON-RPC envelope."""

# Aggregate functions that need a specific field type. Odoo checks only the
# function name, so name:sum reaches SQL and fails there as an internal error.
# The other functions (max, min, count, array_agg, ...) take any column.
_TYPED_AGGREGATES = {
    "sum": ("integer", "float", "monetary"),
    "avg": ("integer", "float", "monetary"),
    "bool_and": ("boolean",),
    "bool_or": ("boolean",),
}

# How long the fields a bulk read had to leave out stay known: the lifetime of
# the fields_get cache (PerformanceManager.cache_fields)
UNREADABLE_FIELDS_TTL = 3600

# read_attachment caps: what goes into the model's context, and the largest
# text file fetched to fill it (beyond that only a link is returned)
READ_TEXT_MAX_CHARS = 100_000
READ_TEXT_MAX_BYTES = 1024 * 1024
READ_IMAGE_MAX_BYTES = 256 * 1024
_TEXT_TOO_LARGE = f"The text file is over {READ_TEXT_MAX_BYTES // (1024 * 1024)} MB, the limit for reading it here."
_IMAGE_TOO_LARGE = (
    f"The image is over {READ_IMAGE_MAX_BYTES // 1024} KB, the limit for returning it as an image."
)

# Documents whose text Odoo extracts into ir.attachment.index_content
_EXTRACTED_TEXT_MIMETYPES = (
    "application/pdf",
    "application/msword",
    "application/rtf",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
)

MAX_BATCH_RECORDS = 100
"""Cap on records per create_records/update_records call, to bound the blast
radius of a single bulk write under YOLO mode (no per-model MCP-side write
approval)."""


def _refuse_bool(value: Any) -> Any:
    """Refuse a boolean id. Pydantic reads True as 1 and False as 0, so
    record_id=true would silently act on record 1. Digit strings stay valid."""
    if isinstance(value, bool):
        raise ValueError("an id must be an integer, not a boolean")
    return value


RecordId = Annotated[int, BeforeValidator(_refuse_bool)]


# Domain operators that take a sub-domain as their value; Odoo 17 added them
_SUB_DOMAIN_OPERATORS = ("any", "not any")


def _uses_sub_domain_operators(domain: Any) -> bool:
    """Whether a parsed domain has an 'any' or 'not any' condition.

    The top level is enough: '|' and '&' are prefix operators, so a domain is a
    flat list, and a sub-domain can only sit inside an 'any' condition.
    """
    if not isinstance(domain, (list, tuple)):
        return False
    for term in domain:
        if isinstance(term, (list, tuple)) and len(term) == 3:
            operator = term[1]
            if isinstance(operator, str) and operator.strip().lower() in _SUB_DOMAIN_OPERATORS:
                return True
    return False


def _wrap_bare_string(value: Any) -> Any:
    """A bare string is a list of one: "partner_id" is ["partner_id"]."""
    return [value] if isinstance(value, str) else value


def _lower_case(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


# The parameters below advertise a typed schema (strict clients reject a bare
# {} or a union of types), but validate as Any: the handlers keep parsing a
# JSON or Python-literal string, and their error text names the problem.
Domain = Annotated[
    Optional[Any],
    WithJsonSchema(
        {
            "anyOf": [{"type": "array", "items": {}}, {"type": "null"}],
            "description": (
                "Odoo domain: a list of conditions [field, operator, value], joined by "
                "'&' (the default), '|' or '!'. Example: [[\"is_company\", \"=\", true]]. "
                "The 'any' and 'not any' operators, which take a sub-domain as value, need "
                "Odoo 17 or later."
            ),
        }
    ),
]
FieldNames = Annotated[
    Optional[Any],
    WithJsonSchema({"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}),
]
ArgumentList = Annotated[
    Optional[Any], WithJsonSchema({"anyOf": [{"type": "array", "items": {}}, {"type": "null"}]})
]
KeywordArguments = Annotated[
    Optional[Any],
    WithJsonSchema({"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]}),
]
StringList = Annotated[Optional[List[str]], BeforeValidator(_wrap_bare_string)]

# Context keys a tool call may set. They choose which data a call sees, not
# what a write does: default_*, tracking_disable and similar keys would change
# business behavior without showing in the tool's arguments (issue #129).
CALL_CONTEXT_KEYS = ("lang", "tz", "active_test", "allowed_company_ids")

CallContext = Annotated[
    Optional[Any],
    WithJsonSchema(
        {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "lang": {"type": "string"},
                        "tz": {"type": "string"},
                        "active_test": {"type": "boolean"},
                        "allowed_company_ids": {"type": "array", "items": {"type": "integer"}},
                    },
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
            "description": (
                "Odoo context for this call. allowed_company_ids sets the companies, and "
                "the first one is the active company: company-dependent fields such as "
                "standard_price read and write its value. lang and tz set the language "
                "and timezone. active_test=false includes archived records in searches."
            ),
        }
    ),
]


def _with_context(call_context: Optional[Dict[str, Any]], **fixed: Any) -> Dict[str, Any]:
    """The caller's context with the tool's own keys over it."""
    return {**(call_context or {}), **fixed}


def _context_kwarg(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``context=`` for a connection call, left out when there is none.

    A copy per call: execute_kw adds keys to the context it is given (lang,
    allowed_company_ids), and the next call of the tool must not see them.
    """
    return {"context": dict(context)} if context else {}


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
        app: MCPServer,
        connection: OdooConnection,
        access_controller: AccessController,
        config: OdooConfig,
    ):
        """Initialize tool handler.

        Args:
            app: MCPServer application instance
            connection: Odoo connection instance
            access_controller: Access control instance
            config: Odoo configuration instance
        """
        self.app = app
        self.connection = connection
        self.access_controller = access_controller
        self.config = config
        # (model, requested field names, companies) -> (time found, names Odoo refused)
        self._unreadable_fields: Dict[
            Tuple[str, Tuple[str, ...], Tuple[int, ...]], Tuple[float, List[str]]
        ] = {}
        # Codes of the active languages, read on Odoo 16 and 17 (see _check_lang)
        self._active_langs: Optional[Set[str]] = None

        # Register tools
        self._register_tools()
        self._install_argument_check()

    def _check_lang(self, lang: str) -> None:
        """Refuse a context lang that Odoo has not installed (blocking).

        Odoo 18 and later refuse it themselves. Odoo 16 and 17 ignore it, so a
        write would land in the default language and replace that value. The
        active codes are cached and read again once for an unknown code. When
        Odoo refuses the read of res.lang (standard mode without it), the
        check is left out.
        """
        major = self.connection.get_major_version()
        if not isinstance(major, int) or major >= 18:
            return
        for attempt in (0, 1):
            if self._active_langs is None or attempt:
                try:
                    rows = self.connection.search_read(
                        "res.lang", [["active", "=", True]], ["code"]
                    )
                except OdooValidationFault as e:
                    logger.debug(f"Could not read the active languages: {e}")
                    return
                self._active_langs = {row["code"] for row in rows}
            if lang in self._active_langs:
                return
        raise ValidationError(
            f"Language '{lang}' is not installed in Odoo. Installed: "
            f"{', '.join(sorted(self._active_langs or []))}."
        )

    def _call_context(self, context: Any) -> Optional[Dict[str, Any]]:
        """Validate a tool's ``context`` argument (blocking: it may read the
        user's companies and the active languages). Returns None when there is none."""
        if context is None or context == {}:
            return None
        if not isinstance(context, dict):
            raise ValidationError('context must be an object, for example {"lang": "de_DE"}.')
        unknown = sorted(set(context) - set(CALL_CONTEXT_KEYS))
        if unknown:
            hint = (
                " Use allowed_company_ids: its first ID is the active company."
                if "company_id" in unknown
                else ""
            )
            raise ValidationError(
                f"Unknown context key(s): {', '.join(unknown)}. "
                f"Allowed keys: {', '.join(CALL_CONTEXT_KEYS)}.{hint}"
            )
        for key in ("lang", "tz"):
            if key in context and (not isinstance(context[key], str) or not context[key].strip()):
                raise ValidationError(f"context.{key} must be a non-empty string.")
        if "active_test" in context and not isinstance(context["active_test"], bool):
            raise ValidationError("context.active_test must be true or false.")
        if "lang" in context:
            self._check_lang(context["lang"])
        if "allowed_company_ids" in context:
            companies = context["allowed_company_ids"]
            if (
                not isinstance(companies, list)
                or not companies
                or any(isinstance(c, bool) or not isinstance(c, int) or c < 1 for c in companies)
            ):
                raise ValidationError(
                    "context.allowed_company_ids must be a non-empty list of company IDs."
                )
            if self.config.allowed_companies:
                limit, source = self.config.allowed_companies, "ODOO_ALLOWED_COMPANIES"
            else:
                limit, source = self.connection.user_company_ids(), "the user's companies"
            outside = [c for c in companies if limit is not None and c not in limit]
            if outside:
                raise ValidationError(
                    f"Companies {outside} are outside {source} {sorted(limit or [])}."
                )
        return dict(context)

    def _check_domain_operators(self, domain: List[Any]) -> None:
        """Refuse 'any' and 'not any' before Odoo 17 (the version is cached).

        Odoo 16 has no such operator and fails with "unhashable type: 'list'",
        which reads as a connection error. A dotted path does the same there.
        """
        major = self.connection.get_major_version()
        if isinstance(major, int) and major < 17 and _uses_sub_domain_operators(domain):
            raise ValidationError(
                f"The 'any' and 'not any' operators need Odoo 17 or later; this server runs "
                f"Odoo {major}. Use a dotted path instead, for example "
                '[["child_ids.email", "!=", false]].'
            )

    def _install_argument_check(self) -> None:
        """Refuse tool arguments the tool does not take, by name.

        The SDK (mcp 2.2) drops unknown arguments without a word, so a
        misspelled parameter (limt, filter) silently runs with the defaults.
        tools/call goes through the app's public call_tool(); a wrapper on the
        app instance checks the names against the tool's input schema first.
        """
        app = self.app
        call_tool = app.call_tool

        async def checked_call_tool(name, arguments, context=None):
            tool = app._tool_manager.get_tool(name)
            if tool is not None and arguments:
                known = list(tool.parameters.get("properties", {}))
                unknown = sorted(set(arguments) - set(known))
                if unknown:
                    raise ValidationError(
                        f"Unknown argument(s) for {name}: {', '.join(unknown)}. "
                        f"Valid arguments: {', '.join(known)}."
                    )
            return await call_tool(name, arguments, context)

        app.call_tool = checked_call_tool  # ty: ignore[invalid-assignment]

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
            "price",
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

    def _schema_default_fields(self, fields_info: Dict[str, Dict[str, Any]]) -> List[str]:
        """Fields for the get_fields default view.

        The top MAX_SCHEMA_FIELDS value fields by read importance, every
        structure field (x2many, binary, html), and the essential fields. The
        read score drops structure fields because their values are heavy, but
        in a schema they are the model's shape (order_line, invoice_line_ids).
        Ranked against value fields they fall out on large models, so they
        skip the cap. The technical and credential-name exclusions still apply,
        scored under a value type. A deliberate divergence from the reference
        in-process implementation, which ranks them inside the cap.
        """
        scored = []
        structure = []
        for name, info in fields_info.items():
            if info.get("type") in _STRUCTURE_FIELD_TYPES:
                if self._score_field_importance(name, dict(info, type="char")) > 0:
                    structure.append(name)
                continue
            score = self._score_field_importance(name, info)
            if score > 0:
                scored.append((name, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        selected = [name for name, _ in scored[:MAX_SCHEMA_FIELDS]] + structure
        for name in ("id", "name", "display_name", "active"):
            if name in fields_info and name not in selected:
                selected.append(name)
        return selected

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

            # Ensure essential fields are always included, and the parent of a
            # hierarchy: without it the children of a record cannot be told apart
            essential_fields = ["id", "name", "display_name", "active"]
            for field in [*essential_fields, "parent_id"]:
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

    def _read_bulk(
        self,
        model: str,
        ids: List[int],
        fields: Optional[List[str]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """read_without_binary_payloads for a bulk field selection (smart
        defaults or every field), leaving out the fields Odoo refuses (blocking).

        A computed field can read a model the user cannot access (the
        accounting totals on a contact), and then the whole read fails with an
        AccessError. The fields that fail are left out, and their names are
        returned. They are cached per model, field list and companies for
        UNREADABLE_FIELDS_TTL; the server runs as one uid, so the key needs no
        user. An explicit field list does not come here: it keeps the error.
        ``context`` is the caller's context, used by every read here.
        """
        companies = tuple(
            (context or {}).get("allowed_company_ids") or self.config.allowed_companies or ()
        )
        key = (model, tuple(fields) if fields is not None else (), companies)
        found_at, skipped = self._unreadable_fields.get(key, (0.0, []))
        if time.monotonic() - found_at > UNREADABLE_FIELDS_TTL:
            skipped = []

        def every_name() -> List[str]:
            return fields if fields is not None else list(self.connection.fields_get(model))

        try:
            to_read = [name for name in every_name() if name not in skipped] if skipped else fields
            return (
                read_without_binary_payloads(self.connection, model, ids, to_read, context),
                skipped,
            )
        except OdooValidationFault as e:
            if e.fault_code != ACCESS_ERROR_FAULT_CODE or not ids:
                raise
            error = e

        # A refusal of the record itself (a record rule, a company the user
        # does not have) fails a read of only "id" too: no field to leave out
        probe_context = _with_context(context, bin_size=True)
        try:
            self.connection.read(model, [ids[0]], ["id"], probe_context)
        except OdooValidationFault as e:
            if e.fault_code == ACCESS_ERROR_FAULT_CODE:
                raise error from None
            raise

        names = every_name()
        # Binaries stay out of the probe: on Odoo 20 a read returns their content
        binary_names = self._binary_field_names(model)
        probe_names = [name for name in names if name not in binary_names]
        skipped = self._find_unreadable_fields(model, ids[0], probe_names, probe_context)
        readable = [name for name in names if name not in skipped]
        if not skipped or not readable:
            # Not a field the user cannot read: the record itself, for example
            raise error
        self._unreadable_fields[key] = (time.monotonic(), skipped)
        logger.info(f"Leaving out fields of {model} that the user cannot read: {skipped}")
        return (
            read_without_binary_payloads(self.connection, model, ids, readable, context),
            skipped,
        )

    def _find_unreadable_fields(
        self,
        model: str,
        record_id: int,
        names: List[str],
        context: Dict[str, Any],
    ) -> List[str]:
        """The names whose read of one record fails with an AccessError (blocking).

        Halves the list on each failure, so k refused fields among n cost about
        2·k·log2(n) reads instead of n. One record is enough: the failure comes
        from model access, not from the record.
        """
        if not names:
            return []
        try:
            self.connection.read(model, [record_id], names, context)
            return []
        except OdooValidationFault as e:
            if e.fault_code != ACCESS_ERROR_FAULT_CODE:
                raise
        if len(names) == 1:
            return list(names)
        middle = len(names) // 2
        return self._find_unreadable_fields(
            model, record_id, names[:middle], context
        ) + self._find_unreadable_fields(model, record_id, names[middle:], context)

    def _check_write_fields(self, model: str, names: Iterable[str]) -> None:
        """Refuse unknown field names before a write (blocking).

        Odoo 19 and later answer an unknown field in write() with a bare
        KeyError. fields_get also leaves out the fields the user's groups
        cannot see, so the message names both causes. Without field metadata
        the write goes to Odoo unchecked.
        """
        try:
            known = self.connection.fields_get(model)
        except Exception as e:
            logger.debug(f"Could not get field metadata for {model}; write unchecked: {e}")
            return
        if not isinstance(known, dict) or not known:
            return
        unknown = sorted({name for name in names if name not in known})
        if unknown:
            raise ValidationError(
                f"Invalid field {', '.join(repr(name) for name in unknown)} on {model}: "
                "it does not exist, or this user cannot see it"
            )

    def _check_aggregate_types(self, model: str, aggregates: List[str]) -> None:
        """Refuse an aggregate whose function does not fit the field type (blocking).

        Unknown fields and functions are left to Odoo, which refuses them with
        a clear message.
        """
        typed = [
            spec
            for spec in aggregates
            if isinstance(spec, str) and spec.partition(":")[2] in _TYPED_AGGREGATES
        ]
        if not typed:
            return
        fields_info = self.connection.fields_get(model)
        for spec in typed:
            name, _, function = spec.partition(":")
            field_type = (fields_info.get(name) or {}).get("type")
            allowed = _TYPED_AGGREGATES[function]
            if field_type is not None and field_type not in allowed:
                raise ValidationError(
                    f"Aggregate '{spec}' needs a field of type {', '.join(allowed)}. "
                    f"'{name}' is a {field_type} field."
                )

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
        ``False``. An ``ir.attachment`` content field (``datas``; ``raw`` and
        ``db_datas`` on Odoo 20, which removed ``datas``) gets the
        attachment-specific ``odoo://attachment/{id}`` URI so its stored
        mimetype and ``type='url'`` handling apply on read.

        Only keys already present in ``record`` are touched — a caller that
        requested ``fields=['name', 'type']`` must never gain an unrequested
        ``datas`` key. A ``type='url'`` attachment stores its payload as a
        URL, so its content field is ``False``; that falsy value is still
        swapped, but only when the record carries the ``type`` key and
        ``type == 'url'`` — the attachment resource serves the URL as
        ``text/uri-list``. Empty binary attachments (``type='binary'``,
        ``datas=False``) correctly stay ``False``; when ``type`` was not read,
        the url-vs-empty split is unknowable, so a falsy content field is left
        as-is.
        """
        rid = record_id if record_id is not None else record.get("id")
        if not isinstance(rid, int) or rid <= 0:
            return
        url_attachment = (
            model == "ir.attachment" and "type" in record and record.get("type") == "url"
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
            # The one dict that IS a payload is Odoo 20's {content, size}
            # shape, which a server without bin_size returns for every
            # populated binary.
            is_payload = (isinstance(value, str) and value) or is_binary_payload_dict(value)
            if not is_payload and not (name in ATTACHMENT_CONTENT_FIELDS and url_attachment):
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
        self, model: str, record: Dict[str, Any], context: Optional[Dict[str, Any]] = None
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
                related = self.connection.read(
                    relation, ids, ["display_name"], **_context_kwarg(context)
                )
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
            raise ValidationError(
                f"Domain must be a list, got {type(parsed).__name__}{_double_encoded_hint(parsed)}"
            )
        _check_xmlrpc_int_bounds(parsed, "domain")
        check_domain_balance(parsed)

        logger.debug(f"Parsed domain from string: {parsed}")
        return parsed

    async def _ctx_info(self, ctx, message: str):
        """Log a step message on the server.

        Under mcp 1.x this also reached the client as a log notification.
        mcp 2.x deprecates client logging (SEP-2577), so the step messages
        stay in the server log. ``ctx`` is kept for the call sites.
        """
        logger.debug(message)

    async def _ctx_warning(self, ctx, message: str):
        """Log a cautionary step message on the server (see ``_ctx_info``)."""
        logger.info(message)

    def _register_tools(self):
        """Register all tool handlers with the MCPServer."""

        @self.app.tool(
            title="Search Records",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def search_records(
            model: str,
            domain: Domain = None,
            fields: FieldNames = None,
            limit: Optional[int] = None,
            offset: int = 0,
            order: Optional[str] = None,
            context: CallContext = None,
            ctx: Optional[Context] = None,
        ) -> SearchResult:
            """Search for records in an Odoo model. This is the main read tool.

            To read many known records, pass [["id", "in", ids]] as the domain in
            one call, not one get_record call per ID. Prefer one call with a
            larger limit over many small pages. For counts and per-group totals,
            use aggregate_records.

            Binary fields (images, files) come back as odoo:// URIs; read one with
            read_attachment.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                domain: Odoo domain filter - can be:
                    - A list: [['is_company', '=', True]]
                    - None: returns all records (default)
                    The 'any' and 'not any' sub-domain operators need Odoo 17+.
                fields: Field selection options - can be:
                    - None (default): Returns smart selection of common fields
                    - A list: ["field1", "field2", ...] - Returns only specified fields
                    - An empty list []: Treated like None (smart defaults)
                    - ["__all__"]: Returns ALL fields (warning: may be slow)
                limit: Maximum number of records to return. Omit it or pass 0
                    for the server-configured default (ODOO_MCP_DEFAULT_LIMIT). Capped
                    at ODOO_MCP_MAX_LIMIT.
                offset: Number of records to skip (capped at 1000 pages of
                    `limit`, min 10000 — narrow the domain or use `order`
                    instead of paging that deep)
                order: Sort order (e.g., 'name asc')

            Returns:
                Search results with records, total count, and pagination info
            """
            result = await self._handle_search_tool(
                model, domain, fields, limit, offset, order, ctx, context=context
            )
            return SearchResult(**result)

        @self.app.tool(
            title="Get Record",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def get_record(
            model: str,
            record_id: RecordId,
            fields: Optional[List[str]] = None,
            context: CallContext = None,
            ctx: Optional[Context] = None,
        ) -> RecordResult:
            """Get a specific record by ID with smart field selection.

            This tool supports selective field retrieval to optimize performance and response size.
            By default, returns a smart selection of commonly-used fields based on the model's field metadata.
            Binary fields (images, files) come back as odoo:// URIs; read one with read_attachment.
            To read several records, use search_records with [["id", "in", ids]] in one call.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID
                fields: Field selection options:
                    - None (default): Returns smart selection of common fields
                    - ["field1", "field2", ...]: Returns only specified fields
                    - An empty list []: Treated like None (smart defaults)
                    - ["__all__"]: Returns ALL fields (warning: can be very large)

            Workflow for field discovery:
            1. To see the fields of a model, call get_fields:
               get_fields("res.partner")
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
                model, record_id, fields, ctx, context=context
            )

        @self.app.tool(
            title="Get Fields",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
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
                field_names: Restrict the result to these field names, or
                    ["__all__"] for every field on the model. Omit for the
                    60 most relevant value fields (many2one included) plus
                    every one2many, many2many, file and HTML field, with
                    selection lists cut at 20 values;
                    an empty list [] is treated like omitting it.
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
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
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
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def list_models(ctx: Optional[Context] = None) -> ModelsResult:
            """List all models enabled for MCP access with their allowed operations.

            In YOLO mode every model with records is listed, and the allowed
            operations, the same for every model, are in yolo_mode.operations.

            Returns:
                List of models with their technical names, display names,
                and allowed operations (read, write, create, unlink).
            """
            result = await self._handle_list_models_tool(ctx)
            return ModelsResult(**result)

        @self.app.tool(
            title="List Resource Templates",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
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
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        async def create_record(
            model: str,
            values: Dict[str, Any],
            context: CallContext = None,
            ctx: Optional[Context] = None,
        ) -> CreateResult:
            """Create a new record in an Odoo model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                values: Field values for the new record

            Returns:
                Created record details with ID, URL, and confirmation.
            """
            result = await self._handle_create_record_tool(model, values, ctx, context=context)
            return CreateResult(**result)

        @self.app.tool(
            title="Create Records (Bulk)",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        async def create_records(
            model: str,
            records: List[Dict[str, Any]],
            context: CallContext = None,
            ctx: Optional[Context] = None,
        ) -> BulkCreateResult:
            """Create several records of the same model in one call.

            Use this instead of calling create_record in a loop: one RPC
            round-trip and one transaction, so either every record is created
            or none is. At most 100 records per call; split larger batches.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                records: Field values of each new record (max 100)

            Returns:
                id, display_name and url of each created record, in input order.
            """
            result = await self._handle_create_records_tool(model, records, ctx, context=context)
            return BulkCreateResult(**result)

        @self.app.tool(
            title="Update Record",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def update_record(
            model: str,
            record_id: RecordId,
            values: Dict[str, Any],
            context: CallContext = None,
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
            result = await self._handle_update_record_tool(
                model, record_id, values, ctx, context=context
            )
            return UpdateResult(**result)

        @self.app.tool(
            title="Update Records (Bulk)",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def update_records(
            model: str,
            record_ids: Optional[List[RecordId]] = None,
            values: Optional[Dict[str, Any]] = None,
            updates: Optional[List[Dict[str, Any]]] = None,
            context: CallContext = None,
            ctx: Optional[Context] = None,
        ) -> BulkUpdateResult:
            """Update several existing records of the same model in one call.

            Use this instead of calling update_record in a loop — one RPC
            round-trip instead of N. Use exactly one of the two forms:

            - record_ids + values: apply the same values to every record.
            - updates: [{"id": 7, "values": {...}}, ...] — different values per
              record, written in one transaction (Odoo 19 and later; in
              standard mode only if the Odoo MCP module allows it).

            Capped at 100 distinct records per call; for larger batches, split
            into multiple update_records calls. Archived records can be updated
            (e.g. values={"active": true}).

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_ids: The record IDs to update (max 100), with values
                values: Field values to apply to every record in record_ids
                updates: Per-record values, each {"id": <int>, "values": {...}}

            Returns:
                Updated record details (id, display_name) for every record,
                with confirmation.
            """
            if updates is not None:
                if record_ids is not None or values is not None:
                    raise ValidationError("Use either record_ids with values, or updates, not both")
                result = await self._handle_update_records_each_tool(
                    model, updates, ctx, context=context
                )
            else:
                if record_ids is None or values is None:
                    raise ValidationError("Provide record_ids with values, or updates")
                result = await self._handle_update_records_tool(
                    model, record_ids, values, ctx, context=context
                )
            return BulkUpdateResult(**result)

        @self.app.tool(
            title="Delete Record",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
                open_world_hint=False,
            ),
        )
        async def delete_record(
            model: str,
            record_id: RecordId,
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
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        async def post_message(
            model: str,
            record_id: RecordId,
            body: str,
            subtype: Annotated[Literal["note", "comment"], BeforeValidator(_lower_case)] = "note",
            message_type: Annotated[
                Literal["comment", "notification"], BeforeValidator(_lower_case)
            ] = "comment",
            partner_ids: Optional[List[int]] = None,
            attachment_ids: Optional[List[int]] = None,
            body_is_html: bool = False,
            subject: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> PostMessageResult:
            """Post a message to an Odoo record's chatter (mail.thread).

            ``subtype="note"`` (default) is an internal log; ``subtype="comment"``
            notifies followers. Set ``body_is_html=True`` for HTML markup;
            otherwise the body is plain text and its markup is escaped.

            Args:
                model: Odoo model name (e.g., 'res.partner')
                record_id: Record ID to post to
                body: Message body (plain text by default; HTML if body_is_html=True)
                subtype: 'note' (internal, default) or 'comment' (notifies followers)
                message_type: 'comment' (default) or 'notification'
                partner_ids: Optional list of res.partner IDs to additionally notify
                attachment_ids: Optional list of existing ir.attachment IDs to link
                body_is_html: Treat body as HTML rather than plain text
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
            title="Read Attachment",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def read_attachment(
            uri: Optional[str] = None,
            attachment_id: Optional[RecordId] = None,
            ctx: Optional[Context] = None,
        ) -> Annotated[CallToolResult, ReadAttachmentResult]:
            """Read a file: an attachment, or a binary field such as an image.

            Pass exactly one of uri or attachment_id. The uri is an odoo://
            URI from a tool result: odoo://attachment/{id} or
            odoo://{model}/record/{id}/{field}. What comes back depends on
            the file:

            - text files up to 1 MB: their text, cut at 100,000 characters
            - PDF and Office files: the text Odoo extracted from them (only
              with Odoo's attachment_indexation module), else a download link
            - images up to 256 KB: the image itself
            - anything else: a download link for a person logged in to Odoo

            Args:
                uri: odoo:// URI of the file
                attachment_id: ID of an ir.attachment

            Returns:
                The file's text or image, or a download link.
            """
            return await self._handle_read_attachment_tool(uri, attachment_id, ctx)

        @self.app.tool(
            title="List Record Attachments",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def list_record_attachments(
            model: str,
            record_id: RecordId,
            ctx: Optional[Context] = None,
        ) -> AttachmentListResult:
            """List the files attached to a record, newest first.

            Each entry carries an odoo://attachment/{id} URI that serves the
            file. Files behind binary fields (e.g. image_1920) are not listed;
            get_record returns those as URIs.

            Args:
                model: The model of the record (e.g., 'res.partner')
                record_id: The record whose attachments to list

            Returns:
                id, name, mimetype, size, type, create_date and uri of each file.
            """
            result = await self._handle_list_record_attachments_tool(model, record_id, ctx)
            return AttachmentListResult(**result)

        @self.app.tool(
            title="Upload Attachment",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        async def upload_attachment(
            model: str,
            record_id: RecordId,
            name: str,
            data: str,
            mimetype: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> UploadAttachmentResult:
            """Attach a file to a record (an ir.attachment on it).

            The file must be plain base64 (no "data:..." prefix), at most about
            2.9 MB after decoding. Odoo detects the mimetype from the content
            when it is not given. To show the file in the record's chatter,
            pass the returned attachment_id to post_message(attachment_ids=...).

            Args:
                model: The model of the record (e.g., 'res.partner')
                record_id: The record to attach the file to
                name: File name, e.g. 'contract.pdf'
                data: File content, base64-encoded
                mimetype: Optional mimetype, e.g. 'application/pdf'

            Returns:
                The new attachment's id and its odoo://attachment/{id} URI.
            """
            result = await self._handle_upload_attachment_tool(
                model, record_id, name, data, mimetype, ctx
            )
            return UploadAttachmentResult(**result)

        @self.app.tool(
            title="Aggregate Records",
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        async def aggregate_records(
            model: str,
            groupby: StringList = None,
            aggregates: StringList = None,
            domain: Domain = None,
            order: Optional[str] = None,
            limit: Optional[int] = None,
            offset: int = 0,
            context: CallContext = None,
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
                domain: Odoo domain filter, a list, or None for every record.
                    The 'any' and 'not any' sub-domain operators need Odoo 17+.
                order: Sort expression over groupby keys / aggregates,
                    e.g. ``"date_order:month"`` or ``"amount_total:sum desc"``.
                    Odoo 16 cannot order by ``__count``.
                limit: Maximum number of groups. Omitted or 0, it is
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
                model, groupby, aggregates, domain, order, limit, offset, ctx, context=context
            )
            return AggregateResult(**result)

        # Two-key opt-in: invisible to the client unless both flags are set.
        if self.config.is_write_allowed and self.config.enable_method_calls:
            logger.info("call_model_method tool ENABLED (full YOLO + ODOO_MCP_ENABLE_METHOD_CALLS)")

            @self.app.tool(
                title="Call Model Method",
                annotations=ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=False,
                    open_world_hint=True,
                ),
            )
            async def call_model_method(
                model: str,
                method: str,
                arguments: ArgumentList = None,
                keyword_arguments: KeywordArguments = None,
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
                    arguments: Positional argument list for ``execute_kw``.
                        For recordset methods, the first
                        element is typically the list of ids: ``[[42]]`` runs on
                        id 42. Defaults to ``[]``. Over JSON-2 it can hold only
                        that id list; pass every other argument in
                        keyword_arguments, by parameter name.
                    keyword_arguments: Optional dict of
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
        context: Any = None,
    ) -> Dict[str, Any]:
        """Handle search tool request."""
        try:
            with perf_logger.track_operation("tool_search", model=model):
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await self._ctx_info(ctx, f"Searching {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                call_context = await asyncio.to_thread(self._call_context, context)
                parsed_domain = self._parse_domain_input(domain)
                self._check_domain_operators(parsed_domain)
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
                                f"{_double_encoded_hint(parsed_fields)}"
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
                                    f"{_double_encoded_hint(parsed_fields)}"
                                )
                        except (ValueError, SyntaxError, RecursionError) as e:
                            raise ValidationError(
                                f"Invalid fields parameter. Expected JSON array or Python list, got: {fields[:100]}..."
                            ) from e

                # Set defaults
                if limit is not None and limit < 0:
                    raise ValidationError(f"limit must be 0 or more, got {limit}")
                if not limit:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                _validate_offset(offset, limit)

                # Search for records
                # The call context goes to the search, the count and the read:
                # allowed_company_ids changes which records the rules let
                # through, so total must count the same set as the page
                record_ids = await asyncio.to_thread(
                    self.connection.search,
                    model,
                    parsed_domain,
                    limit=limit,
                    offset=offset,
                    order=order,
                    **_context_kwarg(call_context),
                )

                # Always count. Inferring "a short page holds every match"
                # is wrong for models whose _search post-filters access in
                # Python AFTER the SQL limit — mail.message does exactly that
                # for any non-superuser, so a limit=10 search returns 5 rows
                # while 85 match. That inference undercounted `total` and
                # stopped pagination early; search_count returns the true
                # accessible count (verified: 86 == len(unlimited search)).
                total_count = await asyncio.to_thread(
                    self.connection.search_count,
                    model,
                    parsed_domain,
                    **_context_kwarg(call_context),
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

                # Read records without binary payloads (see read_without_binary_payloads);
                # populated binaries are swapped for odoo:// URIs below.
                records = []
                withheld_fields: Set[str] = set()
                skipped_fields: List[str] = []
                explicit_fields = bool(parsed_fields) and parsed_fields != ["__all__"]
                if record_ids:
                    if explicit_fields:
                        # An explicit field list keeps an AccessError
                        records = await asyncio.to_thread(
                            read_without_binary_payloads,
                            self.connection,
                            model,
                            record_ids,
                            fields_to_fetch,
                            call_context,
                        )
                    else:
                        records, skipped_fields = await asyncio.to_thread(
                            self._read_bulk, model, record_ids, fields_to_fetch, call_context
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
                    await asyncio.to_thread(scrub_json_fields, self.connection, model, records)
                    # Process datetime fields in each record
                    records = await asyncio.to_thread(
                        lambda: [self._process_record_dates(record, model) for record in records]
                    )
                    # Coerce XML-RPC types (Binary, DateTime) Pydantic can't serialize
                    records = [_json_safe(record) for record in records]
                await self._ctx_info(ctx, f"Returning {len(records)} records")

                notes = []
                if withheld_fields:
                    notes.append(_withheld_fields_note(sorted(withheld_fields)))
                if skipped_fields:
                    notes.append(_skipped_fields_note(skipped_fields))
                return {
                    "records": records,
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "model": model,
                    "note": " ".join(notes) or None,
                    "skipped_fields": skipped_fields or None,
                }

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except (OdooValidationFault, OdooRequestFault) as e:
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
        context: Any = None,
    ) -> RecordResult:
        """Handle get record tool request."""
        try:
            with perf_logger.track_operation("tool_get_record", model=model):
                _validate_record_id(record_id)

                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                call_context = await asyncio.to_thread(self._call_context, context)
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

                # Read the record without binary payloads (see read_without_binary_payloads);
                # populated binaries are swapped for odoo:// URIs below.
                # An explicit field list keeps an AccessError.
                skipped_fields: List[str] = []
                if field_selection_method == "explicit":
                    records = await asyncio.to_thread(
                        read_without_binary_payloads,
                        self.connection,
                        model,
                        [record_id],
                        fields_to_fetch,
                        call_context,
                    )
                else:
                    records, skipped_fields = await asyncio.to_thread(
                        self._read_bulk, model, [record_id], fields_to_fetch, call_context
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
                await asyncio.to_thread(scrub_json_fields, self.connection, model, [record])

                # Inline preview: resolve display names for small x2many
                # collections (ids in the record stay untouched)
                related_summaries = await asyncio.to_thread(
                    self._resolve_related_summaries, model, record, call_context
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
                        note = "Limited fields returned for performance. Use fields=['__all__'] for all fields or get_fields for the available fields."
                    metadata = FieldSelectionMetadata(
                        fields_returned=len(record),
                        field_selection_method=field_selection_method,
                        total_fields_available=total_fields,
                        note=note,
                    )

                # Surface withheld credential-like fields and refused fields
                # (bulk paths only). Local name deliberately differs from the
                # module-level field_security.withheld_note import — shadowing
                # it here would hide the helper for the rest of this function.
                withheld_message = " ".join(
                    note
                    for note in (
                        _withheld_fields_note(withheld_fields) if withheld_fields else "",
                        _skipped_fields_note(skipped_fields) if skipped_fields else "",
                    )
                    if note
                )
                if withheld_message:
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
                    record=record,
                    metadata=metadata,
                    related_summaries=related_summaries,
                    skipped_fields=skipped_fields or None,
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
        except (OdooValidationFault, OdooRequestFault) as e:
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
                show_all = bool(field_names) and "__all__" in field_names
                explicit_names = list(field_names) if field_names and not show_all else None
                curated = not explicit_names and not show_all
                request_attributes = selected_attributes
                if curated:
                    request_attributes = selected_attributes + [
                        a for a in _SCHEMA_SCORING_ATTRIBUTES if a not in selected_attributes
                    ]
                # field_names go server-side as fields_get's allfields
                # filter; unknown names are silently omitted by Odoo.
                fields_metadata = await asyncio.to_thread(
                    self.connection.fields_get, model, request_attributes, explicit_names
                )

                omitted = 0
                capped = False
                if curated:
                    keep = set(self._schema_default_fields(fields_metadata))
                    omitted = len(fields_metadata) - len(keep)
                    fields_metadata = {
                        name: {k: v for k, v in meta.items() if k in selected_attributes}
                        for name, meta in fields_metadata.items()
                        if name in keep
                    }
                    for meta in fields_metadata.values():
                        selection = meta.get("selection")
                        if selection and len(selection) > SELECTION_OPTIONS_CAP:
                            meta["selection_more"] = len(selection) - SELECTION_OPTIONS_CAP
                            meta["selection"] = selection[:SELECTION_OPTIONS_CAP]
                            capped = True

                fields = [
                    FieldInfo(**{"name": name, **meta})
                    for name, meta in sorted(fields_metadata.items())
                ]
                notes = []
                if omitted:
                    notes.append(
                        f"Showing {len(fields)} of {len(fields) + omitted} "
                        'fields. Pass field_names=[...] for specific fields or ["__all__"] '
                        "for the complete schema."
                    )
                if capped:
                    notes.append(
                        f"Selection lists over {SELECTION_OPTIONS_CAP} values are cut "
                        "(see selection_more); name the field in field_names to get every value."
                    )
                # Odoo leaves out unknown names and attributes without a word
                unknown = [n for n in explicit_names or [] if n not in fields_metadata]
                if unknown:
                    notes.append(f"{model} has no field named {', '.join(unknown)}.")
                if attributes and fields_metadata:
                    absent = [
                        a for a in attributes if not any(a in m for m in fields_metadata.values())
                    ]
                    if absent:
                        notes.append(f"No field has the attribute {', '.join(absent)}.")
                return FieldsResult(
                    model=model,
                    fields=fields,
                    total=len(fields),
                    omitted=omitted or None,
                    note=" ".join(notes) or None,
                )

        except ValidationError:
            raise
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except (OdooValidationFault, OdooRequestFault) as e:
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
                data = await asyncio.to_thread(
                    get_user_context_data, self.connection, self.config.allowed_companies
                )
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
                            "&",
                            # Abstract models hold no records; Odoo 19 and later
                            # flag them, earlier versions only by name
                            ("model", "not in", ["base", "_unknown"]),
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

                        ir_model_fields = await asyncio.to_thread(
                            self.connection.fields_get, "ir.model"
                        )
                        if "abstract" in ir_model_fields:
                            domain = ["&", ("abstract", "=", False), *domain]

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
                            "warning": "All models are accessible without MCP security.",
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
                    "description": "Search records with default settings (the first ODOO_MCP_DEFAULT_LIMIT records, 25 by default)",
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
        context: Any = None,
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
                call_context = await asyncio.to_thread(self._call_context, context)

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

                record_id = await asyncio.to_thread(
                    self.connection.create, model, values, **_context_kwarg(call_context)
                )

                # display_name only — universal and cheap; get_record for more.
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                records = await asyncio.to_thread(
                    self.connection.read,
                    model,
                    [record_id],
                    essential_fields,
                    **_context_kwarg(call_context),
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
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in create_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to create record: {sanitized_msg}") from e

    async def _handle_create_records_tool(
        self,
        model: str,
        records: List[Dict[str, Any]],
        ctx=None,
        context: Any = None,
    ) -> Dict[str, Any]:
        """Handle bulk create_records tool request."""
        try:
            with perf_logger.track_operation("tool_create_records", model=model):
                if not records:
                    raise ValidationError("No records provided")
                if len(records) > MAX_BATCH_RECORDS:
                    raise ValidationError(
                        f"Too many records: {len(records)} provided, maximum "
                        f"{MAX_BATCH_RECORDS} per call"
                    )
                for index, values in enumerate(records):
                    if not isinstance(values, dict) or not values:
                        raise ValidationError(
                            f"Record {index}: provide a non-empty object of field values"
                        )
                    # Fail cleanly before any RPC — see create_record
                    _check_xmlrpc_int_bounds(values, f"records[{index}]")

                # One check for the whole batch, as update_records does
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "create"
                )
                await self._ctx_info(ctx, f"Creating {len(records)} {model} record(s)...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")
                call_context = await asyncio.to_thread(self._call_context, context)

                if model == "ir.attachment":
                    # Same gate as create_record, per planted attachment
                    for values in records:
                        await self._gate_attachment_target(
                            values.get("res_model"), "attachment would be attached to"
                        )

                record_ids = await asyncio.to_thread(
                    self.connection.create_many, model, records, **_context_kwarg(call_context)
                )

                # display_name only — universal and cheap; get_record for more.
                rows = await asyncio.to_thread(
                    self.connection.read,
                    model,
                    record_ids,
                    ["id", "display_name"],
                    **_context_kwarg(call_context),
                )
                by_id = {row["id"]: row for row in rows}
                created = []
                for record_id in record_ids:
                    row = await asyncio.to_thread(
                        self._process_record_dates,
                        by_id.get(record_id, {"id": record_id}),
                        model,
                    )
                    created.append(
                        {**row, "url": self.connection.build_record_url(model, record_id)}
                    )

                return {
                    "success": True,
                    "created_count": len(created),
                    "records": created,
                    "message": f"Successfully created {len(created)} {model} record(s)",
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
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in create_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to create records: {sanitized_msg}") from e

    async def _handle_update_record_tool(
        self,
        model: str,
        record_id: int,
        values: Dict[str, Any],
        ctx=None,
        context: Any = None,
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
                call_context = await asyncio.to_thread(self._call_context, context)

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

                # Check that the record exists. A read of only "id" cannot:
                # Odoo 19 echoes {"id": x} back for a missing x. active_test=False
                # so that an archived record can still be updated (unarchived).
                existing_count = await asyncio.to_thread(
                    self.connection.search_count,
                    model,
                    [["id", "=", record_id]],
                    context=_with_context(call_context, active_test=False),
                )
                if not existing_count:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")
                await asyncio.to_thread(self._check_write_fields, model, values)

                # Update the record
                success = await asyncio.to_thread(
                    self.connection.write,
                    model,
                    [record_id],
                    values,
                    **_context_kwarg(call_context),
                )

                # display_name only — universal and cheap; get_record for more.
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                records = await asyncio.to_thread(
                    self.connection.read,
                    model,
                    [record_id],
                    essential_fields,
                    **_context_kwarg(call_context),
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
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to update record: {sanitized_msg}") from e

    async def _handle_update_records_tool(
        self,
        model: str,
        record_ids: List[int],
        values: Dict[str, Any],
        ctx=None,
        context: Any = None,
    ) -> Dict[str, Any]:
        """Handle bulk update_records tool request."""
        try:
            with perf_logger.track_operation("tool_update_records", model=model):
                if not record_ids:
                    raise ValidationError("No record IDs provided")
                # A repeated id is one record: dedupe (order kept) before the
                # cap, so the cap and the reported count are about records
                record_ids = list(dict.fromkeys(record_ids))
                if len(record_ids) > MAX_BATCH_RECORDS:
                    raise ValidationError(
                        f"Too many records: {len(record_ids)} provided, maximum "
                        f"{MAX_BATCH_RECORDS} per call"
                    )
                for rid in record_ids:
                    _validate_record_id(rid)

                # One check for the whole batch — matches update_record's
                # per-call (not per-record) access-control semantics.
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Updating {len(record_ids)} {model} record(s)...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")
                call_context = await asyncio.to_thread(self._call_context, context)

                if not values:
                    raise ValidationError("No values provided for record update")

                _check_xmlrpc_int_bounds(values, "values")

                if model == "ir.attachment":
                    await self._gate_attachment_records(record_ids)
                    if "res_model" in values:
                        await self._gate_attachment_target(
                            values["res_model"], "attachment would be moved to"
                        )

                # Check every record exists before writing — a partial batch
                # write with no rollback signal would be worse than failing
                # up front and naming what's missing. Uses search(), not
                # read(model, ids, ["id"]): reading only the id field never
                # touches the table, so Odoo echoes it back for ids that
                # don't exist instead of raising or omitting them.
                # active_test=False: an archived record exists (and unarchiving
                # one is a common bulk update).
                existing_ids = set(
                    await asyncio.to_thread(
                        self.connection.search,
                        model,
                        [["id", "in", record_ids]],
                        context=_with_context(call_context, active_test=False),
                    )
                )
                missing_ids = [rid for rid in record_ids if rid not in existing_ids]
                if missing_ids:
                    raise NotFoundError(f"Record(s) not found: {model} with ID(s) {missing_ids}")
                await asyncio.to_thread(self._check_write_fields, model, values)

                success = await asyncio.to_thread(
                    self.connection.write, model, record_ids, values, **_context_kwarg(call_context)
                )

                essential_fields = ["id", "display_name"]
                records = await asyncio.to_thread(
                    self.connection.read,
                    model,
                    record_ids,
                    essential_fields,
                    **_context_kwarg(call_context),
                )
                if not records:
                    raise ValidationError(
                        f"Failed to read updated records: {model} with IDs {record_ids}"
                    )

                records = [
                    await asyncio.to_thread(self._process_record_dates, rec, model)
                    for rec in records
                ]

                return {
                    "success": success,
                    "updated_count": len(records),
                    "records": records,
                    "message": (f"Successfully updated {len(records)} {model} record(s)"),
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except MCPPermissionError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to update records: {sanitized_msg}") from e

    async def _handle_update_records_each_tool(
        self,
        model: str,
        updates: List[Dict[str, Any]],
        ctx=None,
        context: Any = None,
    ) -> Dict[str, Any]:
        """Handle the per-record form of update_records (``web_save_multi``)."""
        try:
            with perf_logger.track_operation("tool_update_records_each", model=model):
                if not updates:
                    raise ValidationError("No updates provided")
                if len(updates) > MAX_BATCH_RECORDS:
                    raise ValidationError(
                        f"Too many records: {len(updates)} provided, maximum "
                        f"{MAX_BATCH_RECORDS} per call"
                    )
                ids: List[int] = []
                vals_list: List[Dict[str, Any]] = []
                for index, entry in enumerate(updates):
                    record_id = entry.get("id") if isinstance(entry, dict) else None
                    entry_values = entry.get("values") if isinstance(entry, dict) else None
                    if (
                        not isinstance(record_id, int)
                        or isinstance(record_id, bool)
                        or not isinstance(entry_values, dict)
                        or not entry_values
                    ):
                        raise ValidationError(
                            f'Update {index}: provide {{"id": <record id>, "values": {{...}}}}'
                        )
                    _validate_record_id(record_id)
                    _check_xmlrpc_int_bounds(entry_values, f"updates[{index}].values")
                    if record_id in ids:
                        # Two value sets for one record: which one wins would be
                        # an accident of order, so refuse instead of merging
                        raise ValidationError(f"Record {record_id} appears more than once")
                    ids.append(record_id)
                    vals_list.append(entry_values)

                # web_save_multi exists from Odoo 19; before it there is no
                # atomic per-record write over RPC, and N separate writes can
                # fail halfway. An unknown version is tried as is.
                major = self.connection.get_major_version()
                if isinstance(major, int) and major < 19:
                    raise ValidationError(
                        f"Different values per record need Odoo 19 or later (this is "
                        f"Odoo {major}). Use update_record per record, or record_ids "
                        f"with values for shared values."
                    )

                # One check for the whole batch, as the shared-values form does
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await self._ctx_info(ctx, f"Updating {len(ids)} {model} record(s)...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")
                call_context = await asyncio.to_thread(self._call_context, context)

                if model == "ir.attachment":
                    # Both directions, as in update_record
                    await self._gate_attachment_records(ids)
                    for entry_values in vals_list:
                        if "res_model" in entry_values:
                            await self._gate_attachment_target(
                                entry_values["res_model"], "attachment would be moved to"
                            )

                # Name every missing id before writing anything; active_test=False
                # so that archived records count (see the shared-values form)
                existing_ids = set(
                    await asyncio.to_thread(
                        self.connection.search,
                        model,
                        [["id", "in", ids]],
                        context=_with_context(call_context, active_test=False),
                    )
                )
                missing_ids = [rid for rid in ids if rid not in existing_ids]
                if missing_ids:
                    raise NotFoundError(f"Record(s) not found: {model} with ID(s) {missing_ids}")
                await asyncio.to_thread(
                    self._check_write_fields, model, {k for v in vals_list for k in v}
                )

                try:
                    rows = await asyncio.to_thread(
                        self.connection.web_save_multi,
                        model,
                        ids,
                        vals_list,
                        **_context_kwarg(call_context),
                    )
                except OdooValidationFault as e:
                    if not self.config.is_yolo_enabled and "web_save_multi" in str(e):
                        raise ValidationError(
                            f"{e} The Odoo MCP module on this server does not allow "
                            "per-record values. Use update_record per record, or "
                            "record_ids with values for shared values."
                        ) from e
                    raise
                records = [
                    await asyncio.to_thread(self._process_record_dates, row, model) for row in rows
                ]

                return {
                    "success": True,
                    "updated_count": len(records),
                    "records": records,
                    "message": f"Successfully updated {len(records)} {model} record(s)",
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except MCPPermissionError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to update records: {sanitized_msg}") from e

    async def _handle_read_attachment_tool(
        self,
        uri: Optional[str],
        attachment_id: Optional[int],
        ctx=None,
    ) -> CallToolResult:
        """Handle read attachment tool request.

        Content goes through the app's own resources/read path, so the access
        gates, the ODOO_MCP_MAX_BINARY_SIZE pre-flight and the Odoo 20 binary
        handling are the resources' own. Only attachment metadata (incl. the
        extracted index_content) is read here, under the same gate.
        """
        try:
            with perf_logger.track_operation("tool_read_attachment"):
                if (uri is None) == (attachment_id is None):
                    raise ValidationError("Pass exactly one of uri or attachment_id")
                if attachment_id is not None:
                    _validate_record_id(attachment_id, "attachment ID")
                    uri = build_attachment_uri(attachment_id)
                assert uri is not None

                base_url = self.config.url.rstrip("/")
                field_match = BINARY_FIELD_URI_PATTERN.match(uri)
                attachment_match = ATTACHMENT_URI_PATTERN.match(uri)
                if attachment_match:
                    attachment_id = int(attachment_match.group(1))
                    _validate_record_id(attachment_id, "attachment ID")
                    download_url = f"{base_url}/web/content/{attachment_id}?download=true"
                    meta = await self._attachment_metadata(attachment_id)
                elif field_match:
                    model, record_id, field = field_match.groups()
                    download_url = (
                        f"{base_url}/web/content/{model}/{record_id}/{field}?download=true"
                    )
                    meta = await self._field_attachment_metadata(model, int(record_id), field)
                else:
                    raise ValidationError(
                        "Pass an odoo://attachment/{id} or odoo://{model}/record/{id}/{field} URI"
                    )
                await self._ctx_info(ctx, f"Reading {uri}...")

                result = {
                    "uri": uri,
                    "name": meta.get("name"),
                    "mimetype": meta.get("mimetype"),
                    "size": meta.get("size"),
                    "download_url": download_url,
                }
                mimetype = meta.get("mimetype") or ""
                size = meta.get("size")

                if meta.get("type") == "url":
                    return self._attachment_result(
                        {**result, "kind": "url", "text": meta.get("url") or ""}
                    )
                if mimetype.startswith(_EXTRACTED_TEXT_MIMETYPES):
                    extracted = (meta.get("index_content") or "").strip()
                    # Without attachment_indexation, Odoo 16-18 store the main
                    # type ("application") as the index, not the document text
                    if extracted and extracted != mimetype.split("/")[0]:
                        return self._attachment_text_result(result, extracted, "extracted_text")
                    return self._attachment_link_result(
                        result, "Odoo extracted no text from this file."
                    )
                if mimetype and _is_text_mimetype(mimetype):
                    if size is not None and size > READ_TEXT_MAX_BYTES:
                        return self._attachment_link_result(result, _TEXT_TOO_LARGE)
                elif mimetype.startswith("image/"):
                    if size is not None and size > READ_IMAGE_MAX_BYTES:
                        return self._attachment_link_result(result, _IMAGE_TOO_LARGE)
                elif mimetype:
                    return self._attachment_link_result(
                        result, f"{mimetype} files are returned as a link."
                    )

                # Text, an image, or a field without metadata: read the content
                contents = await self.app.read_resource(uri)
                item = list(contents)[0]
                content, mimetype = item.content, item.mime_type or mimetype
                result["mimetype"] = mimetype
                if isinstance(content, str):
                    return self._attachment_text_result(result, content, "text")
                result["size"] = len(content)
                if mimetype.startswith("image/") and len(content) <= READ_IMAGE_MAX_BYTES:
                    payload = base64.b64encode(content).decode("ascii")
                    return self._attachment_result(
                        {**result, "kind": "image"},
                        ImageContent(type="image", data=payload, mime_type=mimetype),
                    )
                return self._attachment_link_result(
                    result,
                    _IMAGE_TOO_LARGE
                    if mimetype.startswith("image/")
                    else f"{mimetype} files are returned as a link.",
                )

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except MCPPermissionError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in read_attachment tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to read attachment: {sanitized_msg}") from e

    async def _attachment_metadata(self, attachment_id: int) -> Dict[str, Any]:
        """Name, mimetype, size, type, url and extracted text of an attachment (gated)."""
        await asyncio.to_thread(
            self.access_controller.validate_model_access, "ir.attachment", "read"
        )
        if not self.connection.is_authenticated:
            raise ValidationError("Not authenticated with Odoo")
        # Gate on the attached-to model before any metadata leaves Odoo
        await self._gate_attachment_records([attachment_id])
        rows = await asyncio.to_thread(
            self.connection.search_read,
            "ir.attachment",
            [["id", "=", attachment_id]],
            ["name", "mimetype", "file_size", "type", "url", "index_content"],
            context={"active_test": False},
        )
        if not rows:
            raise NotFoundError(f"Attachment not found: {attachment_id}")
        row = rows[0]
        # Odoo answers False for empty values
        return {
            **row,
            "name": row.get("name") or None,
            "mimetype": row.get("mimetype") or None,
            "size": row.get("file_size") or None,
        }

    async def _field_attachment_metadata(
        self, model: str, record_id: int, field: str
    ) -> Dict[str, Any]:
        """Metadata of the attachment that stores a binary field, if readable.

        Empty when there is none (a plain column) or ir.attachment is not
        accessible; the content read then decides by what comes back.
        """
        try:
            await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
            await asyncio.to_thread(
                self.access_controller.validate_model_access, "ir.attachment", "read"
            )
            rows = await asyncio.to_thread(
                self.connection.search_read,
                "ir.attachment",
                [
                    ["res_model", "=", model],
                    ["res_id", "=", record_id],
                    ["res_field", "=", field],
                ],
                ["name", "mimetype", "file_size"],
                limit=1,
            )
        except AccessControlError as e:
            if isinstance(e, AccessControlUnavailableError):
                raise
            # The model itself must be readable; only ir.attachment may be off
            await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
            return {}
        if not rows:
            return {}
        row = rows[0]
        return {
            "name": row.get("name"),
            "mimetype": row.get("mimetype") or None,
            "size": row.get("file_size") or None,
        }

    def _attachment_text_result(
        self, result: Dict[str, Any], text: str, kind: str
    ) -> CallToolResult:
        truncated = len(text) > READ_TEXT_MAX_CHARS
        fields = {
            **result,
            "kind": kind,
            "text": text[:READ_TEXT_MAX_CHARS],
            "truncated": truncated,
        }
        if truncated:
            fields["note"] = (
                f"Only the first {READ_TEXT_MAX_CHARS:,} of {len(text):,} characters are "
                "returned. The download_url serves the whole file."
            )
        return self._attachment_result(fields)

    def _attachment_link_result(self, result: Dict[str, Any], why: str) -> CallToolResult:
        return self._attachment_result({**result, "kind": "link", "note": why})

    @staticmethod
    def _attachment_result(fields: Dict[str, Any], *blocks: Any) -> CallToolResult:
        """Structured result plus the content blocks the model reads."""
        structured = ReadAttachmentResult(**fields).model_dump(mode="json")
        summary = {k: v for k, v in structured.items() if v is not None and k != "text"}
        lines = [json.dumps(summary, indent=2)]
        if structured.get("text"):
            lines.append(structured["text"])
        return CallToolResult(
            content=[TextContent(type="text", text="\n\n".join(lines)), *blocks],
            structured_content=structured,
        )

    async def _handle_list_record_attachments_tool(
        self,
        model: str,
        record_id: int,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle list record attachments tool request."""
        try:
            with perf_logger.track_operation("tool_list_record_attachments", model=model):
                _validate_record_id(record_id)
                if model == "ir.attachment":
                    raise ValidationError("Pass the record the files are attached to")

                # The record's model and ir.attachment both: an attachment row
                # carries url and index_content (the extracted document text)
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, "ir.attachment", "read"
                )
                await self._ctx_info(ctx, f"Listing attachments of {model}/{record_id}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                exists = await asyncio.to_thread(
                    self.connection.search_count,
                    model,
                    [["id", "=", record_id]],
                    context={"active_test": False},
                )
                if not exists:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # res_field=False: attachments behind binary fields (images)
                # are the field values, not files attached to the record
                domain = [
                    ["res_model", "=", model],
                    ["res_id", "=", record_id],
                    ["res_field", "=", False],
                ]
                total = await asyncio.to_thread(
                    self.connection.search_count, "ir.attachment", domain
                )
                rows = await asyncio.to_thread(
                    self.connection.search_read,
                    "ir.attachment",
                    domain,
                    ["name", "mimetype", "file_size", "type", "create_date"],
                    limit=self.config.max_limit,
                    order="create_date desc, id desc",
                )
                attachments = []
                for row in rows:
                    row = await asyncio.to_thread(self._process_record_dates, row, "ir.attachment")
                    attachments.append(
                        {
                            "id": row["id"],
                            "name": row.get("name") or "",
                            "mimetype": row.get("mimetype") or None,
                            "size": row.get("file_size") or None,
                            "type": row.get("type") or "binary",
                            "create_date": row.get("create_date") or None,
                            "uri": build_attachment_uri(row["id"]),
                        }
                    )

                return {
                    "model": model,
                    "record_id": record_id,
                    "attachments": attachments,
                    "total": total,
                    "note": (
                        f"Showing the newest {len(attachments)} of {total} attachments."
                        if total > len(attachments)
                        else None
                    ),
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in list_record_attachments tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to list attachments: {sanitized_msg}") from e

    async def _handle_upload_attachment_tool(
        self,
        model: str,
        record_id: int,
        name: str,
        data: str,
        mimetype: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle upload attachment tool request."""
        try:
            with perf_logger.track_operation("tool_upload_attachment", model=model):
                _validate_record_id(record_id)
                if model == "ir.attachment":
                    raise ValidationError(
                        "Attach the file to a business record, not to another attachment"
                    )
                if not name or not name.strip():
                    raise ValidationError("Provide a file name")
                if data.startswith("data:"):
                    raise ValidationError(
                        "Send the file as plain base64, without the 'data:...;base64,' prefix"
                    )
                try:
                    raw = base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError) as e:
                    raise ValidationError(f"'data' is not valid base64: {e}") from e
                if not raw:
                    raise ValidationError("The file is empty")
                if len(raw) > MAX_UPLOAD_BYTES:
                    raise ValidationError(
                        f"The file is {len(raw):,} bytes, over the {MAX_UPLOAD_BYTES:,}-byte "
                        f"limit for one upload"
                    )

                # Write access on the record the file goes to, and create
                # access on ir.attachment (standard mode checks both models)
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, model, "write"
                )
                await asyncio.to_thread(
                    self.access_controller.validate_model_access, "ir.attachment", "create"
                )
                await self._ctx_info(ctx, f"Attaching {name} to {model}/{record_id}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # res_id is a plain integer: Odoo would attach to a record
                # that does not exist. active_test=False: archived ones do.
                exists = await asyncio.to_thread(
                    self.connection.search_count,
                    model,
                    [["id", "=", record_id]],
                    context={"active_test": False},
                )
                if not exists:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Odoo 20 removed ir.attachment.datas; raw takes the same base64 string
                content_field = "raw" if uses_odoo_20_binaries(self.connection) else "datas"
                values = {
                    "name": name,
                    "res_model": model,
                    "res_id": record_id,
                    content_field: data,
                }
                if mimetype:
                    values["mimetype"] = mimetype
                attachment_id = await asyncio.to_thread(
                    self.connection.create, "ir.attachment", values
                )

                return {
                    "success": True,
                    "attachment_id": attachment_id,
                    "uri": build_attachment_uri(attachment_id),
                    "name": name,
                    "size": len(raw),
                    "message": f"Attached {name} to {model} record {record_id}",
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlUnavailableError as e:
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise ValidationError(access_denied_message(e)) from e
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in upload_attachment tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to upload attachment: {sanitized_msg}") from e

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
        except (OdooValidationFault, OdooRequestFault) as e:
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

                # Odoo's own errors for a missing record or attachment name
                # the user id and the raw recordset; say it like the other tools
                existing_count = await asyncio.to_thread(
                    self.connection.search_count,
                    model,
                    [["id", "=", record_id]],
                    context={"active_test": False},
                )
                if not existing_count:
                    raise ValidationError(f"Record not found: {model} with ID {record_id}")
                if attachment_ids:
                    found = await asyncio.to_thread(
                        self.connection.search,
                        "ir.attachment",
                        [["id", "in", list(attachment_ids)]],
                        context={"active_test": False},
                    )
                    missing = [i for i in attachment_ids if i not in set(found)]
                    if missing:
                        raise ValidationError(
                            f"Attachment not found: {', '.join(map(str, missing))}"
                        )

                if not body_is_html:
                    major = self.connection.get_major_version()
                    if isinstance(major, int) and major < 17:
                        # Odoo 16 stores a str body as HTML; 17 and later escape it
                        body = html.escape(body)

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
        except (OdooValidationFault, OdooRequestFault) as e:
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
        context: Optional[Dict[str, Any]] = None,
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
              Odoo 16 orders by an aggregate under its bare field name, so
              ``"amount_total:sum desc"`` becomes ``"amount_total desc"``
              there (see ``_odoo16_group_order``).
        """
        # __count is implicit in read_group (lazy=False), so it is dropped
        # beside other aggregates. Alone it is sent as the only field:
        # fields=[] makes Odoo 16 fail without a groupby (it cannot marshal
        # the None it puts in the row) and aggregate every numeric field with
        # one. fields=["__count"] works on 16, 17 and 18.
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

        kwargs.update(_context_kwarg(context))
        groups = self.connection.execute_kw(model, "read_group", [domain], kwargs)
        if groups is None:
            # Odoo 16 cannot marshal the None in the total row of an empty
            # ungrouped read_group, and the connection reads that fault as a
            # void return. Odoo 17 answers the row below.
            if groupby:
                return []
            return [{"__count": 0, "__extra_domain": [], **dict.fromkeys(fields_kwarg, False)}]

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
        context: Any = None,
    ) -> Dict[str, Any]:
        """Handle aggregate_records tool request."""
        try:
            with perf_logger.track_operation("tool_aggregate_records", model=model):
                # Access check (read permission — same as search_records)
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
                await self._ctx_info(ctx, f"Aggregating {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")
                call_context = await asyncio.to_thread(self._call_context, context)

                # Omitted/empty groupby collapses to a single overall row —
                # both dispatch paths support it natively (one bucket with
                # the requested aggregates), making this the tool for
                # filtered counts via the default __count.
                groupby = list(groupby) if groupby else []

                parsed_domain = self._parse_domain_input(domain)
                self._check_domain_operators(parsed_domain)
                if model == "ir.attachment":
                    scope = await asyncio.to_thread(
                        attachment_scope_domain, self.config, self.access_controller
                    )
                    if scope:
                        # Appended, not "&"-prefixed — see the matching comment
                        # in _handle_search_tool.
                        parsed_domain = list(parsed_domain) + scope

                # Limit defaults & capping (mirror search_records)
                if limit is not None and limit < 0:
                    raise ValidationError(f"limit must be 0 or more, got {limit}")
                if not limit:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                _validate_offset(offset, limit)

                # Default to ['__count'] when caller omits aggregates —
                # otherwise formatted_read_group returns only the groupby
                # keys with no quantitative data, which defeats the tool.
                effective_aggregates = aggregates if aggregates else ["__count"]
                await asyncio.to_thread(self._check_aggregate_types, model, effective_aggregates)

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
                        call_context,
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
                    kwargs.update(_context_kwarg(call_context))
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
        except (OdooValidationFault, OdooRequestFault) as e:
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
                raise ValidationError(
                    f"arguments must be a list, got {type(parsed).__name__}"
                    f"{_double_encoded_hint(parsed)}"
                )
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
                    f"{_double_encoded_hint(parsed)}"
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
        except (OdooValidationFault, OdooRequestFault) as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in call_model_method tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to call model method: {sanitized_msg}") from e


def register_tools(
    app: MCPServer,
    connection: OdooConnection,
    access_controller: AccessController,
    config: OdooConfig,
) -> OdooToolHandler:
    """Register all Odoo tools with the MCPServer app.

    Args:
        app: MCPServer application instance
        connection: Odoo connection instance
        access_controller: Access control instance
        config: Odoo configuration instance

    Returns:
        The tool handler instance
    """
    handler = OdooToolHandler(app, connection, access_controller, config)
    logger.info("Registered Odoo MCP tools")
    return handler
