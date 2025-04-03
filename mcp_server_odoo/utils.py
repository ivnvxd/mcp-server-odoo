"""Utility functions for MCP Server Odoo.

This module provides helper functions and utilities used throughout
the MCP server implementation.
"""

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_uri(uri: str) -> Tuple[str, str, Dict[str, str]]:
    """Parse an MCP resource URI.

    Args:
        uri: Resource URI to parse

    Returns:
        Tuple[str, str, Dict[str, str]]: Model, operation, and parameters

    Raises:
        ValueError: If URI format is invalid
    """
    # Check if URI matches expected pattern
    uri_pattern = r"^odoo://([^/]+)/([^?]+)(?:\?(.*))?$"
    match = re.match(uri_pattern, uri)

    if not match:
        raise ValueError(f"Invalid URI format: {uri}")

    model, operation, query_string = match.groups()

    # Parse query parameters
    params = {}
    if query_string:
        query_params = urllib.parse.parse_qs(query_string)
        params = {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}

    return model, operation, params


def parse_domain(domain_str: str) -> List[Tuple]:
    """Parse an Odoo domain string into a domain list.

    Args:
        domain_str: Domain string to parse

    Returns:
        List[Tuple]: Parsed domain

    Raises:
        ValueError: If domain format is invalid
    """
    try:
        if not domain_str:
            return []

        # Handle URL-encoded domain
        decoded_domain = urllib.parse.unquote(domain_str)

        # Parse JSON
        domain = json.loads(decoded_domain)

        # Validate domain structure
        if not isinstance(domain, list):
            raise ValueError("Domain must be a list")

        for condition in domain:
            if not isinstance(condition, list) or len(condition) != 3:
                raise ValueError("Each domain condition must be a 3-element list")

        return domain

    except json.JSONDecodeError:
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
    """
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
