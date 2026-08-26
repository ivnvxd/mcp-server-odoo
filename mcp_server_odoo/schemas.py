"""Pydantic models for structured tool output.

These models define the response schemas for MCP tools, enabling
automatic JSON schema generation and output validation by MCP clients.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# --- Search Records ---


class SearchResult(BaseModel):
    """Result of a record search operation."""

    records: List[Dict[str, Any]] = Field(description="List of matching records")
    total: int = Field(description="Total number of records matching the domain")
    limit: int = Field(description="Maximum records returned per page")
    offset: int = Field(description="Number of records skipped")
    model: str = Field(description="Odoo model name that was searched")
    note: Optional[str] = Field(
        default=None,
        description="Advisory note (e.g. credential-like fields withheld from an '__all__' read)",
    )


# --- Get Record ---


class FieldSelectionMetadata(BaseModel):
    """Metadata about which fields were returned and why."""

    fields_returned: int = Field(description="Number of fields in the response")
    field_selection_method: str = Field(
        description=(
            "How fields were selected (smart_defaults, explicit, all, "
            "all_fields_fallback — smart selection unavailable, all fields read)"
        )
    )
    total_fields_available: Optional[int] = Field(
        default=None, description="Total fields on the model"
    )
    note: Optional[str] = Field(
        default=None,
        description="Guidance on how to request more fields",
    )


class RelatedSummary(BaseModel):
    """Display-name preview of one record in a small x2many collection."""

    id: int = Field(description="Related record ID")
    display_name: str = Field(description="Related record display name")


class RecordResult(BaseModel):
    """Result of retrieving a single record by ID."""

    record: Dict[str, Any] = Field(description="Record data with requested fields")
    metadata: Optional[FieldSelectionMetadata] = Field(
        default=None,
        description="Field selection metadata (present when using smart defaults)",
    )
    related_summaries: Optional[Dict[str, List[RelatedSummary]]] = Field(
        default=None,
        description=(
            "Display-name previews for small x2many collections, keyed by field name. "
            "Absent for large or unreadable relations; ids in 'record' stay unchanged."
        ),
    )


# --- Get Fields ---


class FieldInfo(BaseModel):
    """Definition of a single field on an Odoo model.

    Extra attributes requested via ``get_fields(attributes=[...])``
    (e.g. ``help``, ``store``) are carried through as additional keys.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(description="Technical field name")
    type: Optional[str] = Field(
        default=None, description="Field type (char, many2one, selection, ...)"
    )
    string: Optional[str] = Field(default=None, description="Human-readable field label")
    required: Optional[bool] = Field(default=None, description="Whether the field is required")
    readonly: Optional[bool] = Field(default=None, description="Whether the field is read-only")
    relation: Optional[str] = Field(default=None, description="Target model for relational fields")
    selection: Optional[List[List[Any]]] = Field(
        default=None, description="Selection options as [value, label] pairs"
    )


class FieldsResult(BaseModel):
    """Result of describing a model's fields via get_fields."""

    model: str = Field(description="Odoo model name that was described")
    fields: List[FieldInfo] = Field(description="Field definitions, sorted by name")
    total: int = Field(description="Number of fields returned")


# --- Get Current Context ---


class CompanyInfo(BaseModel):
    """A company the connected user is allowed to act in."""

    id: int = Field(description="Company ID")
    name: str = Field(description="Company display name")


class CurrentContextResult(BaseModel):
    """Session context of the connected Odoo user.

    Structured fields are null when the user context could not be read
    (e.g. standard mode where res.users is not MCP-enabled); ``text``
    then carries the UTC datetime guidance prefixed by a note explaining
    why the personalized block is missing.
    """

    user_name: Optional[str] = Field(default=None, description="Display name of the connected user")
    login: Optional[str] = Field(default=None, description="Login of the connected user")
    timezone: Optional[str] = Field(
        default=None, description="User timezone (null when unset — treat as UTC)"
    )
    company_id: Optional[int] = Field(default=None, description="Active company ID")
    company_name: Optional[str] = Field(default=None, description="Active company name")
    allowed_companies: Optional[List[CompanyInfo]] = Field(
        default=None,
        description="Companies the user may act in (only when more than one)",
    )
    text: str = Field(
        description=(
            "Formatted context block (the same personalized block appended "
            "to the static initialize instructions)"
        )
    )


# --- List Models ---


class ModelOperations(BaseModel):
    """Allowed CRUD operations for a model."""

    read: bool = Field(description="Can read records")
    write: bool = Field(description="Can update records")
    create: bool = Field(description="Can create records")
    unlink: bool = Field(description="Can delete records")


class ModelInfo(BaseModel):
    """Information about an MCP-enabled Odoo model."""

    model: str = Field(description="Technical model name (e.g. 'res.partner')")
    name: str = Field(description="Human-readable model name")
    operations: Optional[ModelOperations] = Field(
        default=None,
        description=(
            "Allowed operations for this model. Populated per-model in standard "
            "mode; null in YOLO mode, where the flags are global and reported "
            "once under yolo_mode.operations."
        ),
    )


