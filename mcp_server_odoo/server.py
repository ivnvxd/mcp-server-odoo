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


class MCPOdooServer:
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
                # Convert record_id to integer
                try:
                    record_id_int = int(record_id)
                except (ValueError, TypeError):
                    return f"Invalid record ID: {record_id}. Must be an integer."
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

                # Get sort order
                order = params.get("order", None)

                # Execute search
                ids = odoo.search(
                    model, domain, limit=limit, offset=offset, order=order
                )
                count = odoo.count(model, domain)

                if not ids:
                    return f"No records found for {model} with the given criteria."

                # Read records with fields if specified
                records = odoo.read(model, ids, fields=fields)

                # Format the result
                return format_search_results(
                    model, records, count, limit, offset, domain, self.odoo_url, odoo
                )

            except OdooConnectionError as e:
                return f"Error searching records: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error searching records: {e}", exc_info=True)
                return f"Error searching records: {e}"

        @self.mcp.resource("odoo://{model}/browse")
        def browse_records(model: str) -> str:
            """Browse specific records by IDs.

            Args:
                model: The Odoo model name

            Returns:
                str: Formatted records content
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

                # Parse ids
                ids_param = params.get("ids", "")
                if not ids_param:
                    return "Error: No IDs specified. Use the 'ids' parameter with comma-separated IDs."

                # Convert to list of integers
                try:
                    if isinstance(ids_param, str):
                        ids = [
                            int(id_str.strip())
                            for id_str in ids_param.split(",")
                            if id_str.strip()
                        ]
                    else:
                        ids = [
                            int(id_str.strip())
                            if isinstance(id_str, str)
                            else int(id_str)
                            for id_str in ids_param
                            if id_str
                        ]
                except ValueError:
                    return "Error: Invalid ID format. IDs must be integers."

                # Ensure we have valid IDs to browse
                if not ids:
                    return "Error: No valid IDs provided. IDs must be integers."

                # Get fields to return
                fields = params.get("fields", None)
                if fields and isinstance(fields, str):
                    try:
                        fields = eval(fields)  # Convert string representation to list
                    except Exception:
                        fields = fields.split(",")

                # Read the records
                records = odoo.read(model, ids, fields=fields)

                if not records:
                    return f"No records found for {model} with the given IDs."

                # Format the result as a browsable list
                result = [
                    f"# Browse Results: {model}\n\n",
                    f"Found {len(records)} records.\n\n",
                ]

                # Add each record
                for i, record in enumerate(records):
                    result.append(
                        f"## Record {i + 1}: {record.get('name', f'ID: {record["id"]}')}\n"
                    )
                    result.append(format_record(model, record, odoo))
                    if i < len(records) - 1:
                        result.append("\n---\n\n")

                return "".join(result)

            except OdooConnectionError as e:
                return f"Error browsing records: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error browsing records: {e}", exc_info=True)
                return f"Error browsing records: {e}"

        @self.mcp.resource("odoo://{model}/count")
        def count_records(model: str) -> str:
            """Count records based on domain.

            Args:
                model: The Odoo model name

            Returns:
                str: Count result
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
                count = odoo.count(model, domain)

                # Format domain for display
                domain_str = str(domain)
                if domain_str == "[]":
                    domain_str = "all records"
                else:
                    domain_str = f"domain {domain_str}"

                # Return formatted count
                return f"# Count Result\n\nFound **{count}** records for **{model}** with {domain_str}."

            except OdooConnectionError as e:
                return f"Error counting records: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error counting records: {e}", exc_info=True)
                return f"Error counting records: {e}"

        @self.mcp.resource("odoo://{model}/fields")
        def get_model_fields(model: str) -> str:
            """Get field definitions for a model.

            Args:
                model: The Odoo model name

            Returns:
                str: Formatted field definitions
            """
            try:
                odoo = self.odoo
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Get field definitions
                fields_info = odoo.get_model_fields(model)

                if not fields_info:
                    return f"No field definitions found for {model}."

                # Format the result
                result = [
                    f"# Field Definitions: {model}\n\n",
                    f"Model in Odoo instance at {self.odoo_url}\n\n",
                ]

                # Group fields by type
                fields_by_type = {}
                for field_name, field_info in fields_info.items():
                    field_type = field_info.get("type", "unknown")
                    if field_type not in fields_by_type:
                        fields_by_type[field_type] = []
                    fields_by_type[field_type].append((field_name, field_info))

                # Add field groups
                for field_type, fields in sorted(fields_by_type.items()):
                    result.append(f"## {field_type.capitalize()} Fields\n\n")
                    for field_name, field_info in sorted(fields, key=lambda x: x[0]):
                        # Format field name
                        result.append(f"### {field_name}\n\n")

                        # Basic field info
                        result.append(f"- **Type**: {field_info.get('type')}\n")

                        # Add relation info for relational fields
                        if field_info.get("relation"):
                            result.append(
                                f"- **Relation**: {field_info.get('relation')}\n"
                            )

                            # Add link to related model if it's enabled
                            if field_info.get("relation") in (
                                odoo.available_models or []
                            ):
                                result.append(
                                    f"- **Related Model**: [odoo://{field_info.get('relation')}](odoo://{field_info.get('relation')})\n"
                                )

                        # Add required flag
                        if field_info.get("required"):
                            result.append("- **Required**: Yes\n")

                        # Add readonly flag
                        if field_info.get("readonly"):
                            result.append("- **Read Only**: Yes\n")

                        # Add help/description if available
                        if field_info.get("help"):
                            result.append(f"\n{field_info.get('help')}\n")

                        result.append("\n")

                return "".join(result)

            except OdooConnectionError as e:
                return f"Error retrieving field definitions: {e}"
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Error retrieving field definitions: {e}", exc_info=True)
                return f"Error retrieving field definitions: {e}"

    def _register_tools(self) -> None:
        """Register MCP tools for Odoo interactions."""

        @self.mcp.tool()
        def search_odoo(model: str, domain: str = "[]", limit: int = 10) -> str:
            """Search for records in Odoo.

            Args:
                model: The Odoo model name
                domain: Odoo domain expression as string (default: "[]")
                limit: Maximum number of records to return (default: 10)

            Returns:
                str: Formatted search results
            """
            try:
                odoo = self.odoo
                max_limit = self.max_limit
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Validate model
                if model not in (odoo.available_models or []):
                    available = ", ".join(odoo.available_models or [])
                    return f"Error: Model '{model}' is not enabled for MCP access. Available models: {available}"

                # Parse domain
                try:
                    # Convert domain string to Python list
                    if isinstance(domain, str):
                        domain_list = eval(
                            domain
                        )  # Safe because we control the input format
                except Exception as e:
                    return f"Invalid domain format: {e}"

                # Enforce max limit
                limit = min(limit, max_limit)

                # Execute search - the search method actually does a search_read in odoo_connection.py
                # and returns full records, not just IDs
                records = odoo.search(model, domain_list, limit=limit)
                count = odoo.count(model, domain_list)

                if not records:
                    return f"No records found for {model} with the given criteria."

                # Format the result directly from the records we already have
                return format_search_results(
                    model, records, count, limit, 0, domain_list, odoo
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
                model: The Odoo model name
                record_id: Record ID

            Returns:
                str: Formatted record content
            """
            try:
                odoo = self.odoo
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                # Validate model
                if model not in (odoo.available_models or []):
                    available = ", ".join(odoo.available_models or [])
                    return f"Error: Model '{model}' is not enabled for MCP access. Available models: {available}"

                # Ensure record_id is an integer
                try:
                    record_id_int = int(record_id)
                except (ValueError, TypeError):
                    return f"Invalid record ID: {record_id}. Must be an integer."

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

        @self.mcp.tool()
        def list_odoo_models() -> str:
            """List all available Odoo models.

            Returns:
                    str: List of available models
            """
            try:
                odoo = self.odoo
            except Exception as e:
                return f"Error accessing server data: {e}"

            try:
                if not odoo.available_models:
                    return (
                        "No models available. Check your connection and configuration."
                    )

                # Format the result
                result = [
                    "# Available Odoo Models\n\n",
                    f"Models enabled for MCP access in Odoo instance at {self.odoo_url}:\n\n",
                ]

                # Group models by module
                model_groups = {}
                for model in sorted(odoo.available_models):
                    module = model.split(".")[0]
                    if module not in model_groups:
                        model_groups[module] = []
                    model_groups[module].append(model)

                # List models by module
                for module, models in sorted(model_groups.items()):
                    result.append(f"## {module}\n\n")
                    for model in models:
                        result.append(f"- [{model}](odoo://{model})\n")
                    result.append("\n")

                return "".join(result)

            except Exception as e:
                # Catch any unexpected errors
                logger.error(f"Error listing models: {e}", exc_info=True)
                return f"Error listing models: {e}"

    def start(self) -> None:
        """Start the MCP server with stdio transport.

        This method initializes the server and begins listening for
        client messages using the stdio transport.
        """
        try:
            logger.info("Starting MCP Server for Odoo")

            # Start the FastMCP server using run method
            self.mcp.run()

            logger.info("MCP Server for Odoo shutdown successfully")

        except OdooConnectionError as e:
            logger.error(f"Odoo connection error: {e}")
            raise
        except Exception as e:
            logger.exception(f"Error running MCP server: {e}")
            raise
