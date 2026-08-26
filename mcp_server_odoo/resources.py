"""MCP resource handlers for Odoo data access.

This module implements MCP resources for accessing Odoo data through
standardized URIs using FastMCP decorators.

Binary/attachment reads buffer the full content into a single MCP response,
so ``resources/read`` on ``odoo://{model}/record/{id}/{field}`` or
``odoo://attachment/{id}`` is capped at ``ODOO_MCP_MAX_BINARY_SIZE`` bytes
(default 50 MB). Decoding the payload and re-encoding it to base64 for the
wire peaks around 2.3x the stored size, and populated binaries are now
advertised as fetchable URIs that a client will follow, so an oversized
attachment is refused with a clean error instead of being buffered.
"""

import asyncio
import base64
import binascii
import codecs
import json
import re
import xmlrpc.client
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
from urllib.parse import unquote

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.types import Annotations
from pydantic import AnyUrl

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
    ErrorContext,
    MCPPermissionError,
    NotFoundError,
    ValidationError,
)
from .error_sanitizer import ErrorSanitizer
from .field_security import is_sensitive_field_name, strip_sensitive_fields, withheld_note
from .formatters import DatasetFormatter, RecordFormatter
from .logging_config import get_logger, perf_logger
from .odoo_connection import (
    XMLRPC_MAX_INT,
    OdooConnection,
    OdooConnectionError,
    OdooValidationFault,
)
from .uri_schema import (
    ATTACHMENT_URI_PATTERN,
    BINARY_FIELD_TYPES,
    BINARY_FIELD_URI_PATTERN,
)

logger = get_logger(__name__)

# Mimetypes that carry textual payloads — returned inline as ``text`` in a
# ``resources/read`` content entry instead of a base64 ``blob``. Everything
# else (images, audio, PDFs, archives, ...) is returned as a blob. Types
# ending in ``+json``/``+xml`` (application/ld+json, image/svg+xml, ...) are
# matched by suffix in ``_is_text_mimetype`` and need no entry here.
_TEXT_MIMETYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/ecmascript",
        "application/csv",
        "application/yaml",
        "application/x-yaml",
        "application/x-sh",
        "application/sql",
        "application/graphql",
        "text/uri-list",
    }
)

# Magic-number prefixes for common binary formats — a client-side stand-in
# for Odoo's guess_mimetype, used only when no backing ir.attachment carries
# an explicit mimetype.
_MAGIC_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
)


# SVG is XML, so it has no magic number: the root <svg> element may be
# preceded by a UTF-8 BOM, an <?xml?> declaration, a DOCTYPE or comments.
# It earns this extra check because Odoo renders every default user and
# partner avatar as SVG, making it the most common binary field served over
# MCP — without it those all degrade to an opaque octet-stream download.
_SVG_SNIFF_WINDOW = 1024
_SVG_PROLOGUE_PREFIXES = (b"<?xml", b"<!doctype svg", b"<!--")


def _looks_like_svg(raw: bytes) -> bool:
    """Whether `raw` opens an SVG document.

    Only the head of the payload is scanned, so a large XML file cannot turn
    this into a full-buffer search. The first tag must be `<svg` itself or a
    prologue that legitimately precedes it, and any HTML marker (`<html` or
    `<!doctype html`) in the head disqualifies it — a leading comment would
    otherwise defer `<!doctype html` past the prefix check, so an inline
    `<svg>` in a web page is not mistaken for one.
    """
    head = raw[:_SVG_SNIFF_WINDOW]
    if head.startswith(codecs.BOM_UTF8):
        head = head[len(codecs.BOM_UTF8) :]
    head = head.lstrip().lower()
    if head.startswith(b"<svg"):
        return True
    if head.startswith(_SVG_PROLOGUE_PREFIXES):
        return b"<svg" in head and b"<html" not in head and b"<!doctype html" not in head
    return False


def _guess_mimetype(raw: bytes) -> str:
    """Best-effort mimetype from magic bytes; octet-stream when unknown."""
    for signature, mimetype in _MAGIC_SIGNATURES:
        if raw.startswith(signature):
            return mimetype
    if _looks_like_svg(raw):
        return "image/svg+xml"
    return "application/octet-stream"


def _withheld_fields_line(count: int) -> str:
    """Visible trailer for formatted text when a bulk read withheld fields."""
    # withheld_note ends in "request explicitly by name to include"; the
    # resource surface has no fields parameter, so that trailing advice is
    # REPLACED with a pointer to the tools that do carry one, not appended
    # to it — appending said "by name" twice in one sentence.
    base = withheld_note(count).removesuffix(" — request explicitly by name to include")
    return f"\n[{base} — request them by name via the get_record/search_records tools]"


def _is_text_mimetype(mimetype: str) -> bool:
    """Whether ``mimetype`` denotes textual (inline-able) content."""
    base = (mimetype or "").split(";", 1)[0].strip().lower()
    if not base:
        return False
    if base.startswith("text/"):
        return True
    if base in _TEXT_MIMETYPES:
        return True
    return base.endswith("+json") or base.endswith("+xml")


def _parse_and_validate_id(raw: str, label: str) -> int:
    """Parse and bounds-check a record/attachment id from a URI segment.

    Shared by the binary-field, attachment and record-retrieval handlers so
    the parse + positivity + ``XMLRPC_MAX_INT`` ceiling live in one place.
    Raises a clean ``ValueError`` (message reused as the ``{e}`` tail) that each
    caller wraps in its own ``Invalid {label} '{raw}': ...`` ValidationError.

    Rejects strings ``int()`` would otherwise silently coerce — underscores
    (``"1_000"`` → 1000), hex (``"0x10"``), surrounding whitespace (``" 5 "``),
    Unicode digits (``"١٢"``) — so the decorated fallback path agrees with the
    strict URI regex used by the low-level ``resources/read`` override. The
    ASCII-only fullmatch (single optional leading hyphen) also keeps
    ``"--5"`` on the clean message instead of leaking ``int()``'s
    ``invalid literal`` text.
    """
    if not re.fullmatch(r"-?[0-9]+", raw):
        raise ValueError("must be a plain integer")
    # Length guard BEFORE int(): CPython caps str→int conversion at 4300
    # digits and raises a ValueError whose internal wording ("Exceeds the
    # limit ...") must not escape to clients. 19 digits covers 2**63;
    # anything longer is invalid regardless.
    if len(raw.lstrip("-")) > 19:
        raise ValueError(f"{label} exceeds the maximum allowed value")
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    if value > XMLRPC_MAX_INT:
        raise ValueError(f"{label} exceeds the maximum allowed value")
    return value


