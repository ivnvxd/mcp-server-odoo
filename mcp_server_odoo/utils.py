"""Utility functions for MCP Server Odoo.

This module provides helper functions and utilities used throughout
the MCP server implementation.
"""

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

# Valid operations regex patterns
RECORD_OPERATION_PATTERN = r"^record/(\d+)$"
SEARCH_OPERATION_PATTERN = r"^search$"
BROWSE_OPERATION_PATTERN = r"^browse$"
COUNT_OPERATION_PATTERN = r"^count$"
FIELDS_OPERATION_PATTERN = r"^fields$"

# Model name validation pattern
MODEL_NAME_PATTERN = r"^[a-z][a-z0-9_.]*$"


def parse_uri(
    uri: str, enabled_models: Optional[Set[str]] = None
) -> Tuple[str, str, Dict[str, Any]]:
    """Parse an MCP resource URI.

    Args:
        uri: Resource URI to parse
        enabled_models: Optional set of enabled model names to validate against

    Returns:
        Tuple[str, str, Dict[str, Any]]: Model, operation, and parameters

    Raises:
        ValueError: If URI format is invalid or model is not enabled
    """
    # Check if URI matches expected pattern
    uri_pattern = r"^odoo://([^/]+)/([^?]+)(?:\?(.*))?$"
    match = re.match(uri_pattern, uri)

    if not match:
        raise ValueError(f"Invalid URI format: {uri}")

    model, operation, query_string = match.groups()

    # Validate model name format
    if not re.match(MODEL_NAME_PATTERN, model):
        raise ValueError(f"Invalid model name format: {model}")

    # Check if model is enabled (if a list was provided)
    if enabled_models is not None and model not in enabled_models:
        raise ValueError(f"Model not enabled for MCP access: {model}")

    # Validate operation format
    validate_operation(operation)

    # Parse query parameters
    params = {}
    if query_string:
        query_params = urllib.parse.parse_qs(query_string)
        params = {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}

    return model, operation, params


def validate_operation(operation: str) -> Tuple[str, Optional[int]]:
    """Validate and parse operation from URI.

    Args:
        operation: Operation string from URI

    Returns:
        Tuple[str, Optional[int]]: Operation type and optional ID

    Raises:
        ValueError: If operation format is invalid
    """
    # Check for record/<id> operation
    record_match = re.match(RECORD_OPERATION_PATTERN, operation)
    if record_match:
        return "record", int(record_match.group(1))

    # Check other operations
    if re.match(SEARCH_OPERATION_PATTERN, operation):
        return "search", None
    elif re.match(BROWSE_OPERATION_PATTERN, operation):
        return "browse", None
    elif re.match(COUNT_OPERATION_PATTERN, operation):
        return "count", None
    elif re.match(FIELDS_OPERATION_PATTERN, operation):
        return "fields", None

    raise ValueError(f"Invalid operation: {operation}")


def parse_domain(domain_str: str) -> List[Union[str, List]]:
    """Parse an Odoo domain string into a domain list.

    Args:
        domain_str: Domain string to parse

    Returns:
        List[Union[str, List]]: Parsed domain

    Raises:
        ValueError: If domain format is invalid
    """
    try:
        if not domain_str:
            return []

        # Handle URL-encoded domain
        decoded_domain = urllib.parse.unquote(domain_str)

        # Parse JSON
        try:
            domain = json.loads(decoded_domain)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid domain format: {domain_str}")

        # Validate domain structure
        if not isinstance(domain, list):
            raise ValueError("Domain must be a list")

        # Validate individual conditions or operators
        for item in domain:
            if isinstance(item, list):
                # This is a condition triplet
                if len(item) != 3:
                    raise ValueError("Each domain condition must be a 3-element list")

                # Basic operator validation
                if isinstance(item[1], str) and item[1] not in (
                    "=",
                    "!=",
                    ">",
                    ">=",
                    "<",
                    "<=",
                    "like",
                    "ilike",
                    "not like",
                    "not ilike",
                    "in",
                    "not in",
                    "child_of",
                    "parent_of",
                    "=like",
                    "=ilike",
                ):
                    raise ValueError(f"Invalid operator in domain condition: {item[1]}")
            elif isinstance(item, str):
                # This is a logical operator
                if item not in ("&", "|", "!"):
                    raise ValueError(f"Invalid logical operator: {item}")
            else:
                raise ValueError(f"Invalid domain item type: {type(item)}")

        return domain

    except Exception as e:
        if isinstance(e, ValueError) and str(e).startswith("Invalid"):
            raise
        raise ValueError(f"Invalid domain format: {domain_str}")


