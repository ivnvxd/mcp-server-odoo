"""MCP resource handlers for Odoo.

This module implements the resource handling logic for the MCP server,
providing handlers for different resource operations and URI schemes.
"""

import json
import logging
import re
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any, Dict

from mcp import Resource

from mcp_server_odoo.data_formatting import format_record, format_search_results
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError

logger = logging.getLogger(__name__)

# URI regex patterns
MODEL_RECORD_PATTERN = r"^odoo://([^/]+)/record/(\d+)$"
MODEL_SEARCH_PATTERN = r"^odoo://([^/]+)/search"
MODEL_BROWSE_PATTERN = r"^odoo://([^/]+)/browse"
MODEL_COUNT_PATTERN = r"^odoo://([^/]+)/count"
MODEL_FIELDS_PATTERN = r"^odoo://([^/]+)/fields$"


class ResourceHandlerError(Exception):
    """Exception raised for resource handling errors."""

    pass


class BaseResourceHandler(ABC):
    """Base class for MCP resource handlers.

    This abstract class defines the interface for resource handlers
    and provides common functionality for all handlers.
    """

    def __init__(self, odoo: OdooConnection):
        """Initialize the resource handler.

        Args:
            odoo: Odoo connection instance
        """
        self.odoo = odoo

    @abstractmethod
    def can_handle(self, resource: Resource) -> bool:
        """Check if this handler can handle the given resource.

        Args:
            resource: Resource to check

        Returns:
            bool: True if this handler can handle the resource
        """
        pass

    @abstractmethod
    def handle(self, resource: Resource) -> dict:
        """Handle the resource request.

        Args:
            resource: Resource to handle

        Returns:
            dict: Response content dictionary

        Raises:
            ResourceHandlerError: If handling fails
        """
        pass

    def _parse_query_params(self, resource: Resource) -> Dict[str, Any]:
        """Parse query parameters from resource URI.

        Args:
            resource: Resource containing URI with parameters

        Returns:
            Dict[str, Any]: Parsed parameters
        """
        # Extract query string
        parsed_url = urllib.parse.urlparse(str(resource.uri))

        # Parse query parameters
        query_params = urllib.parse.parse_qs(parsed_url.query)

        # Convert to regular dict with single values
        params = {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}

        return params


class RecordResourceHandler(BaseResourceHandler):
    """Handler for record retrieval resources."""

    def can_handle(self, resource: Resource) -> bool:
        """Check if this handler can handle record resources.

        Args:
            resource: Resource to check

        Returns:
            bool: True if URI matches record pattern
        """
        return bool(re.match(MODEL_RECORD_PATTERN, str(resource.uri)))

    def handle(self, resource: Resource) -> dict:
        """Handle record retrieval.

        Args:
            resource: Record resource

        Returns:
            dict: Formatted record content

        Raises:
            ResourceHandlerError: If record retrieval fails
        """
        match = re.match(MODEL_RECORD_PATTERN, str(resource.uri))
        if not match:
            raise ResourceHandlerError(f"Invalid record URI: {resource.uri}")

        model, record_id = match.groups()
        record_id = int(record_id)

        try:
            # Get record by ID
            records = self.odoo.read(model, [record_id])

            if not records:
                raise ResourceHandlerError(f"Record not found: {model} #{record_id}")

            # Format record for display
            formatted_record = format_record(model, records[0], self.odoo)

            return {
                "is_error": False,
                "content": [
                    {
                        "type": "text",
                        "text": formatted_record,
                    },
                ],
            }

        except OdooConnectionError as e:
            raise ResourceHandlerError(str(e))
        except Exception as e:
            # Catch any other unexpected errors
            logger.error(f"Error retrieving record: {e}", exc_info=True)
            raise ResourceHandlerError(f"Error retrieving record: {e}")


