"""Data formatting utilities for Odoo records.

This module provides functions for formatting Odoo data into
human-readable text suitable for MCP resources.
"""

import logging
from typing import Any, Dict, List

from mcp_server_odoo.field_formatters import format_field
from mcp_server_odoo.odoo_connection import OdooConnection

logger = logging.getLogger(__name__)


def format_field_value(
    field_name: str,
    field_value: Any,
    field_type: str,
    model: str,
    odoo: OdooConnection,
    indent: int = 0,
) -> str:
    """Format a field value based on its type.

    Args:
        field_name: Name of the field
        field_value: Value of the field
        field_type: Type of the field
        model: Model name
        odoo: Odoo connection instance
        indent: Indentation level

    Returns:
        str: Formatted field value
    """
    indent_str = "  " * indent

    # Use the field formatters system
    formatted = format_field(
        field_name=field_name,
        field_value=field_value,
        field_type=field_type,
        model=model,
        odoo=odoo,
    )

    # Apply indentation
    if indent > 0:
        lines = formatted.split("\n")
        return "\n".join(f"{indent_str}{line}" for line in lines)

    return formatted


def format_record(
    model: str,
    record: Dict[str, Any],
    odoo: OdooConnection,
    include_header: bool = True,
) -> str:
    """Format a single record for display.

    Args:
        model: Model name
        record: Record data
        odoo: Odoo connection instance
        include_header: Whether to include a header line

    Returns:
        str: Formatted record
    """
    # Get field definitions
    fields_info = odoo.get_model_fields(model)

    # Start with record header
    lines = []

    if include_header:
        record_id = record.get("id", "unknown")
        lines.append(f"Resource: {model}/record/{record_id}")

    # Sort fields: put important fields first, then alphabetical
    priority_fields = ["name", "display_name", "code", "reference", "number"]

    def field_sort_key(field_name):
        # Lower value = higher priority
        for i, pf in enumerate(priority_fields):
            if field_name == pf:
                return i
        # Non-priority fields sorted alphabetically
        return len(priority_fields) + ord(field_name[0])

    field_names = sorted(record.keys(), key=field_sort_key)

    # Skip technical fields
    excluded_fields = [
        "id",
        "__last_update",
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
        "message_ids",
        "message_follower_ids",
        "activity_ids",
        "activity_state",
        "activity_user_id",
        "activity_type_id",
        "activity_date_deadline",
        "activity_summary",
    ]

    for field_name in field_names:
        if field_name in excluded_fields:
            continue

        field_value = record[field_name]
        field_info = fields_info.get(field_name, {})
        field_type = field_info.get("type", "char")

        formatted_value = format_field_value(
            field_name=field_info.get("string", field_name),
            field_value=field_value,
            field_type=field_type,
            model=model,
            odoo=odoo,
        )

        lines.append(formatted_value)

    return "\n".join(lines)


