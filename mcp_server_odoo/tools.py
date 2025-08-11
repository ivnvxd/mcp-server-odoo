"""MCP tool handlers for Odoo operations.

This module implements MCP tools for performing operations on Odoo data.
Tools are different from resources - they can have side effects and perform
actions like creating, updating, or deleting records.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP

from .access_control import AccessControlError, AccessController
from .config import OdooConfig
from .error_handling import (
    NotFoundError,
    ValidationError,
)
from .error_sanitizer import ErrorSanitizer
from .logging_config import get_logger, perf_logger
from .odoo_connection import OdooConnection, OdooConnectionError

logger = get_logger(__name__)

# Legacy error type alias for backward compatibility
ToolError = ValidationError


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

    def _should_include_field_by_default(self, field_name: str, field_info: Dict[str, Any]) -> bool:
        """Determine if a field should be included in default response.

        Args:
            field_name: Name of the field
            field_info: Field metadata from fields_get()

        Returns:
            True if field should be included in default response
        """
        # Always include essential fields
        always_include = {"id", "name", "display_name", "active"}
        if field_name in always_include:
            return True

        # Exclude system/technical fields by prefix
        exclude_prefixes = ("_", "message_", "activity_", "website_message_")
        if field_name.startswith(exclude_prefixes):
            return False

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
            return False

        # Get field type
        field_type = field_info.get("type", "")

        # Exclude binary and large fields
        if field_type in ("binary", "image", "html"):
            return False

        # Exclude expensive computed fields (non-stored)
        if field_info.get("compute") and not field_info.get("store", True):
            return False

        # Exclude one2many and many2many fields (can be large)
        if field_type in ("one2many", "many2many"):
            return False

        # Include required fields
        if field_info.get("required"):
            return True

        # Include simple stored fields that are searchable
        if field_info.get("store", True) and field_info.get("searchable", True):
            if field_type in (
                "char",
                "text",
                "boolean",
                "integer",
                "float",
                "date",
                "datetime",
                "selection",
                "many2one",
            ):
                return True

        return False

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

    def _register_tools(self):
        """Register all tool handlers with FastMCP."""

        @self.app.tool()
        async def search_records(
            model: str,
            domain: Optional[Union[str, List[Union[str, List[Any]]]]] = None,
            fields: Optional[Union[str, List[str]]] = None,
            limit: int = 10,
            offset: int = 0,
            order: Optional[str] = None,
        ) -> Dict[str, Any]:
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

            Returns:
                Dictionary with 'records' list and 'total' count
            """
            return await self._handle_search_tool(model, domain, fields, limit, offset, order)

        @self.app.tool()
        async def get_record(
            model: str,
            record_id: int,
            fields: Optional[List[str]] = None,
        ) -> Dict[str, Any]:
            """Get a specific record by ID with smart field selection.

            This tool supports selective field retrieval to optimize performance and response size.
            By default, returns a smart selection of commonly-used fields based on the model's field metadata.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID
                fields: Field selection options:
                    - None (default): Returns smart selection of common fields
                    - ["field1", "field2", ...]: Returns only specified fields
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
                Dictionary with record data containing requested fields.
                When using smart defaults, includes _metadata with field statistics.
            """
            return await self._handle_get_record_tool(model, record_id, fields)

        @self.app.tool()
        async def list_models() -> Dict[str, List[Dict[str, Any]]]:
            """List all models enabled for MCP access with their allowed operations.

            Returns:
                Dictionary containing a list of model information dictionaries.
                Each model includes:
                - model: Technical name (e.g., 'res.partner')
                - name: Display name (e.g., 'Contact')
                - operations: Dict of allowed operations (read, write, create, unlink)

            Example response:
                {
                    "models": [
                        {
                            "model": "res.partner",
                            "name": "Contact",
                            "operations": {
                                "read": true,
                                "write": true,
                                "create": true,
                                "unlink": false
                            }
                        }
                    ]
                }
            """
            return await self._handle_list_models_tool()

        @self.app.tool()
        async def list_resource_templates() -> Dict[str, Any]:
            """List available resource URI templates.

            Since MCP resources with parameters are registered as templates,
            they don't appear in the standard resource list. This tool provides
            information about available resource patterns you can use.

            Returns:
                Dictionary with resource template information including:
                - templates: List of resource template definitions
                - examples: Example URIs for each template
                - enabled_models: List of models you can use with these templates
            """
            return await self._handle_list_resource_templates_tool()

        @self.app.tool()
        async def create_record(
            model: str,
            values: Dict[str, Any],
        ) -> Dict[str, Any]:
            """Create a new record in an Odoo model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                values: Field values for the new record

            Returns:
                Dictionary with created record details
            """
            return await self._handle_create_record_tool(model, values)

        @self.app.tool()
        async def update_record(
            model: str,
            record_id: int,
            values: Dict[str, Any],
        ) -> Dict[str, Any]:
            """Update an existing record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to update
                values: Field values to update

            Returns:
                Dictionary with updated record details
            """
            return await self._handle_update_record_tool(model, record_id, values)

        @self.app.tool()
        async def delete_record(
            model: str,
            record_id: int,
        ) -> Dict[str, Any]:
            """Delete a record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to delete

            Returns:
                Dictionary with deletion confirmation
            """
            return await self._handle_delete_record_tool(model, record_id)

    async def _handle_search_tool(
        self,
        model: str,
        domain: Optional[Union[str, List[Union[str, List[Any]]]]],
        fields: Optional[List[str]],
        limit: int,
        offset: int,
        order: Optional[str],
    ) -> Dict[str, Any]:
        """Handle search tool request."""
        try:
            with perf_logger.track_operation("tool_search", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "read")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Handle domain parameter - can be string or list
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
                                json_domain = json_domain.replace("True", "true").replace(
                                    "False", "false"
                                )
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
                if limit <= 0 or limit > self.config.max_limit:
                    limit = self.config.default_limit

                # Get total count
                total_count = self.connection.search_count(model, parsed_domain)

                # Search for records
                record_ids = self.connection.search(
                    model, parsed_domain, limit=limit, offset=offset, order=order
                )

                # Determine which fields to fetch
                fields_to_fetch = parsed_fields
                if parsed_fields is None:
                    # Use smart field selection to avoid serialization issues
                    fields_to_fetch = self._get_smart_default_fields(model)
                    logger.debug(
                        f"Using smart defaults for {model} search: {len(fields_to_fetch) if fields_to_fetch else 'all'} fields"
                    )
                elif parsed_fields == ["__all__"]:
                    # Explicit request for all fields
                    fields_to_fetch = None  # Odoo interprets None as all fields
                    logger.debug(f"Fetching all fields for {model} search")

                # Read records
                records = []
                if record_ids:
                    records = self.connection.read(model, record_ids, fields_to_fetch)
                    # Process datetime fields in each record
                    records = [self._process_record_dates(record, model) for record in records]

                return {
                    "records": records,
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "model": model,
                }

        except AccessControlError as e:
            raise ToolError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ToolError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in search_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ToolError(f"Search failed: {sanitized_msg}") from e

    async def _handle_get_record_tool(
        self,
        model: str,
        record_id: int,
        fields: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Handle get record tool request."""
        try:
            with perf_logger.track_operation("tool_get_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "read")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Determine which fields to fetch
                fields_to_fetch = fields
                use_smart_defaults = False
                total_fields = None

                if fields is None:
                    # Use smart field selection
                    fields_to_fetch = self._get_smart_default_fields(model)
                    use_smart_defaults = True
                    logger.debug(
                        f"Using smart defaults for {model}: {len(fields_to_fetch) if fields_to_fetch else 'all'} fields"
                    )
                elif fields == ["__all__"]:
                    # Explicit request for all fields
                    fields_to_fetch = None  # Odoo interprets None as all fields
                    logger.debug(f"Fetching all fields for {model}")
                else:
                    # Specific fields requested
                    logger.debug(f"Fetching specific fields for {model}: {fields}")

                # Read the record
                records = self.connection.read(model, [record_id], fields_to_fetch)

                if not records:
                    raise ToolError(f"Record not found: {model} with ID {record_id}")

                # Process datetime fields in the record
                record = self._process_record_dates(records[0], model)

                # Add metadata when using smart defaults
                if use_smart_defaults:
                    try:
                        # Get total field count for metadata
                        all_fields_info = self.connection.fields_get(model)
                        total_fields = len(all_fields_info)
                    except Exception:
                        pass  # Don't fail if we can't get field count

                    record["_metadata"] = {
                        "fields_returned": (
                            len(record) - 1 if "_metadata" in record else len(record)
                        ),
                        "field_selection_method": "smart_defaults",
                        "note": "Limited fields returned for performance. Use fields=['__all__'] for all fields or see odoo://{}/fields for available fields.".format(
                            model
                        ),
                    }
                    if total_fields:
                        record["_metadata"]["total_fields_available"] = total_fields

                return record

        except ToolError:
            # Re-raise ToolError without modification to preserve specific error messages
            raise
        except NotFoundError as e:
            raise ToolError(str(e)) from e
        except AccessControlError as e:
            raise ToolError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ToolError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ToolError(f"Failed to get record: {sanitized_msg}") from e

    async def _handle_list_models_tool(self) -> Dict[str, Any]:
        """Handle list models tool request with permissions."""
        try:
            with perf_logger.track_operation("tool_list_models"):
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

                        # Query models from database
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
                for model_info in models:
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
        except ToolError:
            # Re-raise ToolError without modification to preserve specific error messages
            raise
        except Exception as e:
            logger.error(f"Error in list_models tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ToolError(f"Failed to list models: {sanitized_msg}") from e

    async def _handle_list_resource_templates_tool(self) -> Dict[str, Any]:
        """Handle list resource templates tool request."""
        try:
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
            raise ToolError(f"Failed to list resource templates: {sanitized_msg}") from e

    async def _handle_create_record_tool(
        self,
        model: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle create record tool request."""
        try:
            with perf_logger.track_operation("tool_create_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "create")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate required fields
                if not values:
                    raise ValidationError("No values provided for record creation")

                # Create the record
                record_id = self.connection.create(model, values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                essential_fields = ["id", "name", "display_name"]

                # Read only the essential fields
                records = self.connection.read(model, [record_id], essential_fields)
                if not records:
                    raise ToolError(f"Failed to read created record: {model} with ID {record_id}")

                # Process dates in the minimal record
                record = self._process_record_dates(records[0], model)

                # Generate direct URL to the record in Odoo
                base_url = self.config.url.rstrip("/")
                record_url = f"{base_url}/web#id={record_id}&model={model}&view_type=form"

                return {
                    "success": True,
                    "record": record,
                    "url": record_url,
                    "message": f"Successfully created {model} record with ID {record_id}",
                }

        except ToolError:
            # Re-raise ToolError without modification to preserve specific error messages
            raise
        except AccessControlError as e:
            raise ToolError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ToolError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in create_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ToolError(f"Failed to create record: {sanitized_msg}") from e

    async def _handle_update_record_tool(
        self,
        model: str,
        record_id: int,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle update record tool request."""
        try:
            with perf_logger.track_operation("tool_update_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "write")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not values:
                    raise ValidationError("No values provided for record update")

                # Check if record exists (only fetch ID to verify existence)
                existing = self.connection.read(model, [record_id], ["id"])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Update the record
                success = self.connection.write(model, [record_id], values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                essential_fields = ["id", "name", "display_name"]

                # Read only the essential fields
                records = self.connection.read(model, [record_id], essential_fields)
                if not records:
                    raise ToolError(f"Failed to read updated record: {model} with ID {record_id}")

                # Process dates in the minimal record
                record = self._process_record_dates(records[0], model)

                # Generate direct URL to the record in Odoo
                base_url = self.config.url.rstrip("/")
                record_url = f"{base_url}/web#id={record_id}&model={model}&view_type=form"

                return {
                    "success": success,
                    "record": record,
                    "url": record_url,
                    "message": f"Successfully updated {model} record with ID {record_id}",
                }

        except ToolError:
            # Re-raise ToolError without modification to preserve specific error messages
            raise
        except NotFoundError as e:
            raise ToolError(str(e)) from e
        except AccessControlError as e:
            raise ToolError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ToolError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ToolError(f"Failed to update record: {sanitized_msg}") from e

    async def _handle_delete_record_tool(
        self,
        model: str,
        record_id: int,
    ) -> Dict[str, Any]:
        """Handle delete record tool request."""
        try:
            with perf_logger.track_operation("tool_delete_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "unlink")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Check if record exists
                existing = self.connection.read(model, [record_id])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Store some info about the record before deletion
                record_name = existing[0].get(
                    "name", existing[0].get("display_name", f"ID {record_id}")
                )

                # Delete the record
                success = self.connection.unlink(model, [record_id])

                return {
                    "success": success,
                    "deleted_id": record_id,
                    "deleted_name": record_name,
                    "message": f"Successfully deleted {model} record '{record_name}' (ID: {record_id})",
                }

        except ToolError:
            # Re-raise ToolError without modification to preserve specific error messages
            raise
        except NotFoundError as e:
            raise ToolError(str(e)) from e
        except AccessControlError as e:
            raise ToolError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ToolError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in delete_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ToolError(f"Failed to delete record: {sanitized_msg}") from e


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
