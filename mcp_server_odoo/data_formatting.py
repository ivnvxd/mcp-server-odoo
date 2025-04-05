"""Data formatting utilities for Odoo records.

This module provides functions for formatting Odoo data into
human-readable text suitable for MCP resources.
"""

import logging
from typing import Any, Dict, List

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

    # Handle null values
    if field_value is None:
        return f"{indent_str}{field_name}: Not set"

    # Handle different field types
    if field_type == "char" or field_type == "text":
        return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "integer":
        return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "float":
        return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "monetary":
        # TODO: Get currency from record
        return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "boolean":
        if field_value is False:
            return f"{indent_str}{field_name}: No"
        return f"{indent_str}{field_name}: Yes"

    elif field_type == "date":
        return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "datetime":
        return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "selection":
        return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "many2one":
        # For many2one, field_value is typically [id, name]
        if isinstance(field_value, list) and len(field_value) == 2:
            related_id, related_name = field_value
            relation = (
                odoo.get_model_fields(model)
                .get(field_name.lower(), {})
                .get("relation", "")
            )
            if not relation:
                # Fallback to using the field name if relation is not found
                relation = field_name
            return (
                f"{indent_str}{field_name}: {related_name} "
                f"[odoo://{relation}/record/{related_id}]"
            )
        else:
            return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "one2many" or field_type == "many2many":
        # For *2many fields, value is usually a list of IDs
        if isinstance(field_value, list):
            count = len(field_value)
            # Get the relation model from field metadata
            field_info = odoo.get_model_fields(model).get(field_name.lower(), {})
            relation = field_info.get("relation", "")

            if relation and count > 0:
                ids_str = ",".join(str(i) for i in field_value)
                return (
                    f"{indent_str}{field_name}: {count} related records "
                    f"[odoo://{relation}/browse?ids={ids_str}]"
                )
            else:
                return f"{indent_str}{field_name}: {count} related records"
        else:
            return f"{indent_str}{field_name}: {field_value}"

    elif field_type == "binary":
        return f"{indent_str}{field_name}: [Binary data]"

    else:
        # Default for unknown types
        return f"{indent_str}{field_name}: {field_value}"


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
    lines.append(f"Search Results: {model} ({total_count} total matches)")

    # Pagination info
    from_record = offset + 1
    to_record = min(offset + limit, total_count)

    if total_count > 0:
        lines.append(f"Showing: Records {from_record}-{to_record} of {total_count}")

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
    if total_count > to_record:
        # There are more records, add next page link
        next_offset = offset + limit
        domain_str = str(domain).replace(" ", "")
        lines.append(
            f"\nNext page: odoo://{model}/search?domain={domain_str}&offset={next_offset}&limit={limit}"
        )

    if offset > 0:
        # We're not on the first page, add previous page link
        prev_offset = max(0, offset - limit)
        domain_str = str(domain).replace(" ", "")
        lines.append(
            f"Previous page: odoo://{model}/search?domain={domain_str}&offset={prev_offset}&limit={limit}"
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