def sanitize_string(value: str) -> str:
    """Sanitize a string for safe output.

    Args:
        value: String to sanitize

    Returns:
        str: Sanitized string
    """
    if not isinstance(value, str):
        value = str(value)

    # Remove control characters
    value = "".join(char for char in value if ord(char) >= 32 or char in "\n\r\t")

    # Limit length for very long strings
    max_length = 1000
    if len(value) > max_length:
        value = value[:max_length] + "... (truncated)"

    return value


def get_model_display_name(model: str) -> str:
    """Get a user-friendly display name for a model.

    Args:
        model: Technical model name

    Returns:
        str: User-friendly name
    """
    # Convert technical name to user-friendly name
    # e.g., "res.partner" -> "Partner"
    parts = model.split(".")

    if len(parts) > 1:
        # Use last part for models with dots
        name = parts[-1]
    else:
        name = model

    # Capitalize and replace underscores
    name = name.replace("_", " ").title()

    return name


def build_resource_uri(
    model: str, operation: str, params: Optional[Dict[str, Any]] = None
) -> str:
    """Build an MCP resource URI.

    Args:
        model: Model name
        operation: Operation name
        params: Query parameters

    Returns:
        str: Formatted URI

    Raises:
        ValueError: If model or operation is invalid
    """
    # Validate model name
    if not re.match(MODEL_NAME_PATTERN, model):
        raise ValueError(f"Invalid model name format: {model}")

    # Validate operation (will raise ValueError if invalid)
    validate_operation(operation)

    base_uri = f"odoo://{model}/{operation}"

    if not params:
        return base_uri

    # Convert parameters to query string
    query_parts = []

    for key, value in params.items():
        if isinstance(value, list) or isinstance(value, dict):
            # Convert complex types to JSON
            encoded_value = urllib.parse.quote(json.dumps(value))
        else:
            encoded_value = urllib.parse.quote(str(value))

        query_parts.append(f"{key}={encoded_value}")

    query_string = "&".join(query_parts)

    return f"{base_uri}?{query_string}"


def build_record_uri(model: str, record_id: Union[int, str]) -> str:
    """Build a URI for a specific record.

    Args:
        model: Model name
        record_id: Record ID

    Returns:
        str: Record URI
    """
    return build_resource_uri(model, f"record/{record_id}")


def build_search_uri(
    model: str,
    domain: Optional[List] = None,
    fields: Optional[List[str]] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    order: Optional[str] = None,
) -> str:
    """Build a search URI with the given parameters.

    Args:
        model: Model name
        domain: Optional domain filter
        fields: Optional fields to include
        limit: Optional record limit
        offset: Optional record offset
        order: Optional sort order

    Returns:
        str: Search URI
    """
    params = {}

    if domain:
        params["domain"] = domain

    if fields:
        params["fields"] = ",".join(fields)

    if limit is not None:
        params["limit"] = limit

    if offset is not None:
        params["offset"] = offset

    if order:
        params["order"] = order

    return build_resource_uri(model, "search", params)


def build_browse_uri(model: str, ids: List[Union[int, str]]) -> str:
    """Build a URI for browsing multiple records by ID.

    Args:
        model: Model name
        ids: List of record IDs

    Returns:
        str: Browse URI
    """
    params = {"ids": ",".join(str(id_) for id_ in ids)}
    return build_resource_uri(model, "browse", params)


def build_count_uri(model: str, domain: Optional[List] = None) -> str:
    """Build a URI for counting records.

    Args:
        model: Model name
        domain: Optional domain filter

    Returns:
        str: Count URI
    """
    params = {}
    if domain:
        params["domain"] = domain

    return build_resource_uri(model, "count", params)


def build_fields_uri(model: str) -> str:
    """Build a URI for fetching model fields.

    Args:
        model: Model name

    Returns:
        str: Fields URI
    """
    return build_resource_uri(model, "fields")
