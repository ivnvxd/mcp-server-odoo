"""MCP Server implementation for Odoo using FastMCP.

This module implements the MCP Server for Odoo using the high-level FastMCP API.
It handles resource management, client communication, and server lifecycle.
"""

import logging
import urllib.parse
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP

from mcp_server_odoo.data_formatting import format_record, format_search_results
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError

logger = logging.getLogger(__name__)


class OdooServer:
    """FastMCP Server implementation for Odoo.

    This class manages the MCP server lifecycle and connects to Odoo
    using provided configuration.
    """

    def __init__(
        self,
        odoo_url: str,
        odoo_db: str,
        odoo_token: str,
        odoo_username: Optional[str] = None,
        odoo_password: Optional[str] = None,
        default_limit: int = 50,
        max_limit: int = 100,
    ):
        """Initialize the Odoo MCP Server.

        Args:
            odoo_url: URL of the Odoo instance
            odoo_db: Odoo database name
            odoo_token: Authentication token for Odoo MCP
            odoo_username: Optional username for Odoo authentication
            odoo_password: Optional password for Odoo authentication
            default_limit: Default record limit for search operations (default: 50)
            max_limit: Maximum allowed record limit (default: 100)
        """
        self.odoo_url = odoo_url
        self.odoo_db = odoo_db
        self.odoo_token = odoo_token
        self.odoo_username = odoo_username
        self.odoo_password = odoo_password
        self.default_limit = default_limit
        self.max_limit = max_limit

        # Create Odoo connection directly
        self.odoo = OdooConnection(
            url=self.odoo_url,
            db=self.odoo_db,
            token=self.odoo_token,
            username=self.odoo_username,
            password=self.odoo_password,
        )

        # Test connection
        try:
            self.odoo.test_connection()
            logger.info(f"Connected to Odoo at {self.odoo_url}")
        except Exception as e:
            logger.exception(f"Failed to initialize Odoo connection: {e}")
            raise

        # Create the FastMCP server
        self.mcp = FastMCP("Odoo")

        # Register resources and tools
        self._register_resources()
        self._register_tools()

    def _register_resources(self) -> None:
        """Register all resource handlers and static resources."""

        # Register static resources for each available model
        if self.odoo.available_models:
            for model in self.odoo.available_models:
                model_uri = f"odoo://{model}"
                model_name = model.replace(".", "_")

                # Register model info resource
                @self.mcp.resource(model_uri)
                def get_model_info() -> str:
                    """Get model information.

                    Returns:
                        str: Model information
                    """
                    # Use closure to capture model name
                    current_model = model

                    try:
                        return f"# {current_model}\n\nModel in Odoo instance at {self.odoo_url}\n\nUse resources:\n- odoo://{current_model}/fields - Get field information\n- odoo://{current_model}/count - Count records\n- odoo://{current_model}/search - Search records\n- odoo://{current_model}/browse - Browse specific records\n- odoo://{current_model}/record/<id> - Get a specific record"
                    except Exception as e:
                        logger.error(f"Error getting model info: {e}", exc_info=True)
                        return f"Error getting model info: {e}"

        # Register other resource handlers
        # Record resource handler
        @self.mcp.resource("odoo://{model}/record/{record_id}")
        def get_record(model: str, record_id: str) -> str:
            """Get a specific record from Odoo.

            Args:
                model: The Odoo model name
                record_id: Record ID

            Returns:
                str: Formatted record content
            """
            try:
                odoo = self.odoo
                record_id_int = int(record_id)
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Get record by ID
                records = odoo.read(model, [record_id_int])

                if not records:
                    return f"Record not found: {model} #{record_id}"

                # Format record for display
                return format_record(model, records[0], odoo)

            except OdooConnectionError as e:
                return f"Error retrieving record: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error retrieving record: {e}", exc_info=True)
                return f"Error retrieving record: {e}"

        @self.mcp.resource("odoo://{model}/search")
        def search_records(model: str) -> str:
            """Search for records in Odoo.

            Args:
                model: The Odoo model name

            Returns:
                str: Formatted search results
            """
            try:
                odoo = self.odoo
                default_limit = self.default_limit
                max_limit = self.max_limit
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Access the current resource URI
                ctx = Context.current()
                if ctx is None:
                    return "Error: No context available"

                # Parse query parameters
                uri = ctx.resource.uri
                parsed_url = urllib.parse.urlparse(str(uri))
                query_params = urllib.parse.parse_qs(parsed_url.query)
                params = {
                    k: v[0] if len(v) == 1 else v for k, v in query_params.items()
                }

                # Parse domain from query params
                domain = params.get("domain", "[]")
                try:
                    # Convert domain string to Python list
                    if isinstance(domain, str):
                        domain = eval(
                            domain
                        )  # Safe because we control the input format
                except Exception as e:
                    return f"Invalid domain format: {e}"

                # Get limit and offset
                try:
                    limit = int(params.get("limit", default_limit))
                    # Enforce max limit
                    limit = min(limit, max_limit)
                except (ValueError, TypeError):
                    limit = default_limit

                try:
                    offset = int(params.get("offset", 0))
                except (ValueError, TypeError):
                    offset = 0

                # Get fields to return
                fields = params.get("fields", None)
                if fields and isinstance(fields, str):
                    try:
                        fields = eval(fields)  # Convert string representation to list
                    except Exception:
                        fields = fields.split(",")

                # Get order
                order = params.get("order", None)

                # Execute search
                records = odoo.search_read(
                    model,
                    domain=domain,
                    fields=fields,
                    limit=limit,
                    offset=offset,
                    order=order,
                )

                # Get total count for pagination info
                total_count = odoo.search_count(model, domain=domain)

                # Format results
                return format_search_results(
                    model=model,
                    records=records,
                    total_count=total_count,
                    limit=limit,
                    offset=offset,
                    odoo=odoo,
                )

            except OdooConnectionError as e:
                return f"Error searching records: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error searching records: {e}", exc_info=True)
                return f"Error searching records: {e}"

        @self.mcp.resource("odoo://{model}/browse")
        def browse_records(model: str) -> str:
            """Browse records in Odoo.

            Args:
                model: The Odoo model name

            Returns:
                str: Formatted browse results
            """
            try:
                odoo = self.odoo
                default_limit = self.default_limit
                max_limit = self.max_limit
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Access the current resource URI
                ctx = Context.current()
                if ctx is None:
                    return "Error: No context available"

                # Parse query parameters
                uri = ctx.resource.uri
                parsed_url = urllib.parse.urlparse(str(uri))
                query_params = urllib.parse.parse_qs(parsed_url.query)
                params = {
                    k: v[0] if len(v) == 1 else v for k, v in query_params.items()
                }

                # Get IDs to browse
                ids_param = params.get("ids", "[]")
                try:
                    if isinstance(ids_param, str):
                        ids = eval(ids_param)  # Convert string representation to list
                    else:
                        ids = ids_param
                except Exception as e:
                    return f"Invalid IDs format: {e}"

                # Validate IDs
                if not isinstance(ids, list):
                    return "IDs must be a list of integers"

                try:
                    ids = [int(id_) for id_ in ids]
                except (ValueError, TypeError):
                    return "IDs must be integers"

                # Get fields to return
                fields = params.get("fields", None)
                if fields and isinstance(fields, str):
                    try:
                        fields = eval(fields)  # Convert string representation to list
                    except Exception:
                        fields = fields.split(",")

                # Limit number of records to prevent overload
                if len(ids) > max_limit:
                    ids = ids[:max_limit]
                    truncated = True
                else:
                    truncated = False

                # Execute read
                records = odoo.read(model, ids, fields=fields)

                # Format results similar to search
                result = format_search_results(
                    model=model,
                    records=records,
                    total_count=len(ids),
                    limit=len(ids),
                    offset=0,
                    odoo=odoo,
                )

                if truncated:
                    result += f"\n\nNote: Results truncated to {max_limit} records."

                return result

            except OdooConnectionError as e:
                return f"Error browsing records: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error browsing records: {e}", exc_info=True)
                return f"Error browsing records: {e}"

        @self.mcp.resource("odoo://{model}/count")
        def count_records(model: str) -> str:
            """Count records in Odoo.

            Args:
                model: The Odoo model name

            Returns:
                str: Formatted count result
            """
            try:
                odoo = self.odoo
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Access the current resource URI
                ctx = Context.current()
                if ctx is None:
                    return "Error: No context available"

                # Parse query parameters
                uri = ctx.resource.uri
                parsed_url = urllib.parse.urlparse(str(uri))
                query_params = urllib.parse.parse_qs(parsed_url.query)
                params = {
                    k: v[0] if len(v) == 1 else v for k, v in query_params.items()
                }

                # Parse domain from query params
                domain = params.get("domain", "[]")
                try:
                    # Convert domain string to Python list
                    if isinstance(domain, str):
                        domain = eval(
                            domain
                        )  # Safe because we control the input format
                except Exception as e:
                    return f"Invalid domain format: {e}"

                # Execute count
                count = odoo.search_count(model, domain=domain)

                return f"Count for {model}: {count} record(s)"

            except OdooConnectionError as e:
                return f"Error counting records: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error counting records: {e}", exc_info=True)
                return f"Error counting records: {e}"

        @self.mcp.resource("odoo://{model}/fields")
        def get_model_fields(model: str) -> str:
            """Get field information for an Odoo model.

            Args:
                model: The Odoo model name

            Returns:
                str: Formatted fields information
            """
            try:
                odoo = self.odoo
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Get field information
                fields_info = odoo.fields_get(model)

                if not fields_info:
                    return f"No field information available for model: {model}"

                # Format fields information
                lines = [f"# Fields for {model}"]
                lines.append("")

                for field_name, field_info in sorted(fields_info.items()):
                    field_type = field_info.get("type", "unknown")
                    field_string = field_info.get("string", field_name)
                    required = field_info.get("required", False)
                    readonly = field_info.get("readonly", False)

                    # Format field description
                    field_desc = f"- {field_name} ({field_type}): {field_string}"

                    # Add flags if applicable
                    flags = []
                    if required:
                        flags.append("required")
                    if readonly:
                        flags.append("readonly")

                    if flags:
                        field_desc += f" [{', '.join(flags)}]"

                    lines.append(field_desc)

                    # Add relation info if applicable
                    if field_type in ["many2one", "one2many", "many2many"]:
                        relation = field_info.get("relation", "unknown")
                        lines.append(f"  Relation: {relation}")

                return "\n".join(lines)

            except OdooConnectionError as e:
                return f"Error retrieving fields: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error retrieving fields: {e}", exc_info=True)
                return f"Error retrieving fields: {e}"

    def _register_tools(self) -> None:
        """Register all tool handlers."""

        @self.mcp.tool()
        def search_odoo(model: str, domain: str = "[]", limit: int = 10) -> str:
            """Search for records in an Odoo model.

            Args:
                model: The Odoo model name (e.g., "res.partner")
                domain: Search domain in Odoo format (e.g., "[('is_company', '=', True)]")
                limit: Maximum number of records to return

            Returns:
                str: Formatted search results
            """
            try:
                odoo = self.odoo
                max_limit = self.max_limit
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Parse domain
                try:
                    if isinstance(domain, str):
                        domain = eval(domain)  # Convert string representation to list
                except Exception as e:
                    return f"Invalid domain format: {e}"

                # Validate and cap limit
                try:
                    limit = int(limit)
                    limit = min(limit, max_limit)
                except (ValueError, TypeError):
                    limit = min(10, max_limit)

                # Execute search
                records = odoo.search(model, domain, limit=limit)
                total_count = odoo.count(model, domain)

                # Format results
                return format_search_results(
                    model=model,
                    records=records,
                    total_count=total_count,
                    limit=limit,
                    offset=0,
                    domain=domain,
                    odoo=odoo,
                )

            except OdooConnectionError as e:
                return f"Error searching records: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error searching records: {e}", exc_info=True)
                return f"Error searching records: {e}"

        @self.mcp.tool()
        def get_odoo_record(model: str, record_id: int) -> str:
            """Get a specific record from Odoo.

            Args:
                model: The Odoo model name (e.g., "res.partner")
                record_id: Record ID

            Returns:
                str: Formatted record content
            """
            try:
                odoo = self.odoo
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Get record by ID
                records = odoo.read(model, [record_id])

                if not records:
                    return f"Record not found: {model} #{record_id}"

                # Format record for display
                return format_record(model, records[0], odoo)

            except OdooConnectionError as e:
                return f"Error retrieving record: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error retrieving record: {e}", exc_info=True)
                return f"Error retrieving record: {e}"

        @self.mcp.tool()
        def list_odoo_models() -> str:
            """List available Odoo models.

            Returns:
                str: Formatted list of Odoo models
            """
            try:
                odoo = self.odoo
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Get available models
                available_models = odoo.available_models

                if not available_models:
                    return "No models available"

                # Format result
                lines = ["# Available Odoo Models", ""]

                for model in sorted(available_models):
                    lines.append(f"- {model}")

                return "\n".join(lines)

            except OdooConnectionError as e:
                return f"Error listing models: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error listing models: {e}", exc_info=True)
                return f"Error listing models: {e}"

    def start(self) -> None:
        """Start the MCP server.

        This method initializes the server and begins listening for
        client messages using the default transport.
        """
        self.mcp.run()