class SearchResourceHandler(BaseResourceHandler):
    """Handler for search resources."""

    def __init__(
        self, odoo: OdooConnection, default_limit: int = 20, max_limit: int = 100
    ):
        """Initialize the search resource handler.

        Args:
            odoo: Odoo connection instance
            default_limit: Default record limit
            max_limit: Maximum allowed record limit
        """
        super().__init__(odoo)
        self.default_limit = default_limit
        self.max_limit = max_limit

    def can_handle(self, resource: Resource) -> bool:
        """Check if this handler can handle search resources.

        Args:
            resource: Resource to check

        Returns:
            bool: True if URI matches search pattern
        """
        return bool(re.match(MODEL_SEARCH_PATTERN, str(resource.uri)))

    def handle(self, resource: Resource) -> dict:
        """Handle search requests.

        Args:
            resource: Search resource

        Returns:
            dict: Formatted search results

        Raises:
            ResourceHandlerError: If search fails
        """
        match = re.match(MODEL_SEARCH_PATTERN, str(resource.uri))
        if not match:
            raise ResourceHandlerError(f"Invalid search URI: {resource.uri}")

        model = match.group(1)
        params = self._parse_query_params(resource)

        # Parse domain parameter
        domain = []
        if "domain" in params:
            try:
                domain_str = params["domain"]
                # Handle both string and array formats
                if isinstance(domain_str, str):
                    try:
                        # First, try parsing as JSON
                        domain = json.loads(domain_str)
                    except json.JSONDecodeError:
                        # If that fails, try to handle Odoo's string format like [('field','=',value)]
                        # This is a common format in Odoo but not valid JSON
                        logger.warning(f"Failed to parse domain as JSON: {domain_str}")
                        # For now, we'll raise an error - this format requires custom parsing
                        raise ResourceHandlerError(
                            f"Invalid domain format: {domain_str}. Use valid JSON array format."
                        )
                elif isinstance(domain_str, list):
                    domain = domain_str

                # Validate domain structure - simple check to ensure it's a list
                if not isinstance(domain, list):
                    raise ResourceHandlerError(
                        "Invalid domain structure: domain must be a list"
                    )

                # Ensure all domain items are properly formatted as lists, not tuples
                # (This addresses tuple vs list representation differences)
                for i, item in enumerate(domain):
                    if isinstance(item, tuple):
                        domain[i] = list(item)
                    elif isinstance(item, list) and len(item) == 3:
                        # Already in the right format
                        pass
                    # Skip logical operators (&, |, !) - they're just strings
                    elif item in ("&", "|", "!"):
                        pass
                    else:
                        # All other domain items should be 3-element lists
                        if not (isinstance(item, list) and len(item) == 3):
                            raise ResourceHandlerError(
                                f"Invalid domain item format: {item}. Expected [field, operator, value]"
                            )
            except json.JSONDecodeError:
                raise ResourceHandlerError(f"Invalid domain format: {params['domain']}")
            except Exception as e:
                logger.error(f"Error parsing domain: {e}", exc_info=True)
                raise ResourceHandlerError(f"Error parsing domain: {e}")

        # Parse fields parameter - supports comma-separated string or list
        fields = None
        if "fields" in params and params["fields"]:
            if isinstance(params["fields"], str):
                fields = [f.strip() for f in params["fields"].split(",") if f.strip()]
            elif isinstance(params["fields"], list):
                fields = params["fields"]

            # Validate fields are non-empty
            if not fields:
                fields = None

        # Handle limit with default and max constraints
        limit = self.default_limit
        if "limit" in params:
            try:
                limit = int(params["limit"])
                # Ensure limit is at least 1 and at most max_limit
                limit = max(1, min(limit, self.max_limit))
            except ValueError:
                logger.warning(
                    f"Invalid limit parameter: {params['limit']}, using default {self.default_limit}"
                )

        # Parse offset, ensure non-negative
        offset = 0
        if "offset" in params:
            try:
                offset = int(params["offset"])
                offset = max(0, offset)  # Ensure non-negative
            except ValueError:
                logger.warning(f"Invalid offset parameter: {params['offset']}, using 0")

        # Parse order
        order = params.get("order")

        try:
            # Get total count for pagination
            total_count = self.odoo.count(model, domain)

            # Perform search
            records = self.odoo.search(
                model=model,
                domain=domain,
                fields=fields,
                limit=limit,
                offset=offset,
                order=order,
            )

            # Format search results
            formatted_results = format_search_results(
                model=model,
                records=records,
                total_count=total_count,
                limit=limit,
                offset=offset,
                domain=domain,
                odoo=self.odoo,
            )

            return {
                "is_error": False,
                "content": [
                    {
                        "type": "text",
                        "text": formatted_results,
                    },
                ],
            }

        except OdooConnectionError as e:
            # Let the parent handler chain handle common errors
            raise ResourceHandlerError(str(e))
        except Exception as e:
            # Catch any other unexpected errors
            logger.error(f"Error during search operation: {e}", exc_info=True)
            raise ResourceHandlerError(f"Error during search operation: {e}")


class BrowseResourceHandler(BaseResourceHandler):
    """Handler for browse resources."""

    def can_handle(self, resource: Resource) -> bool:
        """Check if this handler can handle browse resources.

        Args:
            resource: Resource to check

        Returns:
            bool: True if URI matches browse pattern
        """
        return bool(re.match(MODEL_BROWSE_PATTERN, str(resource.uri)))

    def handle(self, resource: Resource) -> dict:
        """Handle browse requests.

        Args:
            resource: Browse resource

        Returns:
            dict: Formatted records

        Raises:
            ResourceHandlerError: If browse fails
        """
        match = re.match(MODEL_BROWSE_PATTERN, str(resource.uri))
        if not match:
            raise ResourceHandlerError(f"Invalid browse URI: {resource.uri}")

        model = match.group(1)
        params = self._parse_query_params(resource)

        # Parse ids parameter
        ids = []
        if "ids" in params:
            try:
                ids_str = params["ids"]
                if isinstance(ids_str, str):
                    ids = [int(id_str) for id_str in ids_str.split(",")]
            except ValueError:
                raise ResourceHandlerError(f"Invalid ids format: {params['ids']}")

        if not ids:
            raise ResourceHandlerError("No record IDs provided for browse")

        # Parse fields parameter
        fields = (
            params.get("fields", "").split(",")
            if "fields" in params and params["fields"]
            else None
        )

        try:
            # Read records by IDs
            records = self.odoo.read(model, ids, fields)

            if not records:
                raise ResourceHandlerError(
                    f"No records found for {model} with IDs {ids}"
                )

            # Format each record
            formatted_records = "\n\n".join(
                format_record(model, record, self.odoo) for record in records
            )

            return {
                "is_error": False,
                "content": [
                    {
                        "type": "text",
                        "text": formatted_records,
                    },
                ],
            }

        except OdooConnectionError as e:
            raise ResourceHandlerError(str(e))


