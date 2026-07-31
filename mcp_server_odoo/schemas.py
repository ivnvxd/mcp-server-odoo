"""Pydantic models for structured tool output.

These models define the response schemas for MCP tools, enabling
automatic JSON schema generation and output validation by MCP clients.
"""

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

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
    """Result of a server-side aggregation via Odoo's formatted_read_group."""

    groups: List[Dict[str, Any]] = Field(
        description=(
            "Aggregated buckets. Each entry contains the groupby keys, '__count', "
            "any requested aggregate values, and '__extra_domain' for drilldown."
        )
    )
    model: str = Field(description="Odoo model name that was aggregated")
    groupby: List[str] = Field(description="Group-by expressions that were applied")
    aggregates: List[str] = Field(description="Aggregate expressions that were applied")


# --- Call Model Method (XML-RPC execute_kw) ---


class CallModelMethodResult(BaseModel):
    """Result of invoking a public Odoo model method via XML-RPC execute_kw."""

    success: bool = Field(description="Whether Odoo executed the method without RPC fault")
    result: Any = Field(
        default=None,
        description="Return value from Odoo (type depends on the method; may be null)",
    )
    message: str = Field(description="Human-readable summary of the call")


# --- Export Records ---


class ExportConfig(BaseModel):
    """Configuration for a single export operation."""

    export_dir: Path
    max_rows: int = Field(default=10000, gt=0, le=1_000_000)
    batch_size: int = Field(default=500, gt=0, le=10_000)
    format: Literal["csv"] = "csv"
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ExportSuccessResult(BaseModel):
    """Success response for export_records."""

    success: Literal[True] = True
    file_path: str
    file_size_bytes: int
    row_count: int
    truncated: bool
    max_rows_limit: int
    preview: list[str] = Field(..., max_length=10)
    duration_ms: int
    exported_at: str  # ISO 8601 timestamp
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "file_path": "/tmp/odoo-mcp-exports/odoo_export_res_partner_20260614T153022_a1b2c3d4.csv",
                "file_size_bytes": 1248392,
                "row_count": 8421,
                "truncated": False,
                "max_rows_limit": 10000,
                "preview": [
                    "id,name,email",
                    "1,Acme Corp,info@acme.com",
                    "2,Globex Inc,contact@globex.com",
                ],
                "duration_ms": 3420,
                "exported_at": "2026-06-14T15:30:25.123Z",
            }
        }
    )


class ExportBlockedExceedsLimitError(BaseModel):
    """Error when export is blocked due to exceeding row limit."""

    success: Literal[False] = False
    error: Literal["export_blocked_exceeds_limit"] = "export_blocked_exceeds_limit"
    message: str
    matched_count: int
    max_rows_limit: int
    suggestion: str


class ExportAccessDeniedError(BaseModel):
    """Error when access is denied to the requested model."""

    success: Literal[False] = False
    error: Literal["access_denied"] = "access_denied"
    message: str


class ExportDisabledError(BaseModel):
    """Error when the export tool is disabled."""

    success: Literal[False] = False
    error: Literal["export_disabled"] = "export_disabled"
    message: str


class ExportFileError(BaseModel):
    """Error when file write operation fails."""

    success: Literal[False] = False
    error: Literal["file_write_error"] = "file_write_error"
    message: str


# Discriminated union of all export result variants
ExportResult = Union[
    ExportSuccessResult,
    ExportBlockedExceedsLimitError,
    ExportAccessDeniedError,
    ExportDisabledError,
    ExportFileError,
]