class YoloModeInfo(BaseModel):
    """YOLO mode status and configuration."""

    enabled: bool = Field(description="Whether YOLO mode is active")
    level: str = Field(description="YOLO level: 'read' or 'true'")
    description: str = Field(description="Human-readable mode description")
    warning: str = Field(description="Security warning message")
    operations: ModelOperations = Field(description="Global operation permissions in YOLO mode")


class ModelsResult(BaseModel):
    """Result of listing available models."""

    models: List[ModelInfo] = Field(description="List of available models")
    yolo_mode: Optional[YoloModeInfo] = Field(
        default=None, description="YOLO mode info (only present when YOLO is enabled)"
    )
    total: Optional[int] = Field(
        default=None, description="Number of models returned in this response"
    )
    total_available: Optional[int] = Field(
        default=None,
        description=(
            "Number of models available in the database. Larger than 'total' only "
            "when the listing was truncated (see 'note')."
        ),
    )
    note: Optional[str] = Field(
        default=None,
        description="Advisory note (e.g. the listing was truncated to a maximum page size)",
    )
    error: Optional[str] = Field(default=None, description="Error message if model listing failed")


# --- List Resource Templates ---


class ResourceTemplateParameter(BaseModel):
    """Parameter definition for a resource template."""

    model: str = Field(description="Odoo model name (e.g., res.partner)")
    record_id: Optional[str] = Field(default=None, description="Record ID (e.g., 10)")


class ResourceTemplateInfo(BaseModel):
    """Information about an available resource URI template."""

    uri_template: str = Field(description="URI template pattern")
    description: str = Field(description="What this resource provides")
    parameters: Dict[str, str] = Field(description="Template parameter descriptions")
    example: str = Field(description="Example URI")
    note: Optional[str] = Field(default=None, description="Additional usage notes")


class ResourceTemplatesResult(BaseModel):
    """Result of listing resource templates."""

    templates: List[ResourceTemplateInfo] = Field(description="Available resource templates")
    enabled_models: List[str] = Field(description="Sample of models usable with these templates")
    total_models: Optional[int] = Field(
        description="Total number of enabled models (None in YOLO mode: all models are available)"
    )
    note: str = Field(description="Usage guidance for resources vs tools")


# --- Create Record ---


class CreateResult(BaseModel):
    """Result of creating a new record."""

    success: bool = Field(description="Whether the record was created successfully")
    record: Dict[str, Any] = Field(description="Essential fields of the created record")
    url: str = Field(description="Direct URL to the record in Odoo web interface")
    message: str = Field(description="Human-readable success message")


# --- Update Record ---


class UpdateResult(BaseModel):
    """Result of updating an existing record."""

    success: bool = Field(description="Whether the record was updated successfully")
    record: Dict[str, Any] = Field(description="Essential fields of the updated record")
    url: str = Field(description="Direct URL to the record in Odoo web interface")
    message: str = Field(description="Human-readable success message")


# --- Delete Record ---


class DeleteResult(BaseModel):
    """Result of deleting a record."""

    success: bool = Field(description="Whether the record was deleted successfully")
    deleted_id: int = Field(description="ID of the deleted record")
    deleted_name: str = Field(description="Display name of the deleted record")
    message: str = Field(description="Human-readable success message")


# --- Post Message ---


class PostMessageResult(BaseModel):
    """Result of posting a message to a record's chatter."""

    success: bool = Field(description="Whether the message was posted successfully")
    message_id: int = Field(description="ID of the created mail.message record")


# --- Aggregate Records ---


class AggregateResult(BaseModel):
    """Result of a server-side aggregation via Odoo's grouping methods."""

    groups: List[Dict[str, Any]] = Field(
        description=(
            "Aggregated buckets. Each entry contains the groupby keys, '__count', "
            "any requested aggregate values, and '__extra_domain' for drilldown. "
            "AND '__extra_domain' with the 'domain' you passed to reproduce the "
            "group's records, e.g. search_records(domain=[*your_domain, "
            "*group['__extra_domain']]). On Odoo 19 it is only the group's own "
            "condition; on older servers it is already the full domain (your "
            "filter included) — re-ANDing is idempotent either way."
        )
    )
    model: str = Field(description="Odoo model name that was aggregated")
    groupby: List[str] = Field(description="Group-by expressions that were applied")
    aggregates: List[str] = Field(description="Aggregate expressions that were applied")
    has_more: bool = Field(
        default=False,
        description="True when more groups exist beyond this page (detected via a limit+1 peek)",
    )
    next_hint: Optional[str] = Field(
        default=None,
        description="Suggested follow-up call for the next page (set only when has_more)",
    )


# --- Call Model Method (XML-RPC execute_kw) ---


class CallModelMethodResult(BaseModel):
    """Result of invoking a public Odoo model method via XML-RPC execute_kw."""

    success: bool = Field(description="Whether Odoo executed the method without RPC fault")
    result: Any = Field(
        default=None,
        description="Return value from Odoo (type depends on the method; may be null)",
    )
    message: str = Field(description="Human-readable summary of the call")