class CountResourceHandler(BaseResourceHandler):
    """Handler for count resources."""

    def can_handle(self, resource: Resource) -> bool:
        """Check if this handler can handle count resources.

        Args:
            resource: Resource to check

        Returns:
            bool: True if URI matches count pattern
        """
        return bool(re.match(MODEL_COUNT_PATTERN, str(resource.uri)))

    def handle(self, resource: Resource) -> dict:
        """Handle count requests.

        Args:
            resource: Count resource

        Returns:
            dict: Count information

        Raises:
            ResourceHandlerError: If count fails
        """
        match = re.match(MODEL_COUNT_PATTERN, str(resource.uri))
        if not match:
            raise ResourceHandlerError(f"Invalid count URI: {resource.uri}")

        model = match.group(1)
        params = self._parse_query_params(resource)

        # Parse domain parameter
        domain = []
        if "domain" in params:
            try:
                domain_str = params["domain"]
                if isinstance(domain_str, str):
                    domain = json.loads(domain_str)
            except json.JSONDecodeError:
                raise ResourceHandlerError(f"Invalid domain format: {params['domain']}")

        try:
            # Get count
            count = self.odoo.count(model, domain)

            return {
                "is_error": False,
                "content": [
                    {
                        "type": "text",
                        "text": f"Count: {count} records found for {model}",
                    },
                ],
            }

        except OdooConnectionError as e:
            raise ResourceHandlerError(str(e))


class FieldsResourceHandler(BaseResourceHandler):
    """Handler for fields resources."""

    def can_handle(self, resource: Resource) -> bool:
        """Check if this handler can handle fields resources.

        Args:
            resource: Resource to check

        Returns:
            bool: True if URI matches fields pattern
        """
        return bool(re.match(MODEL_FIELDS_PATTERN, str(resource.uri)))

    def handle(self, resource: Resource) -> dict:
        """Handle fields requests.

        Args:
            resource: Fields resource

        Returns:
            dict: Field definitions

        Raises:
            ResourceHandlerError: If fields retrieval fails
        """
        match = re.match(MODEL_FIELDS_PATTERN, str(resource.uri))
        if not match:
            raise ResourceHandlerError(f"Invalid fields URI: {resource.uri}")

        model = match.group(1)

        try:
            # Get field definitions
            fields_info = self.odoo.get_model_fields(model)

            # Format field information
            formatted_fields = f"Fields for {model}:\n\n"
            for field_name, field_info in fields_info.items():
                field_type = field_info.get("type", "unknown")
                field_string = field_info.get("string", field_name)
                field_help = field_info.get("help", "")

                formatted_fields += f"{field_name} ({field_type}): {field_string}\n"
                if field_help:
                    formatted_fields += f"  Description: {field_help}\n"

                # Add relation info for relational fields
                if field_type in ("many2one", "one2many", "many2many"):
                    relation = field_info.get("relation", "")
                    if relation:
                        formatted_fields += f"  Related model: {relation}\n"

                formatted_fields += "\n"

            return {
                "is_error": False,
                "content": [
                    {
                        "type": "text",
                        "text": formatted_fields,
                    },
                ],
            }

        except OdooConnectionError as e:
            raise ResourceHandlerError(str(e))


class ResourceHandlerRegistry:
    """Registry for resource handlers.

    This class manages a collection of resource handlers and
    dispatches resource requests to the appropriate handler.
    """

    def __init__(
        self,
        odoo: OdooConnection,
        default_limit: int = 20,
        max_limit: int = 100,
    ):
        """Initialize the resource handler registry.

        Args:
            odoo: Odoo connection instance
            default_limit: Default record limit
            max_limit: Maximum allowed record limit
        """
        self.odoo = odoo
        self.default_limit = default_limit
        self.max_limit = max_limit

        # Register handlers
        self.handlers = [
            RecordResourceHandler(odoo),
            SearchResourceHandler(odoo, default_limit, max_limit),
            BrowseResourceHandler(odoo),
            CountResourceHandler(odoo),
            FieldsResourceHandler(odoo),
        ]

    def handle_resource(self, resource: Resource) -> dict:
        """Handle a resource request.

        Args:
            resource: Resource to handle

        Returns:
            dict: Response content

        Raises:
            ResourceHandlerError: If no handler found or handling fails
        """
        # Find handler for the resource
        for handler in self.handlers:
            if handler.can_handle(resource):
                return handler.handle(resource)

        # No handler found
        raise ResourceHandlerError(f"Unsupported resource URI format: {resource.uri}")