class OdooResourceHandler:
    """Handles MCP resource requests for Odoo data."""

    def __init__(
        self,
        app: FastMCP,
        connection: OdooConnection,
        access_controller: AccessController,
        config: OdooConfig,
    ):
        """Initialize resource handler.

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

        # Register resources
        self._register_resources()

    async def _ctx_info(self, ctx, message: str):
        """Send info to MCP client context if available."""
        if ctx:
            try:
                await ctx.info(message)
            except Exception:
                logger.debug(f"Failed to send ctx info: {message}")

    def _register_resources(self):
        """Register all resource handlers with FastMCP."""
        # Resources with parameters (like {model}) are registered as templates,
        # not concrete resources, so they won't show in list_resources().

        self._register_concrete_resources()

        # Register record retrieval resource handler
        @self.app.resource(
            "odoo://{model}/record/{record_id}",
            title="Odoo Record",
            description="Retrieve a specific record from an Odoo model by ID",
            annotations=Annotations(audience=["assistant"], priority=0.5),
        )
        async def get_record(model: str, record_id: str, ctx: Optional[Context] = None) -> str:
            """Retrieve a specific record from Odoo.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to retrieve

            Returns:
                Formatted record data as text
            """
            return await self._handle_record_retrieval(model, record_id, ctx)

        # Register search resource (no parameters due to FastMCP limitations)
        @self.app.resource(
            "odoo://{model}/search",
            title="Odoo Search",
            description="Search records with default settings (first 10 records)",
            annotations=Annotations(audience=["assistant"], priority=0.5),
        )
        async def search_records(model: str, ctx: Optional[Context] = None) -> str:
            """Search records with default settings.

            Returns the first 10 records with only the fields the one-line
            summary renders. For field selection, use the search_records
            tool instead.
            """
            await self._ctx_info(ctx, f"Searching {model} (default: first 10 records)...")
            return await self._handle_search(model, None, None, None, None, None)

        # No browse resource: FastMCP URI templates cannot carry query parameters —
        # use the search resource or search_records tool.

        # Register count resource (no parameters due to FastMCP limitations)
        @self.app.resource(
            "odoo://{model}/count",
            title="Odoo Record Count",
            description="Count all records in an Odoo model",
            annotations=Annotations(audience=["assistant"], priority=0.3),
        )
        async def count_records(model: str, ctx: Optional[Context] = None) -> str:
            """Count all records in the model.

            For filtered counts, use the aggregate_records tool with a
            domain (groupby may be omitted — the default __count aggregate
            returns the total), or the 'total' field of a search_records
            result.
            """
            await self._ctx_info(ctx, f"Counting {model} records...")
            return await self._handle_count(model, None)

        # Register fields resource
        @self.app.resource(
            "odoo://{model}/fields",
            title="Odoo Field Definitions",
            description="Get field definitions and metadata for an Odoo model",
            annotations=Annotations(audience=["assistant"], priority=0.4),
        )
        async def get_fields(model: str, ctx: Optional[Context] = None) -> str:
            """Get field definitions for a model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')

            Returns:
                Formatted field definitions and metadata
            """
            await self._ctx_info(ctx, f"Getting field definitions for {model}...")
            return await self._handle_fields(model)

        # Register binary-field resource. This template is advertised via
        # resources/templates/list; actual reads are served by the low-level
        # override below (dynamic mimeType). The function body is the
        # fallback path (static octet-stream mimeType) for callers that
        # bypass the override (e.g. ctx.read_resource).
        @self.app.resource(
            "odoo://{model}/record/{record_id}/{field}",
            title="Odoo Record Binary Field",
            description=(
                "Fetch a binary/image field from an Odoo record (e.g. an image "
                "or stored document) instead of inlining base64"
            ),
            mime_type="application/octet-stream",
            annotations=Annotations(audience=["assistant"], priority=0.3),
        )
        async def get_binary_field(
            model: str, record_id: str, field: str, ctx: Optional[Context] = None
        ) -> Union[bytes, str]:
            """Fetch a record's binary/image field as raw bytes (or text)."""
            content, _mimetype = await self._handle_binary_field_read(model, record_id, field, ctx)
            return content

        # Register attachment resource (same advertisement/fallback split).
        @self.app.resource(
            "odoo://attachment/{attachment_id}",
            title="Odoo Attachment",
            description="Fetch an ir.attachment by ID",
            mime_type="application/octet-stream",
            annotations=Annotations(audience=["assistant"], priority=0.3),
        )
        async def get_attachment(
            attachment_id: str, ctx: Optional[Context] = None
        ) -> Union[bytes, str]:
            """Fetch an ir.attachment's content as raw bytes (or text)."""
            content, _mimetype = await self._handle_attachment_read(attachment_id, ctx)
            return content

        # Serve per-read dynamic mimeTypes for the two binary schemes.
        self._install_binary_read_override()

    def _register_concrete_resources(self):
        """Register concrete resources for enabled models.

        Note: In the current FastMCP implementation, resources with parameters
        are registered as templates and won't show in list_resources().
        This is expected behavior - use list_resource_templates() to see them.
        """
        # The template resources registered with decorators are sufficient
        # FastMCP will handle them properly as templates
        pass

    def _install_binary_read_override(self):
        """Install a low-level ``resources/read`` handler for the binary schemes.

        SDK investigation (mcp 1.27, 2026-07-14):

        * FastMCP fixes a template's ``mimeType`` at registration time —
          ``FastMCP.read_resource`` always returns ``resource.mime_type`` and
          ``FunctionResource`` JSON-serializes any non-str/bytes return, so a
          decorated function cannot vary the mimeType per read. The chosen
          approach is (b): re-register the low-level ``ReadResourceRequest``
          handler (``app._mcp_server.read_resource()`` — private attr, no
          public hook in mcp 1.27) with a dispatcher that serves the two
          binary URI schemes itself, returning ``ReadResourceContents`` with
          the per-read mimeType, and delegates every other URI to
          ``FastMCP.read_resource`` unchanged.
        * Template precedence is safe: FastMCP matches ``{param}`` as
          ``[^/]+`` (no slash), so the 3-segment ``odoo://{model}/record/{id}``
          template can never capture ``record_id="5/image_128"`` — the 3- and
          4-segment templates match disjoint URI sets.
        * Repeated installs are safe: the low-level decorator REPLACES
          ``request_handlers[ReadResourceRequest]`` (plain dict assignment)
          and the dispatcher delegates through ``FastMCP.read_resource`` —
          never the previously installed handler — so a duplicate install
          can replace the dispatcher but never chain onto or recurse into
          it. The owner sentinel below skips a re-install by the SAME
          handler; a DIFFERENT handler (fresh registration on a reused app)
          intentionally replaces the dispatcher so reads go through the
          live connection.
        """
        low_level = getattr(self.app, "_mcp_server", None)
        if low_level is None:
            # Only mocked FastMCP apps (unit tests) lack _mcp_server; if this
            # ever fired in production, binary reads would degrade to the
            # decorated template functions with a static octet-stream
            # mimeType — warn so the degradation is not silent.
            logger.warning("Low-level server unavailable; dynamic binary mimeTypes not installed")
            return
        if getattr(low_level, "_odoo_binary_override_owner", None) is self:
            return

        @low_level.read_resource()
        async def read_resource_dispatch(uri: AnyUrl) -> Iterable[ReadResourceContents]:
            uri_str = str(uri)
            # ids pass as the RAW matched strings so the handlers stay the
            # single validation site shared with the decorated
            # (str-parameter) fallbacks — int()-ing here (as the uri_schema
            # parse helpers do) would let CPython's huge-digit conversion
            # ValueError ("Exceeds the limit ...") escape unsanitized.
            binary_match = BINARY_FIELD_URI_PATTERN.match(uri_str)
            if binary_match:
                model, record_id, field = binary_match.groups()
                content, mimetype = await self._handle_binary_field_read(model, record_id, field)
                return [ReadResourceContents(content=content, mime_type=mimetype)]
            attachment_match = ATTACHMENT_URI_PATTERN.match(uri_str)
            if attachment_match:
                content, mimetype = await self._handle_attachment_read(attachment_match.group(1))
                return [ReadResourceContents(content=content, mime_type=mimetype)]
            return await self.app.read_resource(uri)

        low_level._odoo_binary_override_owner = self

    @staticmethod
    def _decode_binary_value(value: Any) -> bytes:
        """Decode an XML-RPC binary field value to raw bytes.

        Odoo returns binary fields as base64 strings; some transports wrap
        them in ``xmlrpc.client.Binary`` instead.
        """
        if isinstance(value, xmlrpc.client.Binary):
            return value.data
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            try:
                return base64.b64decode(value)
            except (binascii.Error, ValueError) as e:
                raise ValidationError(f"Could not decode binary value: {e}") from e
        raise ValidationError(f"Unexpected binary value type: {type(value).__name__}")

    def _record_field_mimetype(self, model: str, record_id: int, field: str, raw: bytes) -> str:
        """Best-effort mimetype for a record binary field.

        Prefer the backing ``ir.attachment`` (for attachment-stored fields
        such as ``image_1920``); otherwise sniff the bytes. The explicit
        ``res_field`` condition disables the ORM's default res_field
        filtering. The attachment lookup may itself be denied (e.g. standard
        mode without ir.attachment enabled) — fall back to sniffing then.
        """
        try:
            self.access_controller.validate_model_access("ir.attachment", "read")
            rows = self.connection.search_read(
                "ir.attachment",
                [
                    ["res_model", "=", model],
                    ["res_id", "=", record_id],
                    ["res_field", "=", field],
                ],
                ["mimetype"],
                limit=1,
            )
            if rows and rows[0].get("mimetype"):
                return rows[0]["mimetype"]
        except Exception as e:
            logger.debug(f"Attachment mimetype lookup failed for {model}/{record_id}/{field}: {e}")
        return _guess_mimetype(raw)

    @staticmethod
    def _text_or_blob(raw: bytes, mimetype: str) -> Tuple[Union[bytes, str], str]:
        """Split content by mimetype: textual → str, everything else → bytes.

        Textual payloads are decoded with the mimetype's declared ``charset``
        parameter (default UTF-8). If the charset is unknown or the bytes do
        not decode, the payload is served as a blob instead — preserving the
        bytes beats lossy replacement.
        """
        if _is_text_mimetype(mimetype):
            charset = "utf-8"
            for param in mimetype.split(";")[1:]:
                key, _, value = param.partition("=")
                if key.strip().lower() == "charset" and value.strip():
                    charset = value.strip().strip("'\"")
                    break
            try:
                return raw.decode(charset), mimetype
            except (LookupError, UnicodeDecodeError):
                return raw, mimetype
        return raw, mimetype

    # Odoo's human_size(): "%0.2f %s" over base-1024 units. A bin_size read
    # returns this instead of the payload, which is what makes a pre-flight
    # size check possible without pulling the bytes.
    _SIZE_UNITS = {"bytes": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
    _SIZE_PLACEHOLDER_RE = re.compile(
        r"^\s*(\d+(?:[.,]\d+)?)\s*(bytes|kb|mb|gb|tb)\s*$", re.IGNORECASE
    )

    @classmethod
    def _parse_size_placeholder(cls, value: Any) -> Optional[int]:
        """Bytes behind a ``bin_size`` read's placeholder (e.g. "12.50 Kb").

        Returns None when the value is not a placeholder we recognize — the
        caller then falls back to the post-fetch check rather than guessing.
        """
        if not isinstance(value, str):
            return None
        match = cls._SIZE_PLACEHOLDER_RE.match(value)
        if not match:
            return None
        magnitude = float(match.group(1).replace(",", "."))
        return int(magnitude * cls._SIZE_UNITS[match.group(2).lower()])

    @staticmethod
    def _payload_size_bytes(value: Any) -> Optional[int]:
        """Decoded size of an already-fetched payload.

        Mirrors ``_decode_binary_value``'s type handling so the accounting
        matches what the decode will actually produce: ``Binary`` and raw
        ``bytes`` are their own length, a ``str`` is base64 (3 bytes per 4).
        """
        if isinstance(value, xmlrpc.client.Binary):
            return len(value.data)
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, str):
            return (len(value) * 3) // 4
        return None

    @staticmethod
    def _format_bytes(size: int) -> str:
        """Human-readable size that stays meaningful below 1 MB."""
        value = float(size)
        for unit in ("bytes", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{size} bytes"  # pragma: no cover - loop always returns

    def _enforce_binary_limit(self, size: Optional[int], label: str) -> None:
        """Refuse a payload over ``ODOO_MCP_MAX_BINARY_SIZE``.

        ``size`` of None means the size could not be determined; the caller
        has a later checkpoint, so skip rather than block a legitimate read.
        """
        if size is None:
            return
        limit = self.config.max_binary_size
        if size > limit:
            raise ValidationError(
                f"{label} is {self._format_bytes(size)}, over the "
                f"{self._format_bytes(limit)} limit for a single read. Raise "
                f"ODOO_MCP_MAX_BINARY_SIZE or fetch it outside MCP."
            )

    async def _assert_attachment_model_allowed(
        self, res_model: Optional[Any], attachment_id: Any, context: ErrorContext
    ) -> None:
        """Gate an attachment on the model it is attached to.

        Checking only ``ir.attachment`` would let the enabled-model allowlist
        be sidestepped wholesale: enable that one model and every attachment
        body on the database becomes readable, including documents hanging off
        models deliberately left out. Odoo's own ACLs still apply underneath;
        this is the MCP layer's gate. A standalone attachment (no
        ``res_model``) has no second model to check.
        """
        if not res_model or res_model == "ir.attachment":
            return
        try:
            await asyncio.to_thread(self.access_controller.validate_model_access, res_model, "read")
        except AccessControlUnavailableError as e:
            raise ValidationError(
                f"Could not verify access (connection error): {e}", context=context
            ) from e
        except AccessControlError as e:
            logger.warning(f"Access denied for attachment {attachment_id} on {res_model}: {e}")
            raise MCPPermissionError(
                f"Access denied: attachment {attachment_id} belongs to "
                f"'{res_model}', which is not accessible via MCP",
                context=context,
            ) from e

    async def _gate_attachment_row(self, record_id_int: int, context) -> None:
        """Apply the attached-to-model gate to an ir.attachment row by id.

        Used by every ir.attachment path that does not go through
        ``_handle_attachment_read`` (which gates on metadata it already
        fetched): the record resource, and the generic binary-field reader.
        ``datas``, ``raw`` and ``db_datas`` are all delegated to the
        attachment handler; ``thumbnail`` is a binary field on ir.attachment
        too and still reaches the generic path, which validates ir.attachment
        alone. It must not become the hole the payload gate closes — that one
        field is what keeps this helper load-bearing.
        """
        rows = await asyncio.to_thread(
            self.connection.search_read,
            "ir.attachment",
            [["id", "=", record_id_int]],
            ["res_model"],
            context={"active_test": False},
        )
        if rows:
            await self._assert_attachment_model_allowed(
                rows[0].get("res_model"), record_id_int, context
            )

    async def _handle_binary_field_read(
        self, model: str, record_id: str, field: str, ctx=None
    ) -> Tuple[Union[bytes, str], str]:
        """Read a record's binary/image field for ``resources/read``.

        Returns:
            ``(content, mimetype)`` — content is ``str`` for textual
            mimetypes (served inline as text) and ``bytes`` otherwise
            (served as a base64 blob).

        Raises:
            NotFoundError: Missing record/field, or the field holds no data
            MCPPermissionError: If access is denied
            ValidationError: For invalid inputs or connection errors
        """
        # ir.attachment.datas is the attachment resource's payload — delegate
        # so both URI forms behave identically: type='url' attachments serve
        # their URL and the stored mimetype is honored, instead of the
        # generic binary read reporting url-type attachments as "holds no
        # data".
        # raw and db_datas are the same payload as datas, just different
        # views of where it lives — and Odoo marshals `raw` WITHOUT bin_size
        # as the DECODED bytes, not base64, so the generic path would try to
        # base64-decode file content and fail. Delegating all three serves
        # the right bytes with the stored mimetype, honors type='url', and
        # applies the attached-to-model gate.
        if model == "ir.attachment" and field in ("datas", "raw", "db_datas"):
            return await self._handle_attachment_read(record_id, ctx)

        context = ErrorContext(model=model, operation="read_binary_field", record_id=record_id)
        await self._ctx_info(ctx, f"Reading binary field {model}/{record_id}/{field}...")
        logger.info(f"Reading binary field: {model}/{record_id}/{field}")

        try:
            with perf_logger.track_operation("resource_binary_field", model=model):
                try:
                    record_id_int = _parse_and_validate_id(record_id, "Record ID")
                except ValueError as e:
                    raise ValidationError(
                        f"Invalid record ID '{record_id}': {e}", context=context
                    ) from e

                try:
                    await asyncio.to_thread(
                        self.access_controller.validate_model_access, model, "read"
                    )
                except AccessControlUnavailableError as e:
                    raise ValidationError(
                        f"Could not verify access (connection error): {e}", context=context
                    ) from e
                except AccessControlError as e:
                    logger.warning(f"Access denied for {model}.read: {e}")
                    raise MCPPermissionError(access_denied_message(e), context=context) from e

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo", context=context)

                if model == "ir.attachment":
                    await self._gate_attachment_row(record_id_int, context)

                fields_info = await asyncio.to_thread(self.connection.fields_get, model)
                field_info = fields_info.get(field)
                if field_info is None:
                    raise NotFoundError(
                        f"Unknown field '{field}' on model '{model}'", context=context
                    )
                if field_info.get("type") not in BINARY_FIELD_TYPES:
                    raise ValidationError(
                        f"Field '{field}' on '{model}' is not a binary field", context=context
                    )

                # Pre-flight: bin_size returns a short size placeholder in
                # place of the payload, so an oversized field is refused
                # BEFORE its bytes are ever pulled into this process. Without
                # this the limit could only bound the decode, not the fetch
                # that precedes it — which is where an OOM actually happens.
                probe = await asyncio.to_thread(
                    self.connection.search_read,
                    model,
                    [["id", "=", record_id_int]],
                    [field],
                    context={"active_test": False, "bin_size": True},
                )
                if not probe:
                    raise NotFoundError(
                        f"Record not found: {model} with ID {record_id} does not exist",
                        context=context,
                    )
                placeholder = probe[0].get(field)
                if not placeholder:
                    raise NotFoundError(
                        f"Field '{field}' on {model}/{record_id} holds no data",
                        context=context,
                    )
                self._enforce_binary_limit(
                    self._parse_size_placeholder(placeholder),
                    f"Field '{field}' on {model}/{record_id}",
                )

                # Single search_read round trip — a missing id yields [] (a
                # plain read() would fault with MissingError instead). The
                # one path that fetches full bytes: NO bin_size context here.
                # active_test=False: search honors Odoo's active_test even
                # for an id-leaf domain, so without it archived records
                # (partners/products/employees) would 404 here while
                # get_record (plain read) serves them and advertises their
                # binary URIs.
                records = await asyncio.to_thread(
                    self.connection.search_read,
                    model,
                    [["id", "=", record_id_int]],
                    [field],
                    context={"active_test": False},
                )
                if not records:
                    raise NotFoundError(
                        f"Record not found: {model} with ID {record_id} does not exist",
                        context=context,
                    )
                value = records[0].get(field)
                if not value:
                    raise NotFoundError(
                        f"Field '{field}' on {model}/{record_id} holds no data",
                        context=context,
                    )

                # Belt and braces: the placeholder may not have parsed (a
                # custom human_size). Bounds the decode + base64 re-encode.
                self._enforce_binary_limit(
                    self._payload_size_bytes(value), f"Field '{field}' on {model}/{record_id}"
                )
                # Keep the CPU work off the loop
                raw = await asyncio.to_thread(self._decode_binary_value, value)
                mimetype = await asyncio.to_thread(
                    self._record_field_mimetype, model, record_id_int, field, raw
                )
                return await asyncio.to_thread(self._text_or_blob, raw, mimetype)

        except (NotFoundError, MCPPermissionError, ValidationError):
            raise
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            logger.error(f"Connection error reading {model}/{record_id}/{field}: {e}")
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error reading {model}/{record_id}/{field}: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to read binary field: {sanitized_msg}") from e

    async def _handle_attachment_read(
        self, attachment_id: str, ctx=None
    ) -> Tuple[Union[bytes, str], str]:
        """Read an ``ir.attachment`` for ``resources/read``.

        ``type='binary'`` attachments are served as text or blob per the
        mimetype split; ``type='url'`` attachments return their URL as text.

        Access is checked on ``ir.attachment`` AND on the attachment's
        ``res_model``, so the enabled-model allowlist cannot be bypassed by
        reaching a non-enabled model's documents through its attachments.

        Returns:
            ``(content, mimetype)`` — see ``_handle_binary_field_read``.

        Raises:
            NotFoundError: The attachment does not exist
            MCPPermissionError: If access is denied
            ValidationError: For invalid inputs or connection errors
        """
        context = ErrorContext(
            model="ir.attachment", operation="read_attachment", record_id=attachment_id
        )
        await self._ctx_info(ctx, f"Reading attachment {attachment_id}...")
        logger.info(f"Reading attachment: {attachment_id}")

        try:
            with perf_logger.track_operation("resource_attachment"):
                try:
                    attachment_id_int = _parse_and_validate_id(attachment_id, "Attachment ID")
                except ValueError as e:
                    raise ValidationError(
                        f"Invalid attachment ID '{attachment_id}': {e}", context=context
                    ) from e

                try:
                    await asyncio.to_thread(
                        self.access_controller.validate_model_access, "ir.attachment", "read"
                    )
                except AccessControlUnavailableError as e:
                    raise ValidationError(
                        f"Could not verify access (connection error): {e}", context=context
                    ) from e
                except AccessControlError as e:
                    logger.warning(f"Access denied for ir.attachment.read: {e}")
                    raise MCPPermissionError(access_denied_message(e), context=context) from e

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo", context=context)

                # Metadata first, WITHOUT datas: file_size is a stored column,
                # so the size is known before the payload is pulled into this
                # process. Fetching datas here instead would make the limit
                # check pointless — the memory is already spent by then.
                # A missing id yields [] (a plain read() would fault with
                # MissingError instead). active_test=False for symmetry with
                # the binary-field handler (id-leaf domains do NOT disable
                # active_test); ir.attachment has no `active` field today, so
                # this is harmless future-proofing.
                records = await asyncio.to_thread(
                    self.connection.search_read,
                    "ir.attachment",
                    [["id", "=", attachment_id_int]],
                    ["name", "mimetype", "type", "url", "file_size", "res_model"],
                    context={"active_test": False},
                )
                if not records:
                    raise NotFoundError(f"Attachment not found: {attachment_id}", context=context)
                attachment = records[0]

                # Gate on the attached-to model — see _assert_attachment_model_allowed.
                await self._assert_attachment_model_allowed(
                    attachment.get("res_model"), attachment_id, context
                )

                if attachment.get("type") == "url":
                    url = attachment.get("url")
                    if not url:
                        raise NotFoundError(
                            f"Attachment {attachment_id} is a URL attachment without a URL",
                            context=context,
                        )
                    return url, "text/uri-list"

                # file_size is 0/absent on some rows; that just means the
                # post-fetch checkpoint below does the work instead.
                file_size = attachment.get("file_size")
                self._enforce_binary_limit(
                    file_size if isinstance(file_size, int) and file_size > 0 else None,
                    f"Attachment {attachment_id}",
                )

                payload = await asyncio.to_thread(
                    self.connection.search_read,
                    "ir.attachment",
                    [["id", "=", attachment_id_int]],
                    ["datas"],
                    context={"active_test": False},
                )
                datas = payload[0].get("datas") if payload else None
                if not datas:
                    # Same behavior as an empty binary field: a clean error
                    # instead of serving a zero-byte blob
                    raise NotFoundError(
                        f"Attachment {attachment_id} holds no data", context=context
                    )
                # Belt and braces when file_size was unset. Bounds the decode
                # and the base64 re-encode the MCP layer performs on the way out.
                self._enforce_binary_limit(
                    self._payload_size_bytes(datas), f"Attachment {attachment_id}"
                )
                # Keep the CPU work off the loop
                raw = await asyncio.to_thread(self._decode_binary_value, datas)
                mimetype = attachment.get("mimetype") or _guess_mimetype(raw)
                return await asyncio.to_thread(self._text_or_blob, raw, mimetype)

        except (NotFoundError, MCPPermissionError, ValidationError):
            raise
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            logger.error(f"Connection error reading attachment {attachment_id}: {e}")
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error reading attachment {attachment_id}: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to read attachment: {sanitized_msg}") from e

    async def _handle_record_retrieval(self, model: str, record_id: str, ctx=None) -> str:
        """Handle record retrieval request.

        Args:
            model: The Odoo model name
            record_id: The record ID to retrieve

        Returns:
            Formatted record data

        Raises:
            NotFoundError: If record doesn't exist
            MCPPermissionError: If access is denied
            ValidationError: For invalid inputs
        """
        context = ErrorContext(model=model, operation="get_record", record_id=record_id)
        await self._ctx_info(ctx, f"Retrieving {model}/{record_id}...")

        logger.info(f"Retrieving record: {model}/{record_id}")

        try:
            with perf_logger.track_operation("resource_get_record", model=model):
                # Validate record ID
                try:
                    record_id_int = _parse_and_validate_id(record_id, "Record ID")
                except ValueError as e:
                    raise ValidationError(
                        f"Invalid record ID '{record_id}': {e}", context=context
                    ) from e

                # Check model access permissions
                try:
                    await asyncio.to_thread(
                        self.access_controller.validate_model_access, model, "read"
                    )
                except AccessControlUnavailableError as e:
                    raise ValidationError(
                        f"Could not verify access (connection error): {e}", context=context
                    ) from e
                except AccessControlError as e:
                    logger.warning(f"Access denied for {model}.read: {e}")
                    raise MCPPermissionError(access_denied_message(e), context=context) from e

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo", context=context)

                # Metadata is sensitive too — see AccessController.attachment_scope_domain.
                if model == "ir.attachment":
                    await self._gate_attachment_row(record_id_int, context)

                # Search for the record to check if it exists.
                # active_test=False: search honors Odoo's active_test even
                # for an id-leaf domain, so without it archived records
                # (partners/products/employees) would 404 here — while the
                # binary handlers and the get_record tool (plain read) serve
                # them.
                record_ids = await asyncio.to_thread(
                    self.connection.search,
                    model,
                    [("id", "=", record_id_int)],
                    context={"active_test": False},
                )

                if not record_ids:
                    raise NotFoundError(
                        f"Record not found: {model} with ID {record_id} does not exist",
                        context=context,
                    )

                # Read the record with smart field selection to avoid
                # serialization issues (safe_fields is None → all fields).
                # bin_size: any binary field that slips into the read (the
                # all-fields fallback) arrives as a short size placeholder
                # the formatter renders next to a resource URI — text
                # resources never pull full binary payloads.
                safe_fields, withheld = await asyncio.to_thread(self._get_safe_fields, model)
                records = await asyncio.to_thread(
                    self.connection.read, model, record_ids, safe_fields, {"bin_size": True}
                )

                if not records:
                    raise NotFoundError(
                        f"Record not found: {model} with ID {record_id} does not exist"
                    )

                record = records[0]
                if safe_fields is None:
                    # All-fields fallback (metadata unavailable): apply the
                    # same post-read name-based credential strip the tools
                    # use on their bulk reads.
                    withheld = strip_sensitive_fields(record)
                if withheld:
                    logger.debug(
                        f"Withheld credential-like fields on {model}/{record_id}: {withheld}"
                    )

                # Format the record data
                formatted_data = await asyncio.to_thread(self._format_record, model, record)
                if withheld:
                    formatted_data += _withheld_fields_line(len(withheld))

                logger.info(f"Successfully retrieved record: {model}/{record_id}")
                return formatted_data

        except (NotFoundError, MCPPermissionError, ValidationError):
            # Re-raise our custom exceptions
            raise
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            logger.error(f"Connection error retrieving {model}/{record_id}: {e}")
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error retrieving {model}/{record_id}: {e}")
            raise ValidationError(
                f"Failed to retrieve record: {ErrorSanitizer.sanitize_message(str(e))}"
            ) from e

    async def _handle_search(
        self,
        model: str,
        domain: Optional[str],
        fields: Optional[str],
        limit: Optional[int],
        offset: Optional[int],
        order: Optional[str],
    ) -> str:
        """Handle search request with domain filtering.

        Args:
            model: The Odoo model name
            domain: URL-encoded domain filter
            fields: Comma-separated list of fields
            limit: Maximum records to return
            offset: Pagination offset
            order: Sort order

        Returns:
            Formatted search results with pagination

        Raises:
            MCPPermissionError: If access is denied
            ValidationError: For other errors
        """
        logger.info(f"Searching {model} with domain={domain}, limit={limit}, offset={offset}")

        try:
            # Check model access permissions
            try:
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
            except AccessControlUnavailableError as e:
                raise ValidationError(f"Could not verify access (connection error): {e}") from e
            except AccessControlError as e:
                logger.warning(f"Access denied for {model}.read: {e}")
                raise MCPPermissionError(access_denied_message(e)) from e

            # Ensure we're connected
            if not self.connection.is_authenticated:
                raise ValidationError("Not authenticated with Odoo")

            # Parse parameters. The caller's own domain is kept separately:
            # the scope below is an internal injection and must not be echoed
            # into "Search criteria" or the pagination hint, where it would
            # enumerate the whole enabled-model allowlist on every page and
            # invite the caller to pass back a domain the server re-applies.
            requested_domain = self._parse_domain(domain)
            parsed_domain = requested_domain
            if model == "ir.attachment":
                # Metadata is sensitive too — see AccessController.attachment_scope_domain.
                scope = await asyncio.to_thread(
                    attachment_scope_domain, self.config, self.access_controller
                )
                if scope:
                    parsed_domain = list(requested_domain) + scope
            fields_list = self._parse_fields(fields)
            limit_value = self._parse_limit(limit)
            offset_value = self._parse_offset(offset, limit_value)
            order_value = self._parse_order(order)

            # Perform search
            record_ids = await asyncio.to_thread(
                self.connection.search,
                model,
                parsed_domain,
                limit=limit_value,
                offset=offset_value,
                order=order_value,
            )

            # Always count — see the search tool for why a short page cannot
            # be treated as the whole result set (mail.message post-filters
            # access in Python after the SQL limit).
            total_count = await asyncio.to_thread(
                self.connection.search_count, model, parsed_domain
            )

            # Read records if any found. Without an explicit field list,
            # restrict to safe fields — reading ALL fields pulls binary/html
            # payloads for every record (the single-record path has the same
            # protection). bin_size: an explicitly requested binary field
            # arrives as a short size placeholder, not the full base64 blob.
            records = []
            withheld: Set[str] = set()
            if record_ids:
                fields_to_read = fields_list
                if fields_to_read is None:
                    # A default search renders ONE line per record — the
                    # display-name summary — so read only what that summary
                    # can use.
                    fields_to_read = await asyncio.to_thread(self._summary_fields, model)
                records = await asyncio.to_thread(
                    self.connection.read, model, record_ids, fields_to_read, {"bin_size": True}
                )
                if fields_list is None and fields_to_read is None:
                    # Metadata unavailable, so ALL fields came back: apply the
                    # same post-read name-based credential strip the tools use
                    # on their bulk reads. An explicit fields= list is honored
                    # — that escape hatch is never stripped.
                    for record in records:
                        withheld.update(strip_sensitive_fields(record))
                if withheld:
                    logger.debug(
                        f"Withheld credential-like fields on {model} search: {sorted(withheld)}"
                    )

            # Get field metadata for formatting
            try:
                fields_metadata = await asyncio.to_thread(self.connection.fields_get, model)
            except Exception as e:
                logger.debug(f"Could not retrieve field metadata: {e}")
                fields_metadata = None

            # Format search results
            formatted_results = self._format_search_results(
                model,
                records,
                requested_domain,
                fields_list,
                limit_value,
                offset_value,
                total_count,
                fields_metadata,
            )
            if withheld:
                formatted_results += _withheld_fields_line(len(withheld))

            logger.info(f"Search completed: found {len(records)} of {total_count} records")
            return formatted_results

        except (MCPPermissionError, ValidationError):
            # Re-raise our custom exceptions
            raise
        except AccessControlUnavailableError as e:
            # attachment_scope_domain fails closed — an allowlist it cannot
            # read must surface as retryable, never as an unscoped result.
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise MCPPermissionError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            logger.error(f"Connection error searching {model}: {e}")
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error searching {model}: {e}")
            raise ValidationError(
                f"Failed to search records: {ErrorSanitizer.sanitize_message(str(e))}"
            ) from e

    def _parse_domain(self, domain: Optional[str]) -> List[Any]:
        """Parse domain parameter from URL-encoded string.

        Args:
            domain: URL-encoded domain string

        Returns:
            Parsed domain list; ``[]`` for absent or undecodable input

        Raises:
            ValidationError: If the domain's prefix operators are unbalanced
        """
        if not domain:
            return []

        try:
            # URL decode
            decoded = unquote(domain)
            # Parse JSON
            parsed = json.loads(decoded)

            if not isinstance(parsed, list):
                raise ValueError("Domain must be a list")

            # Same invariant the tool paths enforce: _handle_search and
            # _handle_count append attachment_scope_domain()'s prefix-notation
            # result to this, and a dangling "|" would take the scope's
            # OR-subtree as its own operand and OR the allowlist away. Raised
            # rather than swallowed like the decode errors above — a domain
            # that silently became [] would return every scoped row for a
            # request the caller believes it filtered.
            check_domain_balance(parsed)

            return parsed
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Invalid domain parameter: {domain} - {e}")
            return []

    # Fields RecordFormatter._get_record_summary can render, in its own order.
    _SUMMARY_FIELDS = ("display_name", "name", "complete_name", "partner_id", "title")

    def _summary_fields(self, model: str) -> Optional[List[str]]:
        """The fields a search-result summary line can actually use.

        Intersected with the model's real fields — Odoo rejects a read of a
        name it does not define, and only ``display_name`` is universal.
        Returns None when field metadata is unavailable, which keeps the
        existing read-everything fallback (and its credential strip).
        """
        try:
            fields_info = self.connection.fields_get(model)
            available = [name for name in self._SUMMARY_FIELDS if name in fields_info]
        except Exception as e:
            logger.debug(f"Could not get field metadata for {model}: {e}")
            return None
        return available or None

    def _get_safe_fields(self, model: str) -> Tuple[Optional[List[str]], List[str]]:
        """Field names safe to read over XML-RPC, plus the credential names withheld.

        Excludes serialized/html fields (unformattable or heavy types with
        no bin_size guard), private fields, and credential-like field names
        — the same filter the tools apply to bulk reads, so the record/
        search resources cannot surface what the tools withhold (the tools'
        explicit-fields parameter remains the escape hatch). Binary/image
        fields ARE included: both resource read call sites pass
        ``bin_size=True``, so they arrive as short size placeholders the
        formatter renders as fetchable ``odoo://`` URIs.

        Returns ``(safe_fields, withheld)``: ``withheld`` names only the
        fields excluded by the SENSITIVE check (not the html/serialized/
        private exclusions), so callers can surface the withholding.
        ``safe_fields`` is None when metadata is unavailable or no safe
        field exists — callers then fall back to reading all fields and
        strip credential-like names from the result post-read (``withheld``
        is empty in that case so the post-read strip is the single count).
        """
        # STORED binary/image types are deliberately NOT excluded: that
        # exclusion predates bin_size being wired into these reads — with
        # bin_size a stored binary is just a size placeholder, and hiding the
        # fields removed discoverability of the binary-field resources.
        # NON-STORED binaries are a different story: bin_size cannot short
        # -circuit a compute, so each one runs its Python per row (measured on
        # Odoo 19: sale.order.tax_totals makes a 10-row read 3.3x slower,
        # res.partner's six computed avatars 2.2x). They are skipped here;
        # the stored originals they derive from (image_1920 ...) stay, so the
        # binary resources remain discoverable, and any of them can still be
        # fetched by naming it explicitly or via its odoo:// URI.
        problematic_types = ("serialized", "html")
        try:
            fields_info = self.connection.fields_get(model)
            withheld = sorted(name for name in fields_info if is_sensitive_field_name(name))
            withheld_set = set(withheld)
            safe_fields = [
                field_name
                for field_name, field_info in fields_info.items()
                if field_info.get("type", "") not in problematic_types
                and not (
                    field_info.get("type", "") in BINARY_FIELD_TYPES
                    and not field_info.get("store", True)
                )
                and not field_name.startswith("_")
                and field_name not in withheld_set
            ]
        except Exception as e:
            logger.debug(f"Could not get field metadata for {model}: {e}")
            return None, []
        if not safe_fields:
            return None, []
        return safe_fields, withheld

    def _parse_fields(self, fields: Optional[str]) -> Optional[List[str]]:
        """Parse fields parameter from comma-separated string.

        Args:
            fields: Comma-separated field names

        Returns:
            List of field names or None
        """
        if not fields:
            return None

        # Split and clean field names
        field_list = [f.strip() for f in fields.split(",") if f.strip()]
        return field_list if field_list else None

    def _parse_limit(self, limit: Optional[int]) -> int:
        """Parse and validate limit parameter.

        Args:
            limit: Limit value from request

        Returns:
            Valid limit value
        """
        if limit is None:
            return self.config.default_limit

        # Ensure it's within bounds
        if limit <= 0:
            return self.config.default_limit
        elif limit > self.config.max_limit:
            return self.config.max_limit
        else:
            return limit

    def _parse_offset(self, offset: Optional[int], limit: int) -> int:
        """Parse and clamp the offset parameter.

        Belt-and-braces: the registered search template passes offset=None
        today (resource URIs cannot carry query parameters), so this guards
        future/direct callers. Unlike the tools' _validate_offset (which
        rejects), out-of-range offsets are clamped — to the same depth cap
        the tools enforce (config.MAX_OFFSET_PAGES pages of the page size,
        floored at MIN_OFFSET_CAP) and to the XML-RPC 32-bit ceiling.
        """
        if offset is None or offset < 0:
            return 0
        cap = min(max_offset_for(limit), XMLRPC_MAX_INT)
        return min(offset, cap)

    def _parse_order(self, order: Optional[str]) -> Optional[str]:
        """Parse and validate order parameter.

        Args:
            order: Order string (e.g., "name asc, id desc")

        Returns:
            Validated order string or None
        """
        if not order:
            return None

        # Basic validation - just ensure it's not empty after stripping
        cleaned = order.strip()
        return cleaned if cleaned else None

    def _format_search_results(
        self,
        model: str,
        records: List[Dict[str, Any]],
        domain: List[Any],
        fields: Optional[List[str]],
        limit: int,
        offset: int,
        total_count: int,
        fields_metadata: Optional[Dict[str, Any]],
    ) -> str:
        """Format search results with pagination metadata.

        Args:
            model: Model name
            records: List of record data
            domain: Applied domain filter
            fields: Requested fields
            limit: Records per page
            offset: Current offset
            total_count: Total matching records
            fields_metadata: Field metadata for formatting

        Returns:
            Formatted search results
        """
        # Calculate pagination info
        current_page = (offset // limit) + 1 if limit > 0 else 1
        total_pages = (total_count + limit - 1) // limit if limit > 0 else 1
        has_next = offset + limit < total_count
        has_prev = offset > 0

        # Build pagination hints. Resource URIs cannot carry query
        # parameters (FastMCP routes only the bare odoo://{model}/search
        # template), so point clients at the search_records tool instead
        # of emitting unroutable URIs.
        next_hint = None
        prev_hint = None
        domain_str = json.dumps(domain) if domain else None

        def _tool_hint(page_offset: int) -> str:
            hint = f"use the search_records tool with offset={page_offset}, limit={limit}"
            if domain_str:
                hint += f", domain={domain_str}"
            return hint

        if has_next:
            next_hint = _tool_hint(offset + limit)
        if has_prev:
            prev_hint = _tool_hint(max(0, offset - limit))

        # Use DatasetFormatter for rich formatting
        formatter = DatasetFormatter(model)
        return formatter.format_search_results(
            records=records,
            total_count=total_count,
            limit=limit,
            offset=offset,
            domain=domain,
            fields=fields,
            fields_metadata=fields_metadata,
            next_hint=next_hint,
            prev_hint=prev_hint,
            current_page=current_page,
            total_pages=total_pages,
        )

    async def _handle_count(self, model: str, domain: Optional[str]) -> str:
        """Handle count request with domain filtering.

        Args:
            model: The Odoo model name
            domain: URL-encoded domain filter

        Returns:
            Formatted count result

        Raises:
            MCPPermissionError: If access is denied
            ValidationError: For other errors
        """
        logger.info(f"Counting {model} records with domain: {domain}")

        try:
            # Check model access permissions
            try:
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
            except AccessControlUnavailableError as e:
                raise ValidationError(f"Could not verify access (connection error): {e}") from e
            except AccessControlError as e:
                logger.warning(f"Access denied for {model}.read: {e}")
                raise MCPPermissionError(access_denied_message(e)) from e

            # Ensure we're connected
            if not self.connection.is_authenticated:
                raise ValidationError("Not authenticated with Odoo")

            # Parse domain. See _handle_search: the scope is internal and
            # only the caller's own domain is echoed back.
            requested_domain = self._parse_domain(domain)
            parsed_domain = requested_domain
            if model == "ir.attachment":
                # Metadata is sensitive too — see AccessController.attachment_scope_domain.
                scope = await asyncio.to_thread(
                    attachment_scope_domain, self.config, self.access_controller
                )
                if scope:
                    parsed_domain = list(requested_domain) + scope

            # Get count
            count = await asyncio.to_thread(self.connection.search_count, model, parsed_domain)

            # Format result
            formatted_result = self._format_count_result(model, count, requested_domain)

            logger.info(f"Count completed: {count} records match criteria")
            return formatted_result

        except (MCPPermissionError, ValidationError):
            # Re-raise our custom exceptions
            raise
        except AccessControlUnavailableError as e:
            # attachment_scope_domain fails closed — an allowlist it cannot
            # read must surface as retryable, never as an unscoped result.
            raise ValidationError(f"Could not verify access (connection error): {e}") from e
        except AccessControlError as e:
            raise MCPPermissionError(access_denied_message(e)) from e
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            logger.error(f"Connection error counting {model}: {e}")
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error counting {model}: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to count records: {sanitized_msg}") from e

    async def _handle_fields(self, model: str) -> str:
        """Handle fields request for model introspection.

        Args:
            model: The Odoo model name

        Returns:
            Formatted field definitions

        Raises:
            MCPPermissionError: If access is denied
            ValidationError: For other errors
        """
        logger.info(f"Getting field definitions for {model}")

        try:
            # Check model access permissions
            try:
                await asyncio.to_thread(self.access_controller.validate_model_access, model, "read")
            except AccessControlUnavailableError as e:
                raise ValidationError(f"Could not verify access (connection error): {e}") from e
            except AccessControlError as e:
                logger.warning(f"Access denied for {model}.read: {e}")
                raise MCPPermissionError(access_denied_message(e)) from e

            # Ensure we're connected
            if not self.connection.is_authenticated:
                raise ValidationError("Not authenticated with Odoo")

            # Get field definitions
            fields = await asyncio.to_thread(self.connection.fields_get, model)

            # Format result
            formatted_result = self._format_fields_result(model, fields)

            logger.info(f"Fields retrieved: {len(fields)} fields found")
            return formatted_result

        except (MCPPermissionError, ValidationError):
            # Re-raise our custom exceptions
            raise
        except OdooValidationFault as e:
            raise ValidationError(str(e)) from e
        except OdooConnectionError as e:
            logger.error(f"Connection error getting fields for {model}: {e}")
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error getting fields for {model}: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get field definitions: {sanitized_msg}") from e

    def _format_count_result(self, model: str, count: int, domain: List[Any]) -> str:
        """Format count result.

        Args:
            model: Model name
            count: Record count
            domain: Applied domain filter

        Returns:
            Formatted count result
        """
        lines = [
            f"{'=' * 60}",
            f"Count Result: {model}",
            f"{'=' * 60}",
        ]

        if domain:
            formatter = DatasetFormatter(model)
            lines.append(f"Search criteria: {formatter._format_domain(domain)}")
        else:
            lines.append("Search criteria: All records")

        lines.append("")
        lines.append(f"Total count: {count:,} record(s)")

        return "\n".join(lines)

    def _format_fields_result(self, model: str, fields: Dict[str, Dict[str, Any]]) -> str:
        """Format field definitions result.

        Args:
            model: Model name
            fields: Field definitions dictionary

        Returns:
            Formatted field definitions
        """
        lines = [
            f"{'=' * 60}",
            f"Field Definitions: {model}",
            f"{'=' * 60}",
            f"Total fields: {len(fields)}",
            "",
        ]

        # Group fields by type
        fields_by_type = {}
        for field_name, field_info in sorted(fields.items()):
            field_type = field_info.get("type", "unknown")
            if field_type not in fields_by_type:
                fields_by_type[field_type] = []
            fields_by_type[field_type].append((field_name, field_info))

        # Format fields by type
        for field_type in sorted(fields_by_type.keys()):
            lines.append(f"\n{field_type.upper()} Fields ({len(fields_by_type[field_type])}):")
            lines.append("-" * 30)

            for field_name, field_info in fields_by_type[field_type]:
                lines.append(f"\n{field_name}:")
                lines.append(f"  Label: {field_info.get('string', 'N/A')}")
                lines.append(f"  Required: {field_info.get('required', False)}")
                lines.append(f"  Readonly: {field_info.get('readonly', False)}")

                # Add type-specific information
                if field_type == "selection":
                    selection = field_info.get("selection", [])
                    if selection and len(selection) <= 5:
                        lines.append(
                            f"  Options: {', '.join([f'{k} ({v})' for k, v in selection])}"
                        )
                    elif selection:
                        lines.append(f"  Options: {len(selection)} choices available")

                elif field_type in ("many2one", "one2many", "many2many"):
                    relation = field_info.get("relation", "N/A")
                    lines.append(f"  Related Model: {relation}")

                elif field_type in ("float", "monetary"):
                    digits = field_info.get("digits", "N/A")
                    lines.append(f"  Precision: {digits}")

                # Add help text if available
                help_text = field_info.get("help", "")
                if help_text:
                    lines.append(
                        f"  Help: {help_text[:100]}{'...' if len(help_text) > 100 else ''}"
                    )

        return "\n".join(lines)

    def _format_record(self, model: str, record: Dict[str, Any]) -> str:
        """Format a record for MCP consumption.

        Args:
            model: The model name
            record: The record data

        Returns:
            Formatted text representation
        """
        # Get field metadata if available
        try:
            fields_metadata = self.connection.fields_get(model)
        except Exception as e:
            logger.debug(f"Could not retrieve field metadata: {e}")
            fields_metadata = None

        # Use RecordFormatter for rich formatting
        formatter = RecordFormatter(model)
        return formatter.format_record(record, fields_metadata)


def register_resources(
    app: FastMCP,
    connection: OdooConnection,
    access_controller: AccessController,
    config: OdooConfig,
) -> OdooResourceHandler:
    """Register all Odoo resources with the FastMCP app.

    Args:
        app: FastMCP application instance
        connection: Odoo connection instance
        access_controller: Access control instance
        config: Odoo configuration instance

    Returns:
        The resource handler instance
    """
    handler = OdooResourceHandler(app, connection, access_controller, config)
    logger.info("Registered Odoo MCP resources")
    return handler
