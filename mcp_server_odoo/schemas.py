"""Pydantic models for structured tool output.

These models define the response schemas for MCP tools, enabling
automatic JSON schema generation and output validation by MCP clients.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# --- Search Records ---


class SearchResult(BaseModel):
    """Result of a record search operation."""

    records: List[Dict[str, Any]] = Field(description="List of matching records")
    total: int = Field(description="Total number of records matching the domain")
    limit: int = Field(description="Maximum records returned per page")
    offset: int = Field(description="Number of records skipped")
    model: str = Field(description="Odoo model name that was searched")


# --- Get Record ---


class FieldSelectionMetadata(BaseModel):
    """Metadata about which fields were returned and why."""

    fields_returned: int = Field(description="Number of fields in the response")
    field_selection_method: str = Field(
        description="How fields were selected (smart_defaults, explicit, all)"
    )
    total_fields_available: Optional[int] = Field(
        default=None, description="Total fields on the model"
    )
    note: Optional[str] = Field(
        default=None,
        description="Guidance on how to request more fields",
    )


class RecordResult(BaseModel):
    """Result of retrieving a single record by ID."""

    record: Dict[str, Any] = Field(description="Record data with requested fields")
    metadata: Optional[FieldSelectionMetadata] = Field(
        default=None,
        description="Field selection metadata (present when using smart defaults)",
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
        default=None, description="Allowed operations (standard mode only)"
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
    total: Optional[int] = Field(default=None, description="Total number of models")
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
    total_models: int = Field(description="Total number of enabled models")
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


# --- Get Fields ---


class FieldsResult(BaseModel):
    """Result of retrieving field definitions via the get_fields tool."""

    model: str = Field(description="Odoo model name the fields belong to")
    fields: Dict[str, Dict[str, Any]] = Field(
        description="Field definitions keyed by field name (fields_get output)"
    )
    total: int = Field(description="Number of fields returned")
    user_scoped: bool = Field(
        description=(
            "True when the definitions were computed for a specific user — "
            "fields protected by groups= that user lacks are absent"
        )
    )


# --- Get Defaults ---


class DefaultsResult(BaseModel):
    """Result of retrieving default field values via the get_defaults tool."""

    model: str = Field(description="Odoo model name")
    defaults: Dict[str, Any] = Field(
        description="Default values keyed by field name (default_get output)"
    )
    user_scoped: bool = Field(
        description="True when defaults were computed for a specific user"
    )


# --- Check Access ---


class AccessCheckResult(BaseModel):
    """Result of checking a user's access rights on a model."""

    model: str = Field(description="Odoo model name that was checked")
    operation: str = Field(description="Operation checked: read, write, create or unlink")
    allowed: bool = Field(description="Whether the operation is permitted")
    user_scoped: bool = Field(
        description="True when checked for a specific user (pinned or user_id)"
    )


# --- Call Method ---


class PostMessageResult(BaseModel):
    """Result of posting a chatter message via the post_message tool."""

    success: bool = Field(description="Whether the message was posted")
    model: str = Field(description="Model of the thread the message was posted on")
    record_id: int = Field(description="ID of the record the message was posted on")
    message_id: int = Field(description="ID of the created mail.message")
    url: Optional[str] = Field(default=None, description="Web URL of the record")
    message: str = Field(description="Human-readable confirmation message")


class CallMethodResult(BaseModel):
    """Result of calling an allowlisted model method via the call_method tool."""

    success: bool = Field(description="Whether the method call succeeded")
    model: str = Field(description="Model the method was called on")
    method: str = Field(description="Method name that was called")
    result: Any = Field(default=None, description="Raw return value from the Odoo method")
    message: str = Field(description="Human-readable confirmation message")


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
    user_scoped: bool = Field(
        default=False,
        description="True when the aggregation ran as a specific (pinned or given) user",
    )