def format_search_results(
    model: str,
    records: List[Dict[str, Any]],
    total_count: int,
    limit: int,
    offset: int,
    domain: List,
    odoo: OdooConnection,
) -> str:
    """Format search results for display.

    Args:
        model: Model name
        records: List of records
        total_count: Total count of matching records
        limit: Limit used for the search
        offset: Offset used for the search
        domain: Search domain
        odoo: Odoo connection instance

    Returns:
        str: Formatted search results
    """
    lines = []

    # Header information
    model_display = model.replace(".", " ").title()
    lines.append(f"Search Results: {model} ({total_count} total matches)")

    # Pagination info
    if total_count > 0:
        from_record = offset + 1
        to_record = min(offset + limit, total_count)
        lines.append(f"Showing: Records {from_record}-{to_record} of {total_count}")

        # Add summary information if we have a large number of records
        if total_count > 20:
            # Try to get some basic model information
            model_fields = odoo.get_model_fields(model)

            # Add model-specific summaries based on available fields
            # This is just a basic implementation - can be enhanced with more model-specific logic
            type_field = None
            for field_name in ["type", "state", "status", "category_id"]:
                if field_name in model_fields:
                    type_field = field_name
                    break

            if type_field and len(records) > 0 and type_field in records[0]:
                # If we have type information in the results, show a simple distribution
                types = {}
                for record in records:
                    record_type = record.get(type_field)
                    if record_type:
                        if isinstance(record_type, list):
                            # Handle many2many/one2many fields
                            if record_type and isinstance(record_type[0], dict):
                                for item in record_type:
                                    item_name = item.get("name", "Unknown")
                                    types[item_name] = types.get(item_name, 0) + 1
                        else:
                            # Handle scalar or many2one fields
                            if isinstance(record_type, dict) and "name" in record_type:
                                record_type = record_type["name"]
                            types[record_type] = types.get(record_type, 0) + 1

                if types:
                    type_field_label = model_fields[type_field].get(
                        "string", type_field.capitalize()
                    )
                    lines.append("\nSummary:")
                    lines.append(
                        f"- By {type_field_label}: "
                        + ", ".join(
                            f"{type_name} ({count})"
                            for type_name, count in types.items()
                        )
                    )

    # Add records
    if records:
        lines.append("\nRecords:")

        for i, record in enumerate(records, start=1):
            record_id = record.get("id")

            # Try to get a name or identifier for the record
            name_options = ["name", "display_name", "code", "reference", "number"]
            record_name = None

            for name_field in name_options:
                if name_field in record and record[name_field]:
                    record_name = record[name_field]
                    break

            if not record_name:
                record_name = f"Record #{record_id}"

            lines.append(f"{i}. {record_name} [odoo://{model}/record/{record_id}]")
    else:
        lines.append("\nNo records found matching the criteria.")

    # Add pagination links
    domain_str = str(domain).replace(" ", "")

    # Add next page link if there are more records
    if total_count > offset + limit:
        next_offset = offset + limit
        lines.append(
            f"\nNext page: odoo://{model}/search?domain={domain_str}&offset={next_offset}&limit={limit}"
        )

    # Add previous page link if we're not on the first page
    if offset > 0:
        prev_offset = max(0, offset - limit)
        lines.append(
            f"Previous page: odoo://{model}/search?domain={domain_str}&offset={prev_offset}&limit={limit}"
        )

    # Add refinement options for large result sets
    if total_count > 20:
        lines.append("\nRefinement options:")

        # Model-specific refinement suggestions
        if model == "res.partner":
            lines.append(
                f"- Companies only: odoo://{model}/search?domain=[('is_company','=',True)]"
            )
            lines.append(
                f"- Individuals only: odoo://{model}/search?domain=[('is_company','=',False)]"
            )
        elif model == "product.product":
            lines.append(
                f"- Stockable products: odoo://{model}/search?domain=[('type','=','product')]"
            )
            lines.append(
                f"- Services: odoo://{model}/search?domain=[('type','=','service')]"
            )
        elif model == "sale.order":
            lines.append(
                f"- Draft orders: odoo://{model}/search?domain=[('state','=','draft')]"
            )
            lines.append(
                f"- Confirmed orders: odoo://{model}/search?domain=[('state','=','sale')]"
            )

    return "\n".join(lines)


def format_field_list(model: str, fields_info: Dict[str, Dict[str, Any]]) -> str:
    """Format model field definitions for display.

    Args:
        model: Model name
        fields_info: Dictionary of field definitions

    Returns:
        str: Formatted field list
    """
    lines = [f"Fields for {model}:"]

    # Group fields by type
    fields_by_type = {}
    for field_name, field_info in fields_info.items():
        field_type = field_info.get("type", "unknown")
        if field_type not in fields_by_type:
            fields_by_type[field_type] = []
        fields_by_type[field_type].append((field_name, field_info))

    # Sort each group alphabetically by field name
    for field_type in fields_by_type:
        fields_by_type[field_type].sort(key=lambda x: x[0])

    # Order of field types to display
    type_order = [
        "char",
        "text",
        "integer",
        "float",
        "monetary",
        "boolean",
        "date",
        "datetime",
        "selection",
        "many2one",
        "one2many",
        "many2many",
        "binary",
    ]

    # Add remaining types not in the predefined order
    for field_type in sorted(fields_by_type.keys()):
        if field_type not in type_order:
            type_order.append(field_type)

    # Output fields grouped by type
    for field_type in type_order:
        if field_type not in fields_by_type:
            continue

        lines.append(f"\n{field_type.capitalize()} Fields:")
        for field_name, field_info in fields_by_type[field_type]:
            field_string = field_info.get("string", field_name)
            field_required = field_info.get("required", False)

            # Add relation info for relational fields
            if field_type in ["many2one", "one2many", "many2many"]:
                relation = field_info.get("relation", "unknown")
                lines.append(
                    f"  {field_name} ({field_string}) -> {relation}{'*' if field_required else ''}"
                )
            else:
                lines.append(
                    f"  {field_name} ({field_string}){'*' if field_required else ''}"
                )

    lines.append("\n* Required fields")
    return "\n".join(lines)
