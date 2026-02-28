"""Data formatters for LLM-friendly output.

This module provides formatters that convert Odoo data into
hierarchical text format optimized for LLM consumption.
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set

from .uri_schema import build_record_uri, build_search_uri

logger = logging.getLogger(__name__)


class RecordFormatter:
    """Formats Odoo records for LLM consumption.

    This class converts complex Odoo data structures into hierarchical
    text format that is easy for LLMs to understand and process.
    """

    # Field types that should be omitted by default
    OMIT_FIELDS = {
        "__last_update",
        "write_date",
        "create_date",
        "write_uid",
        "create_uid",
        "message_follower_ids",
        "message_ids",
        "message_main_attachment_id",
    }

    # Binary field types
    BINARY_FIELDS = {"binary", "image", "file"}

    def __init__(self, model: str, max_related_items: int = 5):
        """Initialize the formatter.

        Args:
            model: The Odoo model name
            max_related_items: Maximum number of related items to show inline
        """
        self.model = model
        self.max_related_items = max_related_items
        self._recursion_stack: Set[str] = set()

    def format_record(
        self,
        record: Dict[str, Any],
        fields_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
        indent_level: int = 0,
    ) -> str:
        """Format a single record into hierarchical text.

        Args:
            record: The record data dictionary
            fields_metadata: Optional field metadata from fields_get()
            indent_level: Current indentation level for nested structures

        Returns:
            Formatted text representation of the record
        """
        lines = []
        indent = "  " * indent_level

        # Record header
        record_id = record.get("id", "Unknown")
        record_name = record.get("display_name") or record.get("name", f"Record {record_id}")

        lines.append(f"{indent}{'=' * 50}")
        lines.append(f"{indent}Record: {self.model}/{record_id}")
        lines.append(f"{indent}Name: {record_name}")
        lines.append(f"{indent}{'=' * 50}")

        # Group fields by category
        simple_fields = []
        relation_fields = []

        for field_name, field_value in record.items():
            # Skip omitted fields
            if field_name in self.OMIT_FIELDS or field_name.startswith("_"):
                continue

            # Skip ID and name as they're in the header
            if field_name in ("id", "name", "display_name"):
                continue

            # Get field metadata if available
            field_meta = fields_metadata.get(field_name, {}) if fields_metadata else {}
            field_type = field_meta.get("type", "unknown")

            # Categorize fields
            if field_type in ("many2one", "one2many", "many2many"):
                relation_fields.append((field_name, field_value, field_meta))
            else:
                simple_fields.append((field_name, field_value, field_meta))

        # Format simple fields first
        if simple_fields:
            lines.append(f"{indent}Fields:")
            for field_name, field_value, field_meta in simple_fields:
                formatted_value = self._format_field_value(
                    field_name, field_value, field_meta, indent_level + 1
                )
                lines.append(f"{indent}  {field_name}: {formatted_value}")

        # Format relationship fields
        if relation_fields:
            lines.append(f"{indent}Relationships:")
            for field_name, field_value, field_meta in relation_fields:
                lines.extend(
                    self._format_relation_field(
                        field_name, field_value, field_meta, indent_level + 1
                    )
                )

        return "\n".join(lines)

    def format_list(
        self,
        records: List[Dict[str, Any]],
        fields_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> str:
        """Format a list of records into hierarchical text.

        Args:
            records: List of record dictionaries
            fields_metadata: Optional field metadata from fields_get()

        Returns:
            Formatted text representation of the record list
        """
        if not records:
            return f"No {self.model} records found."

        lines = [f"{'=' * 60}", f"{self.model} Records ({len(records)} found)", f"{'=' * 60}", ""]

        for idx, record in enumerate(records, 1):
            lines.append(f"[{idx}] {self._get_record_summary(record)}")
            lines.append("")

        return "\n".join(lines)

    def _format_field_value(
        self, field_name: str, value: Any, field_meta: Dict[str, Any], indent_level: int
    ) -> str:
        """Format a field value based on its type.

        Args:
            field_name: The field name
            value: The field value
            field_meta: Field metadata
            indent_level: Current indentation level

        Returns:
            Formatted field value
        """
        if value is None or value is False:
            return "Not set"

        field_type = field_meta.get("type", "unknown")

        # Text fields
        if field_type in ("char", "text", "html"):
            return str(value)

        # Numeric fields
        elif field_type in ("integer", "float", "monetary"):
            if field_type == "monetary":
                # Try to get currency information
                # TODO: Use currency_field to get proper currency formatting
                # currency_field = field_meta.get("currency_field", "currency_id")
                return f"{value:,.2f}"  # Format with thousand separators
            elif field_type == "float":
                digits = field_meta.get("digits", (16, 2))
                precision = digits[1] if isinstance(digits, tuple) else 2
                return f"{value:,.{precision}f}"
            else:
                return f"{value:,}"  # Integer with thousand separators

        # Date/time fields
        elif field_type in ("date", "datetime"):
            if isinstance(value, str):
                # Handle Odoo's datetime format (YYYYMMDDTHH:MM:SS)
                if (
                    field_type == "datetime"
                    and len(value) == 17
                    and "T" in value
                    and "-" not in value
                ):
                    try:
                        # Parse Odoo's compact datetime format
                        dt = datetime.strptime(value, "%Y%m%dT%H:%M:%S")
                        # Return proper ISO format with UTC timezone
                        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    except ValueError:
                        pass
                # Handle standard datetime formats
                elif field_type == "datetime" and " " in value:
                    try:
                        # Parse standard Odoo datetime format
                        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    except ValueError:
                        pass
                return value  # Return as-is if parsing fails
            elif isinstance(value, (datetime, date)):
                if isinstance(value, datetime):
                    # Ensure datetime includes timezone
                    return value.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                else:
                    # Date only
                    return value.isoformat()
            return str(value)

        # Boolean fields
        elif field_type == "boolean":
            return "Yes" if value else "No"

        # Selection fields
        elif field_type == "selection":
            # Try to get the human-readable selection value
            selection = field_meta.get("selection", [])
            for key, label in selection:
                if key == value:
                    return f"{label} ({value})"
            return str(value)

        # Binary fields
        elif field_type in self.BINARY_FIELDS:
            return f"[Binary data - use {self.model}/{field_name} to retrieve]"

        # Unknown type
        else:
            return str(value)

    def _format_relation_field(
        self, field_name: str, value: Any, field_meta: Dict[str, Any], indent_level: int
    ) -> List[str]:
        """Format a relationship field.

        Args:
            field_name: The field name
            value: The field value
            field_meta: Field metadata
            indent_level: Current indentation level

        Returns:
            List of formatted lines
        """
        lines = []
        indent = "  " * indent_level
        field_type = field_meta.get("type", "unknown")

        # Many2one fields
        if field_type == "many2one":
            if value and isinstance(value, (list, tuple)) and len(value) == 2:
                related_id, related_name = value
                related_model = field_meta.get("relation", "unknown")
                uri = build_record_uri(related_model, related_id)
                lines.append(f"{indent}{field_name}: {related_name} ({uri})")
            else:
                lines.append(f"{indent}{field_name}: Not set")

        # One2many and Many2many fields
        elif field_type in ("one2many", "many2many"):
            if value and isinstance(value, list):
                count = len(value)
                related_model = field_meta.get("relation", "unknown")

                # Build search URI for the related records
                if field_type == "one2many":
                    # For one2many, search by the inverse field
                    inverse_field = field_meta.get("relation_field", "parent_id")
                    domain = [(inverse_field, "=", self._get_current_record_id())]
                else:
                    # For many2many, we'd need the actual IDs
                    domain = [("id", "in", value)] if value else []

                search_uri = build_search_uri(related_model, domain=domain)

                lines.append(f"{indent}{field_name}: {count} record(s)")
                lines.append(f"{indent}  → View all: {search_uri}")

                # Show first few items if count is small
                if count <= self.max_related_items and isinstance(value[0], dict):
                    lines.append(f"{indent}  Items:")
                    for idx, item in enumerate(value[: self.max_related_items], 1):
                        summary = self._get_record_summary(item)
                        lines.append(f"{indent}    [{idx}] {summary}")
                elif count > self.max_related_items:
                    lines.append(
                        f"{indent}  (Showing count only - too many items to display inline)"
                    )
            else:
                lines.append(f"{indent}{field_name}: No records")

        return lines

    def _get_record_summary(self, record: Dict[str, Any]) -> str:
        """Get a one-line summary of a record.

        Args:
            record: The record dictionary

        Returns:
            One-line summary string
        """
        # Try different fields for the summary
        summary_fields = ["display_name", "name", "complete_name", "partner_id", "title"]

        for field in summary_fields:
            if field in record and record[field]:
                value = record[field]
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    return f"{value[1]} (ID: {value[0]})"
                elif isinstance(value, str):
                    return value

        # Fallback to ID
        return f"ID: {record.get('id', 'Unknown')}"

    def _get_current_record_id(self) -> Optional[int]:
        """Get the current record ID being formatted.

        This is a placeholder that should be set when formatting a specific record.

        Returns:
            Current record ID or None
        """
        # This would be set by the formatting context
        return None


class DatasetFormatter:
    """Formats datasets and search results for LLM consumption."""

    def __init__(self, model: str):
        """Initialize the dataset formatter.

        Args:
            model: The Odoo model name
        """
        self.model = model
        self.record_formatter = RecordFormatter(model)

    def format_search_results(
        self,
        records: List[Dict[str, Any]],
        domain: Optional[List] = None,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        total_count: Optional[int] = None,
        fields_metadata: Optional[Dict[str, Any]] = None,
        next_uri: Optional[str] = None,
        prev_uri: Optional[str] = None,
        current_page: Optional[int] = None,
        total_pages: Optional[int] = None,
    ) -> str:
        """Format search results with context and pagination.

        Args:
            records: List of record dictionaries
            domain: Search domain used
            fields: Fields that were requested
            limit: Limit used in search
            offset: Offset used in search
            total_count: Total count of matching records
            fields_metadata: Optional field metadata for rich formatting
            next_uri: URI for next page of results
            prev_uri: URI for previous page of results
            current_page: Current page number
            total_pages: Total number of pages

        Returns:
            Formatted search results with pagination
        """
        lines = [
            f"{'=' * 60}",
            f"Search Results: {self.model}",
            f"{'=' * 60}",
        ]

        # Add search context
        if domain:
            lines.append(f"Search criteria: {self._format_domain(domain)}")

        # Add pagination info
        if total_count is not None:
            showing = len(records)
            if current_page and total_pages:
                lines.append(f"Page {current_page} of {total_pages}")
            if offset is not None:
                lines.append(f"Showing records {offset + 1}-{offset + showing} of {total_count}")
            else:
                lines.append(f"Showing {showing} of {total_count} records")
        else:
            lines.append(f"Found {len(records)} records")

        if fields:
            lines.append(f"Fields: {', '.join(fields)}")

        lines.append("")

        # Format each record
        if not records:
            lines.append("No records found matching the criteria.")
        else:
            for idx, record in enumerate(records, 1):
                if offset:
                    idx = offset + idx
                lines.append(f"[{idx}] {self.record_formatter._get_record_summary(record)}")

                # Add selected field values if specific fields were requested
                if fields and len(fields) <= 5:  # Only show inline for small field sets
                    for field in fields:
                        if field in record and field not in ("id", "name", "display_name"):
                            value = record[field]
                            formatted = self._format_simple_value(value)
                            lines.append(f"    {field}: {formatted}")

                lines.append("")

        # Add navigation links
        navigation = []
        if prev_uri:
            navigation.append(f"← Previous page: {prev_uri}")
        if next_uri:
            navigation.append(f"→ Next page: {next_uri}")

        if navigation:
            lines.append("\nNavigation:")
            lines.extend(navigation)

        # Add summary statistics for large datasets
        if total_count and total_count > 100:
            lines.append("\nDataset Summary:")
            lines.append(f"Total records: {total_count:,}")
            if domain:
                lines.append("Use additional filters to refine results")

        return "\n".join(lines)

    def _format_domain(self, domain: List) -> str:
        """Format a search domain in human-readable form.

        Args:
            domain: Odoo search domain

        Returns:
            Human-readable domain description
        """
        if not domain:
            return "All records"

        conditions = []
        for condition in domain:
            if isinstance(condition, (list, tuple)) and len(condition) == 3:
                field, operator, value = condition
                conditions.append(f"{field} {operator} {value}")
            elif condition in ("&", "|", "!"):
                conditions.append(condition)

        return " ".join(conditions) if conditions else str(domain)

    def _format_simple_value(self, value: Any) -> str:
        """Format a simple value for inline display.

        Args:
            value: The value to format

        Returns:
            Formatted value string
        """
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if value is None:
            return "Not set"
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            # Many2one value
            return f"{value[1]} (ID: {value[0]})"
        elif isinstance(value, list):
            return f"{len(value)} items"
        else:
            return str(value)
