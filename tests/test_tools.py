"""Test suite for MCP tools functionality."""

from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import (
    AccessControlError,
    AccessController,
    AccessControlUnavailableError,
    ModelPermissions,
    attachment_scope_domain,
)
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import (
    ValidationError,
)
from mcp_server_odoo.odoo_connection import (
    OdooConnection,
    OdooConnectionError,
    OdooValidationFault,
)
from mcp_server_odoo.tools import _BLOCKED_METHOD_CALLS, OdooToolHandler


class TestOdooToolHandler:
    """Test cases for OdooToolHandler class."""

    @pytest.fixture
    def mock_app(self):
        """Create a mock FastMCP app."""
        app = MagicMock(spec=FastMCP)
        # Store registered tools
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                # Store the function in our tools dict
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        """Create a mock OdooConnection."""
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        """Create a mock AccessController."""
        controller = MagicMock(spec=AccessController)
        return controller

    @pytest.fixture
    def valid_config(self):
        """Create a valid config."""
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        """Create an OdooToolHandler instance."""
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    def test_handler_initialization(
        self, handler, mock_app, mock_connection, mock_access_controller, valid_config
    ):
        """Test handler is properly initialized with correct references."""
        assert handler.app is mock_app
        assert handler.connection is mock_connection
        assert handler.access_controller is mock_access_controller
        assert handler.config is valid_config

    def test_tools_registered(self, handler, mock_app):
        """Test that all tools are registered with FastMCP."""
        expected_tools = {
            "search_records",
            "get_record",
            "get_fields",
            "get_current_context",
            "list_models",
            "create_record",
            "update_record",
            "delete_record",
            "post_message",
            "aggregate_records",
            "list_resource_templates",
        }
        assert set(mock_app._tools.keys()) == expected_tools

    def test_parse_domain_preserves_true_false_in_string_values(self, handler):
        """Python-literal domains keep 'True'/'False' substrings inside values intact."""
        parsed = handler._parse_domain_input("[['name', '=', 'True North']]")
        assert parsed == [["name", "=", "True North"]]

        parsed = handler._parse_domain_input(
            "[('active', '=', False), ('name', 'like', 'False Bay')]"
        )
        assert parsed == [("active", "=", False), ("name", "like", "False Bay")]

    def test_parse_domain_json_string(self, handler):
        parsed = handler._parse_domain_input('[["is_company", "=", true]]')
        assert parsed == [["is_company", "=", True]]

    def test_parse_domain_rejects_non_list_inputs(self, handler):
        for bad in ({"name": "x"}, 42, True):
            with pytest.raises(ValidationError, match="Domain must be a list"):
                handler._parse_domain_input(bad)
        with pytest.raises(ValidationError, match="Invalid domain"):
            handler._parse_domain_input("not a domain at all")

    @pytest.mark.asyncio
    async def test_search_rejects_negative_offset(
        self, handler, mock_connection, mock_access_controller
    ):
        with pytest.raises(ValidationError, match="offset must be >= 0"):
            await handler._handle_search_tool("res.partner", None, None, 10, -5, None)

    @pytest.mark.asyncio
    async def test_aggregate_rejects_negative_offset(
        self, handler, mock_connection, mock_access_controller
    ):
        with pytest.raises(ValidationError, match="offset must be >= 0"):
            await handler._handle_aggregate_records_tool(
                "res.partner", ["country_id"], None, None, None, 10, -1
            )

    @pytest.mark.asyncio
    async def test_search_rejects_offset_over_cap(
        self, handler, mock_connection, mock_access_controller
    ):
        """offset beyond MAX_OFFSET_PAGES pages of the limit is rejected up front."""
        with pytest.raises(ValidationError, match="exceeds the maximum"):
            await handler._handle_search_tool("res.partner", None, None, 10, 10_001, None)
        mock_connection.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_offset_at_cap_accepted(
        self, handler, mock_connection, mock_access_controller
    ):
        """offset exactly at limit * MAX_OFFSET_PAGES is still allowed."""
        mock_connection.search.return_value = []
        mock_connection.search_count.return_value = 0
        mock_connection.read.return_value = []

        result = await handler._handle_search_tool("res.partner", None, ["name"], 10, 10_000, None)
        assert result["offset"] == 10_000

    @pytest.mark.asyncio
    async def test_search_small_limit_deep_offset_allowed(
        self, handler, mock_connection, mock_access_controller
    ):
        """MIN_OFFSET_CAP floor: limit=1, offset=1500 (fetch-the-Nth-record
        paging) must not fail shallower than a larger page size would."""
        mock_connection.search.return_value = []
        mock_connection.search_count.return_value = 0
        mock_connection.read.return_value = []

        result = await handler._handle_search_tool("res.partner", None, ["name"], 1, 1500, None)
        assert result["offset"] == 1500

    @pytest.mark.asyncio
    async def test_search_small_limit_offset_beyond_floor_rejected(
        self, handler, mock_connection, mock_access_controller
    ):
        """Beyond the MIN_OFFSET_CAP floor a small limit is still rejected."""
        with pytest.raises(ValidationError, match="exceeds the maximum"):
            await handler._handle_search_tool("res.partner", None, None, 1, 10_001, None)
        mock_connection.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_aggregate_rejects_offset_over_cap(
        self, handler, mock_connection, mock_access_controller
    ):
        with pytest.raises(ValidationError, match="exceeds the maximum"):
            await handler._handle_aggregate_records_tool(
                "res.partner", ["country_id"], None, None, None, 10, 10_001
            )
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_first_page_still_counts(
        self, handler, mock_connection, mock_access_controller
    ):
        """A short page must NOT be taken as the whole result set.

        Models whose _search post-filters access in Python AFTER the SQL
        limit return fewer rows than `limit` while more matches exist —
        mail.message does exactly that for any non-superuser (a limit=10
        search returns 5 rows while 85 match). Inferring the total from the
        page undercounted it and stopped pagination early.
        """
        mock_connection.search.return_value = [1, 2, 3]
        mock_connection.search_count.return_value = 85
        mock_connection.fields_get.return_value = {}
        mock_connection.read.return_value = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]

        result = await handler._handle_search_tool("res.partner", None, ["name"], 10, 0, None)

        assert result["total"] == 85, "total comes from search_count, not the page"
        assert len(result["records"]) == 2
        mock_connection.search_count.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_full_page_still_counts(
        self, handler, mock_connection, mock_access_controller
    ):
        """A full page may hide more matches — the exact count query still runs."""
        mock_connection.search.return_value = [1, 2]
        mock_connection.search_count.return_value = 50
        mock_connection.fields_get.return_value = {}
        mock_connection.read.return_value = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]

        result = await handler._handle_search_tool("res.partner", None, ["name"], 2, 0, None)

        assert result["total"] == 50
        mock_connection.search_count.assert_called_once_with("res.partner", [])

    @pytest.mark.asyncio
    async def test_search_nonzero_offset_still_counts(
        self, handler, mock_connection, mock_access_controller
    ):
        """With an offset, a short page reveals nothing about the total."""
        mock_connection.search.return_value = [7]
        mock_connection.search_count.return_value = 11
        mock_connection.fields_get.return_value = {}
        mock_connection.read.return_value = [{"id": 7, "name": "G"}]

        result = await handler._handle_search_tool("res.partner", None, ["name"], 10, 10, None)

        assert result["total"] == 11
        mock_connection.search_count.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_serializes_binary_and_datetime_values(
        self, handler, mock_connection, mock_access_controller
    ):
        """Binary/DateTime XML-RPC values are coerced to JSON-safe types."""
        import json as json_mod
        import xmlrpc.client

        binary = xmlrpc.client.Binary(b"\x89PNG fake image")
        stamp = xmlrpc.client.DateTime("20260610T12:00:00")
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.fields_get.return_value = {}
        mock_connection.read.return_value = [
            {"id": 1, "image_1920": binary, "write_date": stamp, "name": "A"}
        ]

        result = await handler._handle_search_tool(
            "res.partner", None, ["name", "image_1920", "write_date"], 10, 0, None
        )

        record = result["records"][0]
        assert isinstance(record["image_1920"], str)  # base64, not Binary
        assert isinstance(record["write_date"], str)
        json_mod.dumps(result["records"])  # must be JSON-serializable end-to-end

    @pytest.mark.asyncio
    async def test_get_record_serializes_binary_values(
        self, handler, mock_connection, mock_access_controller
    ):
        import xmlrpc.client

        mock_connection.read.return_value = [
            {"id": 7, "image_1920": xmlrpc.client.Binary(b"data"), "name": "B"}
        ]
        mock_connection.fields_get.return_value = {}

        result = await handler._handle_get_record_tool("res.partner", 7, ["name", "image_1920"])
        assert isinstance(result.record["image_1920"], str)

    @pytest.mark.asyncio
    async def test_search_empty_fields_list_uses_smart_defaults(
        self, handler, mock_connection, mock_access_controller
    ):
        """fields=[] must trigger smart defaults, never an unfiltered read."""
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "store": True},
            "email": {"type": "char", "store": True},
        }
        mock_connection.read.return_value = [{"id": 1, "name": "A"}]

        await handler._handle_search_tool("res.partner", None, [], 10, 0, None)

        fields_arg = mock_connection.read.call_args[0][2]
        assert fields_arg, "read must receive a concrete field list, not None/[] (= all fields)"

    @pytest.mark.asyncio
    async def test_get_record_empty_fields_list_uses_smart_defaults(
        self, handler, mock_connection, mock_access_controller
    ):
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "store": True},
        }
        mock_connection.read.return_value = [{"id": 1, "name": "A"}]

        result = await handler._handle_get_record_tool("res.partner", 1, [])

        fields_arg = mock_connection.read.call_args[0][2]
        assert fields_arg, "read must receive a concrete field list, not None/[] (= all fields)"
        assert result.metadata is not None  # smart-defaults metadata attached

    @pytest.mark.asyncio
    async def test_list_resource_templates_standard_mode(
        self, handler, mock_connection, mock_access_controller
    ):
        mock_access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "sale.order", "name": "Sales Order"},
        ]

        result = await handler._handle_list_resource_templates_tool()

        assert result["enabled_models"] == ["res.partner", "sale.order"]
        assert result["total_models"] == 2
        assert len(result["templates"]) == 6
        template_uris = [t["uri_template"] for t in result["templates"]]
        assert "odoo://{model}/record/{record_id}/{field}" in template_uris
        assert "odoo://attachment/{attachment_id}" in template_uris

    @pytest.mark.asyncio
    async def test_list_resource_templates_descriptions_match_registrations(
        self, handler, mock_connection, mock_access_controller
    ):
        """The tool's template descriptions mirror the @app.resource
        registrations in resources.py — the strings are hardcoded here to pin
        both surfaces together."""
        mock_access_controller.get_enabled_models.return_value = []

        result = await handler._handle_list_resource_templates_tool()

        descriptions = {t["uri_template"]: t["description"] for t in result["templates"]}
        assert descriptions == {
            "odoo://{model}/record/{record_id}": (
                "Retrieve a specific record from an Odoo model by ID"
            ),
            "odoo://{model}/search": "Search records with default settings (first 10 records)",
            "odoo://{model}/count": "Count all records in an Odoo model",
            "odoo://{model}/fields": "Get field definitions and metadata for an Odoo model",
            "odoo://{model}/record/{record_id}/{field}": (
                "Fetch a binary/image field from an Odoo record (e.g. an image "
                "or stored document) instead of inlining base64"
            ),
            "odoo://attachment/{attachment_id}": "Fetch an ir.attachment by ID",
        }

    @pytest.mark.asyncio
    async def test_list_resource_templates_yolo_mode(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """YOLO mode reports all-models-available, not total_models=0."""
        from mcp_server_odoo.tools import OdooToolHandler

        yolo_config = OdooConfig(
            url="http://localhost:8069",
            username="admin",
            password="admin",
            database="test_db",
            yolo_mode="read",
        )
        handler = OdooToolHandler(mock_app, mock_connection, mock_access_controller, yolo_config)
        mock_access_controller.get_enabled_models.return_value = []

        result = await handler._handle_list_resource_templates_tool()

        assert result["total_models"] is None
        assert "YOLO mode: ALL models are available" in result["note"]

    @pytest.mark.asyncio
    async def test_event_loop_not_blocked_by_connection_calls(
        self, handler, mock_connection, mock_access_controller
    ):
        """Blocking connection calls run in worker threads, keeping the loop responsive."""
        import asyncio
        import time

        def slow_search(*args, **kwargs):
            time.sleep(0.2)  # blocks its worker thread, must not block the loop
            return []

        mock_connection.search.side_effect = slow_search
        mock_connection.read.return_value = []
        mock_connection.fields_get.return_value = {}

        start = time.monotonic()
        search_task = asyncio.create_task(
            handler._handle_search_tool("res.partner", None, None, 10, 0, None)
        )
        # An independent awaitable must make progress while the RPC blocks.
        await asyncio.sleep(0.01)
        heartbeat_elapsed = time.monotonic() - start

        result = await search_task
        assert result["records"] == []
        assert heartbeat_elapsed < 0.15, (
            f"event loop was blocked for {heartbeat_elapsed:.3f}s by a synchronous RPC"
        )

    @pytest.mark.asyncio
    async def test_search_records_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful search_records operation."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 5
        mock_connection.search.return_value = [1, 2, 3]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Record 1"},
            {"id": 2, "name": "Record 2"},
            {"id": 3, "name": "Record 3"},
        ]

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Call the tool
        result = await search_records(
            model="res.partner",
            domain=[["is_company", "=", True]],
            fields=["name", "email"],
            limit=3,
            offset=0,
            order="name asc",
        )

        # Verify result (SearchResult is a Pydantic model)
        assert result.model == "res.partner"
        assert result.total == 5
        assert result.limit == 3
        assert result.offset == 0
        assert len(result.records) == 3

        # Verify calls
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        mock_connection.search_count.assert_called_once_with(
            "res.partner", [["is_company", "=", True]]
        )
        mock_connection.search.assert_called_once_with(
            "res.partner", [["is_company", "=", True]], limit=3, offset=0, order="name asc"
        )

    @pytest.mark.asyncio
    async def test_search_records_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with access denied."""
        # Setup mocks
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner", domain=[], fields=None, limit=10)

        assert "Access denied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_not_authenticated(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records when not authenticated."""
        # Setup mocks
        mock_connection.is_authenticated = False

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner")

        assert "Not authenticated" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_connection_error(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with connection error."""
        # Setup mocks
        mock_connection.search.side_effect = OdooConnectionError("Connection lost")

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner")

        assert "Connection error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_validation_fault_not_labeled_connection_error(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """A business-validation fault surfaces its message without the
        'Connection error' prefix a transport failure gets."""
        mock_connection.search.side_effect = OdooValidationFault("Invalid field 'bogus' in request")

        search_records = mock_app._tools["search_records"]

        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner")

        message = str(exc_info.value)
        assert "Connection error" not in message
        assert "Invalid field 'bogus' in request" in message

    @pytest.mark.asyncio
    async def test_search_records_access_error_fault_surfaces_without_prefix(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """An AccessError fault (classified OdooValidationFault at the
        connection layer) surfaces Odoo's access-rules explanation without
        the 'Connection error'/'Operation failed' transport prefixes — an
        authorization problem must not read as connectivity."""
        denial = (
            "You are not allowed to modify 'Contact' (res.partner). "
            "This operation is allowed for the following groups:\n"
            "- Administration/Settings"
        )
        mock_connection.search.side_effect = OdooValidationFault(denial)

        search_records = mock_app._tools["search_records"]

        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner")

        message = str(exc_info.value)
        assert "Connection error" not in message
        assert "Operation failed" not in message
        assert denial in message

    @pytest.mark.asyncio
    async def test_search_records_with_domain_operators(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with Odoo domain operators like |, &, !."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        # total now always comes from search_count
        mock_connection.search_count.return_value = 3
        mock_connection.search.return_value = [1, 2, 3]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Partner 1", "state_id": [13, "California"]},
            {"id": 2, "name": "Partner 2", "state_id": [13, "California"]},
            {"id": 3, "name": "Partner 3", "state_id": [14, "CA"]},
        ]

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Test with OR operator
        domain_with_or = [
            ["country_id", "=", 233],
            "|",
            ["state_id.name", "ilike", "California"],
            ["state_id.code", "=", "CA"],
        ]

        result = await search_records(
            model="res.partner", domain=domain_with_or, fields=["name", "state_id"], limit=10
        )

        # Verify result (SearchResult is a Pydantic model). The first page came
        # back short of its limit, so the total is derived from it and the
        # extra count query is skipped.
        assert result.model == "res.partner"
        assert result.total == 3
        assert len(result.records) == 3

        # Verify the domain was passed correctly
        mock_connection.search_count.assert_called_once()
        mock_connection.search.assert_called_with(
            "res.partner", domain_with_or, limit=10, offset=0, order=None
        )

    @pytest.mark.asyncio
    async def test_search_records_with_string_domain(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with domain as JSON string (Claude Desktop format)."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [15]
        mock_connection.read.return_value = [
            {"id": 15, "name": "Azure Interior", "is_company": True},
        ]

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Domain as JSON string (as sent by Claude Desktop)
        domain_string = '[["is_company", "=", true], ["name", "ilike", "azure interior"]]'

        result = await search_records(model="res.partner", domain=domain_string, limit=5)

        # Verify result (SearchResult is a Pydantic model)
        assert result.model == "res.partner"
        assert result.total == 1
        assert len(result.records) == 1
        assert result.records[0]["name"] == "Azure Interior"

        # Verify the domain was parsed and passed correctly as a list
        expected_domain = [["is_company", "=", True], ["name", "ilike", "azure interior"]]
        mock_connection.search.assert_called_with(
            "res.partner", expected_domain, limit=5, offset=0, order=None
        )

    @pytest.mark.asyncio
    async def test_search_records_with_python_style_domain(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with Python-style domain string (single quotes)."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [15]
        mock_connection.read.return_value = [
            {"id": 15, "name": "Azure Interior", "is_company": True},
        ]

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Domain with single quotes (Python style)
        domain_string = "[['name', 'ilike', 'azure interior'], ['is_company', '=', True]]"

        result = await search_records(model="res.partner", domain=domain_string, limit=5)

        # Verify result (SearchResult is a Pydantic model)
        assert result.model == "res.partner"
        assert result.total == 1
        assert len(result.records) == 1
        assert result.records[0]["name"] == "Azure Interior"

        # Verify the domain was parsed correctly
        expected_domain = [["name", "ilike", "azure interior"], ["is_company", "=", True]]
        mock_connection.search.assert_called_with(
            "res.partner", expected_domain, limit=5, offset=0, order=None
        )

    @pytest.mark.asyncio
    async def test_search_records_with_invalid_json_domain(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with invalid JSON string domain."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Invalid JSON string
        invalid_domain = '[["is_company", "=", true'  # Missing closing brackets

        # Should raise ValidationError
        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner", domain=invalid_domain, limit=5)

        assert "Invalid domain parameter" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_with_string_fields(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with fields as JSON string."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [15]
        mock_connection.read.return_value = [
            {"id": 15, "name": "Azure Interior", "is_company": True},
        ]

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Fields as JSON string (as sometimes sent by Claude Desktop)
        fields_string = '["name", "is_company", "id"]'

        result = await search_records(
            model="res.partner", domain=[["is_company", "=", True]], fields=fields_string, limit=5
        )

        # Verify result (SearchResult is a Pydantic model)
        assert result.model == "res.partner"
        assert result.total == 1

        # Verify fields were parsed correctly
        mock_connection.read.assert_called_with(
            "res.partner", [15], ["name", "is_company", "id"], {"bin_size": True}
        )

    @pytest.mark.asyncio
    async def test_search_records_with_complex_domain(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with complex nested domain operators."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 2
        mock_connection.search.return_value = [1, 2]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Company A", "is_company": True},
            {"id": 2, "name": "Company B", "is_company": True},
        ]

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Complex domain with nested operators
        complex_domain = [
            "&",
            ["is_company", "=", True],
            "|",
            ["name", "ilike", "Company"],
            ["email", "!=", False],
        ]

        result = await search_records(model="res.partner", domain=complex_domain, limit=5)

        # Verify the result — short first page derives the total, no count query
        assert result.model == "res.partner"
        assert result.total == 2
        assert len(result.records) == 2

        # Verify the domain was passed correctly
        mock_connection.search_count.assert_called_once()
        mock_connection.search.assert_called_with(
            "res.partner", complex_domain, limit=5, offset=0, order=None
        )

    @pytest.mark.asyncio
    async def test_get_record_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful get_record operation."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.read.return_value = [
            {"id": 123, "name": "Test Partner", "email": "test@example.com"}
        ]

        # Get the registered get_record function
        get_record = mock_app._tools["get_record"]

        # Call the tool
        result = await get_record(model="res.partner", record_id=123, fields=["name", "email"])

        # Verify result — get_record returns RecordResult
        assert result.record["id"] == 123
        assert result.record["name"] == "Test Partner"
        assert result.record["email"] == "test@example.com"

        # Verify calls
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        mock_connection.read.assert_called_once_with(
            "res.partner", [123], ["name", "email"], {"bin_size": True}
        )

    @pytest.mark.asyncio
    async def test_get_record_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test get_record when record doesn't exist."""
        # Setup mocks
        mock_connection.read.return_value = []

        # Get the registered get_record function
        get_record = mock_app._tools["get_record"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await get_record(model="res.partner", record_id=999)

        assert "Record not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_record_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test get_record with access denied."""
        # Setup mocks
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        # Get the registered get_record function
        get_record = mock_app._tools["get_record"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await get_record(model="res.partner", record_id=1)

        assert "Access denied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_record_not_authenticated(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test get_record when not authenticated."""
        # Setup mocks
        mock_connection.is_authenticated = False

        # Get the registered get_record function
        get_record = mock_app._tools["get_record"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await get_record(model="res.partner", record_id=1)

        assert "Not authenticated" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_record_connection_error(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test get_record with connection error."""
        # Setup mocks
        mock_connection.read.side_effect = OdooConnectionError("Connection lost")

        # Get the registered get_record function
        get_record = mock_app._tools["get_record"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await get_record(model="res.partner", record_id=1)

        assert "Connection error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_record_oversized_id_rejected(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """An id beyond the XML-RPC 32-bit range fails cleanly before any RPC."""
        get_record = mock_app._tools["get_record"]

        with pytest.raises(ValidationError, match=str(2**31)):
            await get_record(model="res.partner", record_id=2**31)

        assert mock_connection.method_calls == []

    @pytest.mark.asyncio
    async def test_get_record_negative_id_rejected(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """A negative id fails cleanly before any RPC."""
        get_record = mock_app._tools["get_record"]

        with pytest.raises(ValidationError, match="-1"):
            await get_record(model="res.partner", record_id=-1)

        assert mock_connection.method_calls == []

    @pytest.mark.asyncio
    async def test_list_models_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful list_models operation with permissions."""
        # Setup mocks for get_enabled_models
        mock_access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "sale.order", "name": "Sales Order"},
        ]

        # Setup mocks for get_model_permissions
        from mcp_server_odoo.access_control import ModelPermissions

        partner_perms = ModelPermissions(
            model="res.partner",
            enabled=True,
            can_read=True,
            can_write=True,
            can_create=True,
            can_unlink=False,
        )

        order_perms = ModelPermissions(
            model="sale.order",
            enabled=True,
            can_read=True,
            can_write=False,
            can_create=False,
            can_unlink=False,
        )

        # Configure side_effect to return different permissions based on model
        def get_perms(model):
            if model == "res.partner":
                return partner_perms
            elif model == "sale.order":
                return order_perms
            else:
                raise Exception(f"Unknown model: {model}")

        mock_access_controller.get_model_permissions.side_effect = get_perms

        # Get the registered list_models function
        list_models = mock_app._tools["list_models"]

        # Call the tool
        result = await list_models()

        # Verify result structure (ModelsResult is a Pydantic model)
        assert len(result.models) == 2

        # Verify first model (res.partner)
        partner = result.models[0]
        assert partner.model == "res.partner"
        assert partner.name == "Contact"
        assert partner.operations is not None
        assert partner.operations.read is True
        assert partner.operations.write is True
        assert partner.operations.create is True
        assert partner.operations.unlink is False

        # Verify second model (sale.order)
        order = result.models[1]
        assert order.model == "sale.order"
        assert order.name == "Sales Order"
        assert order.operations is not None
        assert order.operations.read is True
        assert order.operations.write is False
        assert order.operations.create is False
        assert order.operations.unlink is False

        # Verify calls
        mock_access_controller.get_enabled_models.assert_called_once()
        assert mock_access_controller.get_model_permissions.call_count == 2

    @pytest.mark.asyncio
    async def test_list_models_with_permission_failures(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models when some models fail to get permissions."""
        # Setup mocks for get_enabled_models
        mock_access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "unknown.model", "name": "Unknown Model"},
        ]

        # Setup mocks for get_model_permissions
        from mcp_server_odoo.access_control import AccessControlError, ModelPermissions

        partner_perms = ModelPermissions(
            model="res.partner",
            enabled=True,
            can_read=True,
            can_write=True,
            can_create=False,
            can_unlink=False,
        )

        # Configure side_effect to fail for unknown model
        def get_perms(model):
            if model == "res.partner":
                return partner_perms
            else:
                raise AccessControlError(f"Model {model} not found")

        mock_access_controller.get_model_permissions.side_effect = get_perms

        # Get the registered list_models function
        list_models = mock_app._tools["list_models"]

        # Call the tool - should not fail even if some models can't get permissions
        result = await list_models()

        # Verify result structure (ModelsResult is a Pydantic model)
        assert len(result.models) == 2

        # Verify first model (res.partner) - should have correct permissions
        partner = result.models[0]
        assert partner.model == "res.partner"
        assert partner.operations.read is True
        assert partner.operations.write is True
        assert partner.operations.create is False
        assert partner.operations.unlink is False

        # Verify second model (unknown.model) - should have all operations as False
        unknown = result.models[1]
        assert unknown.model == "unknown.model"
        assert unknown.operations.read is False
        assert unknown.operations.write is False
        assert unknown.operations.create is False
        assert unknown.operations.unlink is False

    @pytest.mark.asyncio
    async def test_list_models_error(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models with error."""
        # Setup mocks
        mock_access_controller.get_enabled_models.side_effect = Exception("API error")

        # Get the registered list_models function
        list_models = mock_app._tools["list_models"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await list_models()

        assert "Failed to list models" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_omitted_limit_uses_configured_default(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """Omitting limit must fall back to ODOO_MCP_DEFAULT_LIMIT, not a hardcoded value.

        Uses a non-10 default_limit so this test would fail if the tool signature
        hardcoded a default that bypassed config.
        """
        custom_config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
            default_limit=25,
            max_limit=100,
        )
        OdooToolHandler(mock_app, mock_connection, mock_access_controller, custom_config)

        mock_connection.search_count.return_value = 0
        mock_connection.search.return_value = []
        mock_connection.read.return_value = []

        search_records = mock_app._tools["search_records"]

        result = await search_records(model="res.partner")

        assert result.limit == 25
        assert result.offset == 0
        assert result.total == 0
        assert result.records == []

        # Empty first page reveals total=0 — the count query is skipped
        mock_connection.search_count.assert_called_once()
        mock_connection.search.assert_called_with("res.partner", [], limit=25, offset=0, order=None)

    @pytest.mark.asyncio
    async def test_search_records_limit_validation(
        self, handler, mock_connection, mock_access_controller, mock_app, valid_config
    ):
        """Test search_records limit validation."""
        # Setup mocks
        mock_connection.search_count.return_value = 100
        mock_connection.search.return_value = []
        mock_connection.read.return_value = []

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Test with limit exceeding max
        result = await search_records(model="res.partner", limit=500)

        # Should cap to max_limit since 500 > max_limit (SearchResult is a Pydantic model)
        assert result.limit == valid_config.max_limit

        # Test with limit equal to max_limit (boundary)
        result = await search_records(model="res.partner", limit=valid_config.max_limit)
        assert result.limit == valid_config.max_limit

        # Test with negative limit
        result = await search_records(model="res.partner", limit=-1)

        # Should use default limit
        assert result.limit == valid_config.default_limit

    @pytest.mark.asyncio
    async def test_search_records_calls_context_info(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that search_records sends context logging."""
        from unittest.mock import AsyncMock

        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test"}]

        # Create mock context
        ctx = AsyncMock()

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Call with ctx parameter
        await search_records(
            model="res.partner",
            fields=["name"],
            limit=10,
            ctx=ctx,
        )

        # Verify context.info was called with operation name and model
        ctx.info.assert_called()
        first_call_msg = ctx.info.call_args_list[0][0][0]
        assert "res.partner" in first_call_msg
        assert "Searching" in first_call_msg

    @pytest.mark.asyncio
    async def test_get_record_calls_context_info(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that get_record sends context logging."""
        from unittest.mock import AsyncMock

        mock_access_controller.validate_model_access.return_value = None
        mock_connection.read.return_value = [
            {"id": 1, "name": "Test Partner", "email": "test@example.com"}
        ]

        ctx = AsyncMock()
        get_record = mock_app._tools["get_record"]
        await get_record(model="res.partner", record_id=1, fields=["name"], ctx=ctx)

        ctx.info.assert_called()
        first_msg = ctx.info.call_args_list[0][0][0]
        assert "res.partner" in first_msg
        assert "Getting" in first_msg

    @pytest.mark.asyncio
    async def test_list_models_calls_context_info(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that list_models sends context info messages.

        list_models no longer emits per-iteration progress notifications — it now
        emits a single info before the enrichment loop instead. (Terminal progress
        notifications can be flushed after the response under stdio transport,
        which strict MCP clients treat as a protocol violation.)
        """
        from unittest.mock import AsyncMock

        from mcp_server_odoo.access_control import ModelPermissions

        mock_access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
        ]
        mock_access_controller.get_model_permissions.return_value = ModelPermissions(
            model="res.partner",
            enabled=True,
            can_read=True,
            can_write=False,
            can_create=False,
            can_unlink=False,
        )

        ctx = AsyncMock()
        list_models = mock_app._tools["list_models"]
        await list_models(ctx=ctx)

        ctx.info.assert_called()
        first_msg = ctx.info.call_args_list[0][0][0]
        assert "Listing" in first_msg
        info_messages = [call.args[0] for call in ctx.info.call_args_list]
        assert any("Enriching" in msg for msg in info_messages)

    @pytest.mark.asyncio
    async def test_create_record_calls_context_info(
        self, handler, mock_connection, mock_access_controller, mock_app, valid_config
    ):
        """Test that create_record sends context logging."""
        from unittest.mock import AsyncMock

        mock_access_controller.validate_model_access.return_value = None
        mock_connection.create.return_value = 42
        mock_connection.read.return_value = [{"id": 42, "display_name": "New Record"}]
        mock_connection.build_record_url.return_value = "http://localhost:8069/odoo/res.partner/42"

        ctx = AsyncMock()
        create_record = mock_app._tools["create_record"]
        await create_record(model="res.partner", values={"name": "New Record"}, ctx=ctx)

        ctx.info.assert_called()
        first_msg = ctx.info.call_args_list[0][0][0]
        assert "res.partner" in first_msg
        assert "Creating" in first_msg

    @pytest.mark.asyncio
    async def test_search_all_fields_sends_warning(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that searching with __all__ fields sends a warning via context."""
        from unittest.mock import AsyncMock

        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test"}]

        ctx = AsyncMock()
        search_records = mock_app._tools["search_records"]
        await search_records(model="res.partner", fields=["__all__"], limit=10, ctx=ctx)

        ctx.warning.assert_called()
        warning_msg = ctx.warning.call_args_list[0][0][0]
        assert "ALL fields" in warning_msg

        # Verify that __all__ was translated to fields=None (fetch all fields from Odoo)
        mock_connection.read.assert_called_once()
        call_args = mock_connection.read.call_args
        fields_arg = call_args[0][2]  # Third positional argument is fields
        assert fields_arg is None, "Expected fields=None when __all__ is requested"

    @staticmethod
    def _assert_no_terminal_progress(ctx):
        """Assert no progress notification has progress == total.

        Terminal progress under stdio can flush after the response, which strict
        MCP clients treat as a protocol violation.
        """
        for call in ctx.report_progress.call_args_list:
            progress, total = call.args[0], call.args[1]
            assert progress != total, (
                f"Terminal progress notification ({progress}/{total}) - "
                "stdio clients reject post-response notifications"
            )

    @pytest.mark.asyncio
    async def test_context_error_does_not_crash_tool(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that a broken context does not crash the tool operation."""
        from unittest.mock import AsyncMock

        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test"}]

        # Create a context that raises on every call.
        ctx = AsyncMock()
        ctx.info.side_effect = RuntimeError("transport broken")
        ctx.report_progress.side_effect = RuntimeError("transport broken")

        search_records = mock_app._tools["search_records"]
        # Should succeed despite broken context
        result = await search_records(model="res.partner", fields=["name"], limit=10, ctx=ctx)
        assert result.total == 1
        assert len(result.records) == 1
        # search_records reports every step through _ctx_info; the attempts
        # were made and their RuntimeErrors swallowed by its except branch.
        ctx.info.assert_called()
        ctx.report_progress.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_records_emits_no_terminal_progress(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Regression: terminal progress notifications cause stdio client disconnects."""
        from unittest.mock import AsyncMock

        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 5
        mock_connection.search.return_value = [1, 2, 3]
        mock_connection.read.return_value = [{"id": i, "name": f"R{i}"} for i in (1, 2, 3)]

        ctx = AsyncMock()
        search_records = mock_app._tools["search_records"]
        await search_records(model="res.partner", domain=[], fields=None, limit=10, ctx=ctx)

        self._assert_no_terminal_progress(ctx)

    @pytest.mark.asyncio
    async def test_list_models_emits_no_terminal_progress(
        self, handler, mock_access_controller, mock_app, valid_config
    ):
        """Regression: list_models emitted terminal progress on the last loop iter."""
        from unittest.mock import AsyncMock

        from mcp_server_odoo.access_control import ModelPermissions

        # Force standard-mode branch (not YOLO) so the standard list_models
        # path is exercised — that's where the regression originally lived.
        # valid_config is a function-scoped fixture, so this mutation does
        # not leak.
        valid_config.yolo_mode = "off"

        mock_access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Partner"},
            {"model": "res.users", "name": "User"},
        ]
        mock_access_controller.get_model_permissions.return_value = ModelPermissions(
            model="res.partner",
            enabled=True,
            can_read=True,
            can_write=False,
            can_create=False,
            can_unlink=False,
        )

        ctx = AsyncMock()
        list_models = mock_app._tools["list_models"]
        await list_models(ctx=ctx)

        self._assert_no_terminal_progress(ctx)


class TestGetFieldsTool:
    """Test cases for the get_fields schema-discovery tool."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="k",
            database="d",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_curated_default_attributes(
        self, handler, mock_connection, mock_access_controller
    ):
        """Omitted attributes → curated set requested; results sorted by name."""
        from mcp_server_odoo.tools import CURATED_FIELD_ATTRIBUTES

        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name", "required": True, "readonly": False},
            "state": {
                "type": "selection",
                "string": "State",
                "selection": [["draft", "Draft"], ["done", "Done"]],
            },
            "partner_id": {"type": "many2one", "string": "Partner", "relation": "res.partner"},
        }

        result = await handler._handle_get_fields_tool("res.partner", None, None)

        mock_connection.fields_get.assert_called_once_with(
            "res.partner", list(CURATED_FIELD_ATTRIBUTES), None
        )
        assert result.model == "res.partner"
        assert result.total == 3
        assert [f.name for f in result.fields] == ["name", "partner_id", "state"]
        by_name = {f.name: f for f in result.fields}
        assert by_name["name"].required is True
        assert by_name["partner_id"].relation == "res.partner"
        assert by_name["state"].selection == [["draft", "Draft"], ["done", "Done"]]

    @pytest.mark.asyncio
    async def test_explicit_attributes(self, handler, mock_connection, mock_access_controller):
        """attributes=['help','store'] passed through; extras carried on FieldInfo."""
        mock_connection.fields_get.return_value = {
            "name": {"help": "The partner name", "store": True},
        }

        result = await handler._handle_get_fields_tool("res.partner", None, ["help", "store"])

        mock_connection.fields_get.assert_called_once_with("res.partner", ["help", "store"], None)
        field = result.fields[0].model_dump()
        assert field["name"] == "name"
        assert field["help"] == "The partner name"
        assert field["store"] is True

    @pytest.mark.asyncio
    async def test_field_names_narrow_server_call(
        self, handler, mock_connection, mock_access_controller
    ):
        """field_names goes server-side as fields_get's allfields filter;
        unknown names are silently omitted by the server (mocked here)."""
        from mcp_server_odoo.tools import CURATED_FIELD_ATTRIBUTES

        # Server response already narrowed: Odoo skips unknown allfields names
        mock_connection.fields_get.return_value = {
            "name": {"type": "char"},
            "email": {"type": "char"},
        }

        result = await handler._handle_get_fields_tool(
            "res.partner", ["email", "name", "no_such_field"], None
        )

        mock_connection.fields_get.assert_called_once_with(
            "res.partner", list(CURATED_FIELD_ATTRIBUTES), ["email", "name", "no_such_field"]
        )
        assert [f.name for f in result.fields] == ["email", "name"]
        assert result.total == 2

    @pytest.mark.asyncio
    async def test_empty_attributes_treated_like_omitted(
        self, handler, mock_connection, mock_access_controller
    ):
        """attributes=[] ≡ omitted → curated defaults (repo-wide [] convention)."""
        from mcp_server_odoo.tools import CURATED_FIELD_ATTRIBUTES

        mock_connection.fields_get.return_value = {"name": {"type": "char", "string": "Name"}}

        result = await handler._handle_get_fields_tool("res.partner", None, [])

        mock_connection.fields_get.assert_called_once_with(
            "res.partner", list(CURATED_FIELD_ATTRIBUTES), None
        )
        assert result.total == 1

    @pytest.mark.asyncio
    async def test_empty_field_names_treated_like_omitted(
        self, handler, mock_connection, mock_access_controller
    ):
        """field_names=[] ≡ omitted → no server-side filter, every field returned."""
        from mcp_server_odoo.tools import CURATED_FIELD_ATTRIBUTES

        mock_connection.fields_get.return_value = {
            "name": {"type": "char"},
            "email": {"type": "char"},
            "phone": {"type": "char"},
        }

        result = await handler._handle_get_fields_tool("res.partner", [], None)

        mock_connection.fields_get.assert_called_once_with(
            "res.partner", list(CURATED_FIELD_ATTRIBUTES), None
        )
        assert [f.name for f in result.fields] == ["email", "name", "phone"]
        assert result.total == 3

    @pytest.mark.asyncio
    async def test_registered_wrapper_delegates_to_handler(
        self, handler, mock_app, mock_connection, mock_access_controller
    ):
        """The registered get_fields tool wrapper executes the handler."""
        mock_connection.fields_get.return_value = {"name": {"type": "char", "string": "Name"}}

        result = await mock_app._tools["get_fields"](model="res.partner", field_names=["name"])

        assert result.model == "res.partner"
        assert [f.name for f in result.fields] == ["name"]
        assert result.total == 1

    @pytest.mark.asyncio
    async def test_access_denied(self, handler, mock_connection, mock_access_controller):
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Model res.partner is not enabled for MCP access"
        )

        with pytest.raises(ValidationError, match="Access denied"):
            await handler._handle_get_fields_tool("res.partner", None, None)
        mock_connection.fields_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_access_check_unavailable(self, handler, mock_connection, mock_access_controller):
        mock_access_controller.validate_model_access.side_effect = AccessControlUnavailableError(
            "MCP endpoints unreachable"
        )

        with pytest.raises(ValidationError, match="Could not verify access"):
            await handler._handle_get_fields_tool("res.partner", None, None)
        mock_connection.fields_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_authenticated(self, handler, mock_connection, mock_access_controller):
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError, match="Not authenticated"):
            await handler._handle_get_fields_tool("res.partner", None, None)

    @pytest.mark.asyncio
    async def test_connection_error(self, handler, mock_connection, mock_access_controller):
        mock_connection.fields_get.side_effect = OdooConnectionError("Connection lost")

        with pytest.raises(ValidationError, match="Connection error"):
            await handler._handle_get_fields_tool("res.partner", None, None)

    @pytest.mark.asyncio
    async def test_unexpected_error_sanitized(
        self, handler, mock_connection, mock_access_controller
    ):
        """Generic failures land on the sanitize-and-wrap rung."""
        mock_connection.fields_get.side_effect = RuntimeError("boom")

        with pytest.raises(ValidationError, match="Failed to get fields"):
            await handler._handle_get_fields_tool("res.partner", None, None)


class TestRelatedSummaries:
    """Test cases for x2many display-name summaries on get_record."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="k",
            database="d",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_small_one2many_collection_populates_summaries(
        self, handler, mock_connection, mock_access_controller
    ):
        """1..5 ids → display names resolved; ids in the record stay untouched."""
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "relation": "res.partner", "store": True},
        }

        def read_side_effect(*args, **kwargs):
            if args[2] == ["display_name"]:
                return [
                    {"id": 10, "display_name": "Alice"},
                    {"id": 11, "display_name": False},
                ]
            return [{"id": 1, "name": "Parent", "child_ids": [10, 11]}]

        mock_connection.read.side_effect = read_side_effect

        result = await handler._handle_get_record_tool("res.partner", 1, ["name", "child_ids"])

        assert result.record["child_ids"] == [10, 11]  # untouched
        summaries = result.related_summaries["child_ids"]
        assert [s.model_dump() for s in summaries] == [
            {"id": 10, "display_name": "Alice"},
            {"id": 11, "display_name": "id 11"},  # falsy display_name falls back
        ]
        mock_connection.read.assert_any_call("res.partner", [10, 11], ["display_name"])
        mock_access_controller.validate_model_access.assert_any_call("res.partner", "read")

    @pytest.mark.asyncio
    async def test_small_many2many_collection_populates_summaries(
        self, handler, mock_connection, mock_access_controller
    ):
        mock_connection.fields_get.return_value = {
            "tag_ids": {"type": "many2many", "relation": "res.partner.category", "store": True},
        }

        def read_side_effect(*args, **kwargs):
            if args[2] == ["display_name"]:
                return [{"id": 7, "display_name": "VIP"}]
            return [{"id": 1, "name": "Test", "tag_ids": [7]}]

        mock_connection.read.side_effect = read_side_effect

        result = await handler._handle_get_record_tool("res.partner", 1, ["tag_ids"])

        summaries = result.related_summaries["tag_ids"]
        assert [s.model_dump() for s in summaries] == [{"id": 7, "display_name": "VIP"}]

    @pytest.mark.asyncio
    async def test_collection_at_max_boundary_populates_summaries(
        self, handler, mock_connection, mock_access_controller
    ):
        """Exactly MAX_RELATED_ITEMS ids still resolve (boundary is inclusive)."""
        from mcp_server_odoo.tools import MAX_RELATED_ITEMS

        ids = list(range(10, 10 + MAX_RELATED_ITEMS))
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "relation": "res.partner", "store": True},
        }

        def read_side_effect(*args, **kwargs):
            if args[2] == ["display_name"]:
                return [{"id": i, "display_name": f"Child {i}"} for i in ids]
            return [{"id": 1, "name": "Parent", "child_ids": ids}]

        mock_connection.read.side_effect = read_side_effect

        result = await handler._handle_get_record_tool("res.partner", 1, ["child_ids"])

        assert [s.id for s in result.related_summaries["child_ids"]] == ids

    @pytest.mark.asyncio
    async def test_large_collection_skipped(self, handler, mock_connection, mock_access_controller):
        """More than MAX_RELATED_ITEMS ids → no summaries, no extra read."""
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "relation": "res.partner", "store": True},
        }
        mock_connection.read.return_value = [
            {"id": 1, "name": "Parent", "child_ids": [10, 11, 12, 13, 14, 15]}
        ]

        result = await handler._handle_get_record_tool("res.partner", 1, ["child_ids"])

        assert result.related_summaries is None
        assert mock_connection.read.call_count == 1  # no display_name read

    @pytest.mark.asyncio
    async def test_short_relation_read_summarizes_only_returned_rows(
        self, handler, mock_connection, mock_access_controller
    ):
        """A relation read returning fewer rows than requested ids (e.g. one id
        deleted concurrently) summarizes only the rows that came back."""
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "relation": "res.partner", "store": True},
        }

        def read_side_effect(*args, **kwargs):
            if args[2] == ["display_name"]:
                return [{"id": 10, "display_name": "Alice"}]  # id 11 vanished
            return [{"id": 1, "name": "Parent", "child_ids": [10, 11]}]

        mock_connection.read.side_effect = read_side_effect

        result = await handler._handle_get_record_tool("res.partner", 1, ["child_ids"])

        summaries = result.related_summaries["child_ids"]
        assert [s.model_dump() for s in summaries] == [{"id": 10, "display_name": "Alice"}]

    @pytest.mark.asyncio
    async def test_unreadable_relation_skipped_silently(
        self, handler, mock_connection, mock_access_controller
    ):
        """A relation the caller cannot read is omitted — no error raised."""
        mock_connection.fields_get.return_value = {
            "user_ids": {"type": "one2many", "relation": "res.users", "store": True},
        }
        mock_connection.read.return_value = [{"id": 1, "name": "Parent", "user_ids": [3]}]

        def validate_side_effect(model, operation):
            if model == "res.users":
                raise AccessControlError("Model res.users is not enabled for MCP access")

        mock_access_controller.validate_model_access.side_effect = validate_side_effect

        result = await handler._handle_get_record_tool("res.partner", 1, ["user_ids"])

        assert result.related_summaries is None
        assert result.record["user_ids"] == [3]
        assert mock_connection.read.call_count == 1  # relation never read

    @pytest.mark.asyncio
    async def test_empty_collection_skipped(self, handler, mock_connection, mock_access_controller):
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "relation": "res.partner", "store": True},
        }
        mock_connection.read.return_value = [{"id": 1, "name": "Parent", "child_ids": []}]

        result = await handler._handle_get_record_tool("res.partner", 1, ["child_ids"])

        assert result.related_summaries is None
        assert mock_connection.read.call_count == 1

    @pytest.mark.asyncio
    async def test_command_tuple_values_skipped(
        self, handler, mock_connection, mock_access_controller
    ):
        """Non-int items (x2many command triples) are not treated as ids."""
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "relation": "res.partner", "store": True},
        }
        mock_connection.read.return_value = [
            {"id": 1, "name": "Parent", "child_ids": [[6, 0, [10, 11]]]}
        ]

        result = await handler._handle_get_record_tool("res.partner", 1, ["child_ids"])

        assert result.related_summaries is None
        assert result.record["child_ids"] == [[6, 0, [10, 11]]]  # untouched
        assert mock_connection.read.call_count == 1  # no display_name read

    @pytest.mark.asyncio
    async def test_metadata_without_relation_skipped(
        self, handler, mock_connection, mock_access_controller
    ):
        """x2many metadata lacking a relation key → field skipped, no extra read."""
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "store": True},  # no relation
        }
        mock_connection.read.return_value = [{"id": 1, "name": "Parent", "child_ids": [10, 11]}]

        result = await handler._handle_get_record_tool("res.partner", 1, ["child_ids"])

        assert result.related_summaries is None
        assert result.record["child_ids"] == [10, 11]  # untouched
        assert mock_connection.read.call_count == 1  # no display_name read

    @pytest.mark.asyncio
    async def test_non_list_value_skipped(self, handler, mock_connection, mock_access_controller):
        """A non-list x2many value (e.g. False) → field skipped, no extra read."""
        mock_connection.fields_get.return_value = {
            "child_ids": {"type": "one2many", "relation": "res.partner", "store": True},
        }
        mock_connection.read.return_value = [{"id": 1, "name": "Parent", "child_ids": False}]

        result = await handler._handle_get_record_tool("res.partner", 1, ["child_ids"])

        assert result.related_summaries is None
        assert result.record["child_ids"] is False  # untouched
        assert mock_connection.read.call_count == 1  # no display_name read


class TestAggregateRecordsTool:
    """Test cases for the aggregate_records tool."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        # Default to v19 so this class focuses on the formatted_read_group path.
        # The legacy read_group fallback is exercised by TestAggregateRecordsReadGroupFallback.
        connection.get_major_version = MagicMock(return_value=19)
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="k",
            database="d",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_success_with_sum_aggregate(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"date_order:month": "2026-01-01", "__count": 3, "amount_total:sum": 1500.0},
            {"date_order:month": "2026-02-01", "__count": 5, "amount_total:sum": 2300.0},
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="sale.order",
            groupby=["date_order:month"],
            aggregates=["amount_total:sum"],
            domain=[["state", "in", ["sale", "done"]]],
        )

        assert result.model == "sale.order"
        assert result.groupby == ["date_order:month"]
        assert result.aggregates == ["amount_total:sum"]
        assert len(result.groups) == 2
        assert result.groups[0]["amount_total:sum"] == 1500.0
        # 2 groups < effective limit → the page is complete
        assert result.has_more is False
        assert result.next_hint is None

        mock_access_controller.validate_model_access.assert_called_once_with("sale.order", "read")
        mock_connection.execute_kw.assert_called_once_with(
            "sale.order",
            "formatted_read_group",
            [[["state", "in", ["sale", "done"]]]],
            {
                "groupby": ["date_order:month"],
                "aggregates": ["amount_total:sum"],
                # effective limit (10) + 1: peek row detects has_more
                "limit": 11,
                "offset": 0,
            },
        )

    @pytest.mark.asyncio
    async def test_empty_aggregates_defaults_to_count(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When caller omits aggregates, the tool defaults to ['__count'].

        Real Odoo's formatted_read_group does NOT auto-include __count when
        aggregates is empty — the bucket would just contain the groupby keys
        with no quantitative data. We inject __count so callers always get
        useful results.
        """
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"country_id": [1, "Belgium"], "__count": 12},
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(model="res.partner", groupby=["country_id"])

        # Result echoes the effective aggregates (what was actually applied).
        assert result.aggregates == ["__count"]
        # The 4th positional arg of execute_kw is the kwargs dict.
        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        assert passed_kwargs["aggregates"] == ["__count"]

    @pytest.mark.asyncio
    async def test_order_omitted_when_none(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When order=None, the kwarg must be absent from the execute_kw call."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="res.partner", groupby=["country_id"])

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        assert "order" not in passed_kwargs

    @pytest.mark.asyncio
    async def test_order_passed_when_provided(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="res.partner", groupby=["country_id"], order="country_id")

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        assert passed_kwargs["order"] == "country_id"

    @pytest.mark.asyncio
    async def test_domain_string_parsed(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(
            model="sale.order",
            groupby=["partner_id"],
            domain='[["state", "=", "sale"]]',
        )

        # parsed_domain is the first positional arg of args (wrapped in a list)
        passed_args = mock_connection.execute_kw.call_args.args[2]
        assert passed_args == [[["state", "=", "sale"]]]

    @pytest.mark.asyncio
    async def test_unknown_version_uses_formatted_read_group(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When get_major_version returns None, dispatch the modern path.

        The XML-RPC fault on a 17/18 server still surfaces; callers can set
        ODOO_DB or check connection logs to investigate.
        """
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.get_major_version.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="sale.order", groupby=["partner_id"])

        method = mock_connection.execute_kw.call_args.args[1]
        assert method == "formatted_read_group"

    @pytest.mark.asyncio
    async def test_empty_groupby_returns_overall_row(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """groupby=[] collapses to one overall-aggregate row — the
        filtered-count path (search_count is not an MCP tool)."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [{"__count": 42}]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="res.partner", groupby=[], domain=[["is_company", "=", True]]
        )

        assert result.groupby == []
        assert result.aggregates == ["__count"]
        assert result.groups == [{"__count": 42}]
        assert result.has_more is False
        passed = mock_connection.execute_kw.call_args
        assert passed.args[1] == "formatted_read_group"
        assert passed.args[2] == [[["is_company", "=", True]]]
        assert passed.args[3]["groupby"] == []

    @pytest.mark.asyncio
    async def test_omitted_groupby_behaves_like_empty(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """groupby omitted entirely behaves like groupby=[]."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [{"__count": 7}]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(model="res.partner", domain=[["active", "=", True]])

        assert result.groupby == []
        assert result.groups[0]["__count"] == 7

    @pytest.mark.asyncio
    async def test_access_denied(self, handler, mock_connection, mock_access_controller, mock_app):
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        aggregate_records = mock_app._tools["aggregate_records"]
        with pytest.raises(ValidationError) as exc_info:
            await aggregate_records(model="res.partner", groupby=["country_id"])

        assert "Access denied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_not_authenticated(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.is_authenticated = False

        aggregate_records = mock_app._tools["aggregate_records"]
        with pytest.raises(ValidationError) as exc_info:
            await aggregate_records(model="res.partner", groupby=["country_id"])

        assert "Not authenticated" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_limit_defaults_applied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="res.partner", groupby=["country_id"])  # no limit

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        # default_limit (10) from valid_config fixture, + 1 peek row
        assert passed_kwargs["limit"] == 11

    @pytest.mark.asyncio
    async def test_limit_capped_at_max(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="res.partner", groupby=["country_id"], limit=10000)

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        # max_limit (100) from valid_config fixture, + 1 peek row
        assert passed_kwargs["limit"] == 101

    @pytest.mark.asyncio
    async def test_has_more_when_peek_returns_extra_group(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """limit+1 groups back → extra row dropped, has_more + next_hint set."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"country_id": [1, "Belgium"], "__count": 3},
            {"country_id": [2, "Germany"], "__count": 2},
            {"country_id": [3, "France"], "__count": 1},
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(model="res.partner", groupby=["country_id"], limit=2)

        # The peek asked Odoo for limit+1 groups
        assert mock_connection.execute_kw.call_args.args[3]["limit"] == 3
        # Extra row dropped; truncation is signalled with a follow-up hint
        assert len(result.groups) == 2
        assert result.has_more is True
        assert result.next_hint == "aggregate_records with offset=2, limit=2"

    @pytest.mark.asyncio
    async def test_next_hint_advances_from_current_offset(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """next_hint is offset+limit, not bare limit (only visible at offset>0)."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"country_id": [3, "France"], "__count": 2},
            {"country_id": [4, "Spain"], "__count": 2},
            {"country_id": [5, "Italy"], "__count": 1},
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="res.partner", groupby=["country_id"], limit=2, offset=2
        )

        assert result.has_more is True
        assert result.next_hint == "aggregate_records with offset=4, limit=2"

    @pytest.mark.asyncio
    async def test_no_has_more_when_page_complete(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Exactly limit groups back → no truncation, no hint."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"country_id": [1, "Belgium"], "__count": 3},
            {"country_id": [2, "Germany"], "__count": 2},
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(model="res.partner", groupby=["country_id"], limit=2)

        assert len(result.groups) == 2
        assert result.has_more is False
        assert result.next_hint is None

    @pytest.mark.asyncio
    async def test_next_hint_suppressed_at_offset_cap(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Last in-cap page with more groups → has_more True but no hint.

        offset is at the cap max_offset_for(limit) accepts (MIN_OFFSET_CAP here —
        the floor dominates for a small limit), so offset+limit would overrun
        what _validate_offset accepts; the hint is omitted rather than
        suggesting a call that would be rejected.
        """
        from mcp_server_odoo.config import max_offset_for

        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"country_id": [1, "Belgium"], "__count": 3},
            {"country_id": [2, "Germany"], "__count": 2},
            {"country_id": [3, "France"], "__count": 1},
        ]

        cap_offset = max_offset_for(2)  # exactly at the accepted offset cap
        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="res.partner", groupby=["country_id"], limit=2, offset=cap_offset
        )

        assert result.has_more is True
        assert result.next_hint is None

    @pytest.mark.asyncio
    async def test_connection_error_sanitized(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = OdooConnectionError("XML-RPC fault")

        aggregate_records = mock_app._tools["aggregate_records"]
        with pytest.raises(ValidationError) as exc_info:
            await aggregate_records(model="res.partner", groupby=["country_id"])

        assert "Connection error" in str(exc_info.value)


class TestAggregateRecordsReadGroupFallback:
    """Tests for the legacy read_group dispatch path on Odoo 17/18."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        # v18 → triggers the read_group fallback path
        connection.get_major_version = MagicMock(return_value=18)
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="k",
            database="d",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_v18_dispatches_to_read_group(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """v18 routes to read_group, not formatted_read_group."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="sale.order", groupby=["partner_id"])

        method = mock_connection.execute_kw.call_args.args[1]
        assert method == "read_group"

    @pytest.mark.asyncio
    async def test_kwargs_translated(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """aggregates → fields, order → orderby, lazy=False forced."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(
            model="sale.order",
            groupby=["partner_id"],
            aggregates=["amount_total:sum"],
            order="amount_total:sum desc",
        )

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        assert "aggregates" not in passed_kwargs
        assert "order" not in passed_kwargs
        assert passed_kwargs["fields"] == ["amount_total:sum"]
        assert passed_kwargs["orderby"] == "amount_total:sum desc"
        assert passed_kwargs["lazy"] is False

    @pytest.mark.asyncio
    async def test_fallback_peeks_limit_plus_one_and_sets_has_more(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """The legacy read_group path also peeks limit+1 and signals has_more."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"partner_id": [1, "A"], "__count": 3},
            {"partner_id": [2, "B"], "__count": 2},
            {"partner_id": [3, "C"], "__count": 1},
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(model="sale.order", groupby=["partner_id"], limit=2)

        assert mock_connection.execute_kw.call_args.args[3]["limit"] == 3
        assert len(result.groups) == 2
        assert result.has_more is True
        assert result.next_hint == "aggregate_records with offset=2, limit=2"

    @pytest.mark.asyncio
    async def test_count_stripped_from_fields(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """__count must NOT be passed to read_group's fields= (it's implicit)."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        # Caller omits aggregates → tool defaults to ["__count"] → stripped before fields=
        await aggregate_records(model="sale.order", groupby=["partner_id"])

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        assert passed_kwargs["fields"] == []

    @pytest.mark.asyncio
    async def test_count_stripped_keeps_other_aggregates(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(
            model="sale.order",
            groupby=["partner_id"],
            aggregates=["__count", "amount_total:sum"],
        )

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        assert passed_kwargs["fields"] == ["amount_total:sum"]

    @pytest.mark.asyncio
    async def test_response_normalized_domain_rename(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """__domain in raw read_group output is renamed to __extra_domain."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {
                "partner_id": [1, "Acme"],
                "__count": 3,
                "amount_total:sum": 1500.0,
                "__domain": [["partner_id", "=", 1]],
            },
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="sale.order",
            groupby=["partner_id"],
            aggregates=["amount_total:sum"],
        )

        bucket = result.groups[0]
        assert "__domain" not in bucket
        assert bucket["__extra_domain"] == [["partner_id", "=", 1]]
        # Untouched fields pass through
        assert bucket["__count"] == 3
        assert bucket["amount_total:sum"] == 1500.0
        assert bucket["partner_id"] == [1, "Acme"]

    @pytest.mark.asyncio
    async def test_response_normalized_aggregate_key_rename(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """read_group emits 'id' for an 'id:count' aggregate; tool renames it back."""
        mock_access_controller.validate_model_access.return_value = None
        # Simulate raw read_group output: bare field names, no :op suffix.
        mock_connection.execute_kw.return_value = [
            {
                "is_company": False,
                "__count": 28,
                "id": 28,  # raw read_group emits 'id' for 'id:count'
                "__domain": [["is_company", "=", False]],
            },
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="res.partner",
            groupby=["is_company"],
            aggregates=["id:count"],
        )

        bucket = result.groups[0]
        # Bare 'id' key is renamed to 'id:count' to match v19 shape
        assert "id" not in bucket
        assert bucket["id:count"] == 28
        assert bucket["__count"] == 28
        assert bucket["is_company"] is False

    @pytest.mark.asyncio
    async def test_unrequested_fields_filtered_out(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """read_group with empty fields= returns ALL aggregator fields on
        the model. Strip anything the caller didn't ask for."""
        mock_access_controller.validate_model_access.return_value = None
        # Simulate read_group's noisy default response: caller asked for no
        # aggregates (count-only), but Odoo also returns message_bounce,
        # partner_latitude, color, partner_gid as a side effect.
        mock_connection.execute_kw.return_value = [
            {
                "__count": 49,
                "create_date:month": "February 2026",
                "__range": {"create_date:month": {"from": "2026-02-01", "to": "2026-03-01"}},
                "__domain": [["create_date", ">=", "2026-02-01"]],
                "message_bounce": 0,
                "partner_latitude": False,
                "partner_longitude": False,
                "color": 0,
                "partner_gid": False,
            },
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(model="res.partner", groupby=["create_date:month"])

        bucket = result.groups[0]
        # Wanted keys: groupby + metadata
        assert "__count" in bucket
        assert "__extra_domain" in bucket
        assert "__range" in bucket
        assert "create_date:month" in bucket
        # Noise that read_group emitted but caller didn't request
        assert "message_bounce" not in bucket
        assert "partner_latitude" not in bucket
        assert "partner_longitude" not in bucket
        assert "color" not in bucket
        assert "partner_gid" not in bucket

    @pytest.mark.asyncio
    async def test_aggregate_groupby_collision_refused(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """An aggregate over a groupby key is refused on the read_group path.

        read_group returns both under the same bare key, so the aggregate
        clobbers the group's identity AND its drilldown domain (every bucket
        comes back reading `partner_id: 1`). Odoo 19's formatted_read_group
        keys them separately; on older servers, say so instead of returning
        corrupted groups.
        """
        mock_access_controller.validate_model_access.return_value = None

        aggregate_records = mock_app._tools["aggregate_records"]
        with pytest.raises(Exception, match="also a groupby key"):
            await aggregate_records(
                model="sale.order",
                groupby=["partner_id"],
                aggregates=["partner_id:count_distinct"],
            )

        # Refused before the RPC — no corrupt page was ever fetched
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.parametrize(
        "method", ["copy_data", "copy_multi", "update", "get_view", "get_views"]
    )
    @pytest.mark.asyncio
    async def test_primitive_aliases_are_blocked(self, mock_app, method):
        """Aliases Odoo accepts over execute_kw for methods already blocked:
        copy_data is a read, update is write, get_view(s) is fields_get."""
        from mcp_server_odoo.tools import _validate_method_call

        with pytest.raises(ValidationError):
            _validate_method_call("res.partner", method)

    @pytest.mark.asyncio
    async def test_id_aggregate_refused_on_odoo_15_16(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Odoo 15/16 alias `id:<op>` to the bare key "id" and then delete it
        (`del data['id']` in _read_group_format_result), so the aggregate
        silently vanishes. 17/18 keep it."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.get_major_version.return_value = 16

        aggregate_records = mock_app._tools["aggregate_records"]
        with pytest.raises(Exception, match="drops the 'id' key"):
            await aggregate_records(
                model="res.partner", groupby=["country_id"], aggregates=["id:count"]
            )
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_id_aggregate_allowed_on_odoo_17(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.get_major_version.return_value = 17
        mock_connection.execute_kw.return_value = [{"country_id": (1, "BE"), "id": 3}]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="res.partner", groupby=["country_id"], aggregates=["id:count"]
        )
        assert result.groups[0]["id:count"] == 3

    @pytest.mark.asyncio
    async def test_two_aggregates_over_the_same_field_refused(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """read_group keys results by the BARE field name, so sum and avg over
        one field collide: Odoo keeps the last, the rename loop relabels it
        with the FIRST spec, and the second aggregate vanishes silently."""
        mock_access_controller.validate_model_access.return_value = None

        aggregate_records = mock_app._tools["aggregate_records"]
        with pytest.raises(Exception, match="under the bare key"):
            await aggregate_records(
                model="sale.order",
                groupby=["state"],
                aggregates=["amount_total:sum", "amount_total:avg"],
            )

        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_extra_domain_on_legacy_path_is_a_superset(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Legacy read_group sets __domain to AND(caller domain, group
        condition) while v19 emits only the group condition, so the renamed
        __extra_domain carries a SUPERSET on <19. Both are correct under the
        documented contract ("AND it with the domain you passed") because
        re-ANDing is idempotent — this pins the shape so the contract in
        AggregateResult's description stays true.
        """
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {
                "partner_id": [7, "Acme"],
                "__count": 2,
                "__domain": ["&", ["state", "=", "sale"], ["partner_id", "=", 7]],
            }
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="sale.order",
            groupby=["partner_id"],
            domain=[["state", "=", "sale"]],
        )

        bucket = result.groups[0]
        assert bucket["__extra_domain"] == [
            "&",
            ["state", "=", "sale"],
            ["partner_id", "=", 7],
        ]
        assert "__domain" not in bucket, "legacy key is renamed, not duplicated"

    @pytest.mark.asyncio
    async def test_empty_groupby_normalized_overall_row(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """The read_group fallback also supports groupby=[]: one overall
        row with the count, unrequested aggregator noise filtered out."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"__count": 42, "message_bounce": 0, "partner_latitude": False}
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="res.partner", groupby=[], domain=[["is_company", "=", True]]
        )

        passed = mock_connection.execute_kw.call_args
        assert passed.args[1] == "read_group"
        assert passed.args[3]["groupby"] == []
        assert result.groupby == []
        assert result.aggregates == ["__count"]
        # Odoo 15/16 omit __domain on the overall-total row, so the key is
        # filled in as [] to keep it present on every version. (17/18 DO send
        # it — see the next test.)
        assert result.groups == [{"__count": 42, "__extra_domain": []}]
        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_empty_groupby_keeps_server_supplied_domain(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Odoo 17/18 DO send __domain on the overall-total row — carrying the
        caller's own filter — and it is renamed, not overwritten with [].

        Verified live against Odoo 18.0: aggregate_records(res.partner,
        domain=[["is_company","=",True]]) returns that same domain back. The
        drilldown contract stays correct because re-ANDing it is idempotent.
        """
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = [
            {"__count": 11, "__domain": [["is_company", "=", True]]}
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="res.partner", groupby=[], domain=[["is_company", "=", True]]
        )

        assert result.groups == [{"__count": 11, "__extra_domain": [["is_company", "=", True]]}]
        assert "__domain" not in result.groups[0]

    @pytest.mark.asyncio
    async def test_v17_also_dispatches_to_read_group(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.get_major_version.return_value = 17
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="sale.order", groupby=["partner_id"])

        assert mock_connection.execute_kw.call_args.args[1] == "read_group"

    @pytest.mark.asyncio
    async def test_v16_also_dispatches_to_read_group(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """No version floor — read_group exists on every supported Odoo."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.get_major_version.return_value = 16
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="sale.order", groupby=["partner_id"])

        assert mock_connection.execute_kw.call_args.args[1] == "read_group"

    @pytest.mark.asyncio
    async def test_v19_dispatches_to_formatted_read_group(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.get_major_version.return_value = 19
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="sale.order", groupby=["partner_id"])

        assert mock_connection.execute_kw.call_args.args[1] == "formatted_read_group"


class TestYoloListModels:
    """Test cases for list_models in YOLO mode."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def yolo_config(self):
        """Create a YOLO mode config."""
        return OdooConfig(
            url="http://localhost:8069",
            username="admin",
            api_key="test_api_key",
            database="test_db",
            yolo_mode="read",
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, yolo_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, yolo_config)

    @pytest.mark.asyncio
    async def test_yolo_list_models_success(self, handler, mock_connection, mock_app, yolo_config):
        """Test list_models in YOLO mode queries ir.model and returns model list."""
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "sale.order", "name": "Sales Order"},
        ]

        list_models = mock_app._tools["list_models"]
        result = await list_models()

        # Verify search_read was called on ir.model
        mock_connection.search_read.assert_called_once()
        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"
        assert call_args[0][2] == ["model", "name"]

        # Result is a ModelsResult Pydantic model
        assert result.total == 2
        assert len(result.models) == 2
        assert result.models[0].model == "res.partner"
        assert result.models[0].name == "Contact"
        assert result.models[1].model == "sale.order"
        assert result.models[1].name == "Sales Order"

        # Verify YOLO metadata
        assert result.yolo_mode is not None
        assert result.yolo_mode.enabled is True
        assert result.yolo_mode.level == "read"
        assert result.yolo_mode.operations.read is True
        assert result.yolo_mode.operations.write is False

        # Rows carry no per-model operations in YOLO: the flags are global
        # and already reported once under yolo_mode.operations.
        for model_info in result.models:
            assert model_info.operations is None

    @pytest.mark.asyncio
    async def test_yolo_list_models_full_access(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """Test list_models in YOLO 'true' mode reports full access operations."""
        config = OdooConfig(
            url="http://localhost:8069",
            username="admin",
            api_key="test_api_key",
            database="test_db",
            yolo_mode="true",
        )
        OdooToolHandler(mock_app, mock_connection, mock_access_controller, config)

        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
        ]

        list_models = mock_app._tools["list_models"]
        result = await list_models()

        assert result.yolo_mode.level == "true"
        assert result.yolo_mode.operations.write is True
        assert result.yolo_mode.operations.create is True
        assert result.yolo_mode.operations.unlink is True

        # Rows carry no per-model operations in YOLO: the flags are global
        # and already reported once under yolo_mode.operations.
        for model_info in result.models:
            assert model_info.operations is None

    @pytest.mark.asyncio
    async def test_yolo_list_models_error(self, handler, mock_connection, mock_app, yolo_config):
        """Test list_models in YOLO mode returns error dict when search_read fails."""
        mock_connection.search_read.side_effect = Exception("Connection refused")

        list_models = mock_app._tools["list_models"]
        result = await list_models()

        # Should return error structure, not raise
        assert result.models == []
        assert result.total == 0
        assert result.error is not None
        assert "Connection refused" in result.error
        assert result.yolo_mode.enabled is True
        assert result.yolo_mode.operations.read is False


class TestCreateRecordTool:
    """Test cases for create_record tool."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_create_record_success(self, handler, mock_connection, mock_app):
        """Test successful record creation returns CreateResult with correct data."""
        mock_connection.create.return_value = 42
        mock_connection.read.return_value = [{"id": 42, "display_name": "New Partner"}]
        mock_connection.build_record_url.return_value = "http://localhost:8069/odoo/res.partner/42"

        create_record = mock_app._tools["create_record"]
        result = await create_record(model="res.partner", values={"name": "New Partner"})

        assert result.success is True
        assert result.record["id"] == 42
        assert result.record["display_name"] == "New Partner"
        assert result.url == "http://localhost:8069/odoo/res.partner/42"
        assert "42" in result.message

        mock_connection.create.assert_called_once_with("res.partner", {"name": "New Partner"})
        mock_connection.read.assert_called_once_with("res.partner", [42], ["id", "display_name"])

    @pytest.mark.asyncio
    async def test_create_record_empty_values(self, handler, mock_app):
        """Test create_record rejects empty values."""
        create_record = mock_app._tools["create_record"]
        with pytest.raises(ValidationError, match="No values provided"):
            await create_record(model="res.partner", values={})

    @pytest.mark.asyncio
    async def test_create_record_not_authenticated(self, handler, mock_connection, mock_app):
        """Test create_record when not authenticated."""
        mock_connection.is_authenticated = False
        create_record = mock_app._tools["create_record"]
        with pytest.raises(ValidationError, match="Not authenticated"):
            await create_record(model="res.partner", values={"name": "Test"})

    @pytest.mark.asyncio
    async def test_create_record_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test create_record with access denied checks 'create' permission."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )
        create_record = mock_app._tools["create_record"]
        with pytest.raises(ValidationError, match="Access denied"):
            await create_record(model="res.partner", values={"name": "Test"})
        mock_access_controller.validate_model_access.assert_called_once_with(
            "res.partner", "create"
        )

    @pytest.mark.asyncio
    async def test_create_record_connection_error(self, handler, mock_connection, mock_app):
        """Test create_record with connection error."""
        mock_connection.create.side_effect = OdooConnectionError("Connection lost")
        create_record = mock_app._tools["create_record"]
        with pytest.raises(ValidationError, match="Connection error"):
            await create_record(model="res.partner", values={"name": "Test"})

    @pytest.mark.asyncio
    async def test_create_record_oversized_int_rejected_before_rpc(
        self, handler, mock_connection, mock_app
    ):
        """An int outside the XML-RPC 32-bit range anywhere in the values
        fails with a clean ValidationError before any RPC (xmlrpc.client
        would otherwise raise OverflowError mid-marshal)."""
        create_record = mock_app._tools["create_record"]
        with pytest.raises(ValidationError, match="32-bit marshalling range"):
            await create_record(model="res.partner", values={"color": 2**31})
        mock_connection.create.assert_not_called()


class TestUpdateRecordTool:
    """Test cases for update_record tool."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_update_record_success(self, handler, mock_connection, mock_app):
        """Test successful record update with existence check and result read."""
        # First read: existence check returns [{"id": 10}]
        # Second read: post-update fetch returns updated record
        mock_connection.read.side_effect = [
            [{"id": 10}],  # existence check
            [{"id": 10, "display_name": "Updated Partner"}],  # post-update read
        ]
        mock_connection.write.return_value = True
        mock_connection.build_record_url.return_value = "http://localhost:8069/odoo/res.partner/10"

        update_record = mock_app._tools["update_record"]
        result = await update_record(
            model="res.partner", record_id=10, values={"name": "Updated Partner"}
        )

        assert result.success is True
        assert result.record["id"] == 10
        assert result.record["display_name"] == "Updated Partner"
        assert "10" in result.message

        # Verify existence check then post-update read
        assert mock_connection.read.call_count == 2
        mock_connection.read.assert_any_call("res.partner", [10], ["id"])
        mock_connection.read.assert_any_call("res.partner", [10], ["id", "display_name"])
        mock_connection.write.assert_called_once_with(
            "res.partner", [10], {"name": "Updated Partner"}
        )

    @pytest.mark.asyncio
    async def test_update_record_not_found(self, handler, mock_connection, mock_app):
        """Test update_record when record doesn't exist."""
        mock_connection.read.return_value = []  # existence check fails
        update_record = mock_app._tools["update_record"]
        with pytest.raises(ValidationError, match="Record not found"):
            await update_record(model="res.partner", record_id=999, values={"name": "Test"})
        # Should not attempt write
        mock_connection.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_record_oversized_id_rejected(self, handler, mock_connection, mock_app):
        """An id beyond the XML-RPC 32-bit range fails cleanly before any RPC."""
        update_record = mock_app._tools["update_record"]
        with pytest.raises(ValidationError, match=str(2**31)):
            await update_record(model="res.partner", record_id=2**31, values={"name": "Test"})
        assert mock_connection.method_calls == []

    @pytest.mark.asyncio
    async def test_update_record_empty_values(self, handler, mock_app):
        """Test update_record rejects empty values."""
        update_record = mock_app._tools["update_record"]
        with pytest.raises(ValidationError, match="No values provided"):
            await update_record(model="res.partner", record_id=1, values={})

    @pytest.mark.asyncio
    async def test_update_record_nested_oversized_int_rejected_before_rpc(
        self, handler, mock_connection, mock_app
    ):
        """An oversized int nested in a one2many command tuple fails with a
        clean ValidationError before any RPC — the bounds walk recurses into
        lists/tuples/dicts inside the values."""
        update_record = mock_app._tools["update_record"]
        with pytest.raises(ValidationError, match="32-bit marshalling range"):
            await update_record(
                model="res.partner",
                record_id=1,
                values={"child_ids": [(4, 2**31, 0)]},
            )
        mock_connection.read.assert_not_called()
        mock_connection.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_record_access_denied(self, handler, mock_access_controller, mock_app):
        """Test update_record checks 'write' permission."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )
        update_record = mock_app._tools["update_record"]
        with pytest.raises(ValidationError, match="Access denied"):
            await update_record(model="res.partner", record_id=1, values={"name": "Test"})
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "write")

    @pytest.mark.asyncio
    async def test_update_record_not_authenticated(self, handler, mock_connection, mock_app):
        """Test update_record when not authenticated."""
        mock_connection.is_authenticated = False
        update_record = mock_app._tools["update_record"]
        with pytest.raises(ValidationError, match="Not authenticated"):
            await update_record(model="res.partner", record_id=1, values={"name": "Test"})


class TestDeleteRecordTool:
    """Test cases for delete_record tool."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_delete_record_success(self, handler, mock_connection, mock_app):
        """Test successful record deletion with pre-delete info fetch."""
        mock_connection.read.return_value = [{"id": 5, "display_name": "Old Partner"}]
        mock_connection.unlink.return_value = True

        delete_record = mock_app._tools["delete_record"]
        result = await delete_record(model="res.partner", record_id=5)

        assert result.success is True
        assert result.deleted_id == 5
        assert result.deleted_name == "Old Partner"
        assert "Old Partner" in result.message

        mock_connection.read.assert_called_once_with("res.partner", [5], ["id", "display_name"])
        mock_connection.unlink.assert_called_once_with("res.partner", [5])

    @pytest.mark.asyncio
    async def test_delete_record_not_found(self, handler, mock_connection, mock_app):
        """Test delete_record when record doesn't exist."""
        mock_connection.read.return_value = []
        delete_record = mock_app._tools["delete_record"]
        with pytest.raises(ValidationError, match="Record not found"):
            await delete_record(model="res.partner", record_id=999)
        mock_connection.unlink.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_record_access_denied(self, handler, mock_access_controller, mock_app):
        """Test delete_record checks 'unlink' permission."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )
        delete_record = mock_app._tools["delete_record"]
        with pytest.raises(ValidationError, match="Access denied"):
            await delete_record(model="res.partner", record_id=1)
        mock_access_controller.validate_model_access.assert_called_once_with(
            "res.partner", "unlink"
        )

    @pytest.mark.asyncio
    async def test_delete_record_not_authenticated(self, handler, mock_connection, mock_app):
        """Test delete_record when not authenticated."""
        mock_connection.is_authenticated = False
        delete_record = mock_app._tools["delete_record"]
        with pytest.raises(ValidationError, match="Not authenticated"):
            await delete_record(model="res.partner", record_id=1)

    @pytest.mark.asyncio
    async def test_delete_record_connection_error(self, handler, mock_connection, mock_app):
        """Test delete_record with connection error during unlink."""
        mock_connection.read.return_value = [{"id": 1, "display_name": "Test"}]
        mock_connection.unlink.side_effect = OdooConnectionError("Connection lost")
        delete_record = mock_app._tools["delete_record"]
        with pytest.raises(ValidationError, match="Connection error"):
            await delete_record(model="res.partner", record_id=1)

    @pytest.mark.asyncio
    async def test_delete_record_oversized_id_rejected(self, handler, mock_connection, mock_app):
        """An id beyond the XML-RPC 32-bit range fails cleanly before any RPC."""
        delete_record = mock_app._tools["delete_record"]
        with pytest.raises(ValidationError, match=str(2**31)):
            await delete_record(model="res.partner", record_id=2**31)
        assert mock_connection.method_calls == []


class TestPostMessageTool:
    """Test cases for post_message tool."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_post_message_success_default_note(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Happy path: defaults map to mail.mt_note and write permission is checked."""
        mock_connection.execute_kw.return_value = 42

        post_message = mock_app._tools["post_message"]
        result = await post_message(
            model="res.partner",
            record_id=7,
            body="Called customer, will follow up",
        )

        assert result.success is True
        assert result.message_id == 42

        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "write")
        mock_connection.execute_kw.assert_called_once()
        args, kwargs = mock_connection.execute_kw.call_args
        # positional args: model, method, args_list, kwargs_dict
        assert args[0] == "res.partner"
        assert args[1] == "message_post"
        assert args[2] == [7]
        sent_kwargs = args[3]
        assert sent_kwargs["body"] == "Called customer, will follow up"
        assert sent_kwargs["message_type"] == "comment"
        assert sent_kwargs["subtype_xmlid"] == "mail.mt_note"
        # partner_ids / attachment_ids omitted when None
        assert "partner_ids" not in sent_kwargs
        assert "attachment_ids" not in sent_kwargs
        # body_is_html omitted when False (Odoo's default)
        assert "body_is_html" not in sent_kwargs

    @pytest.mark.asyncio
    async def test_post_message_subtype_comment_maps_to_mt_comment(
        self, handler, mock_connection, mock_app
    ):
        """subtype='comment' must map to mail.mt_comment."""
        mock_connection.execute_kw.return_value = 99

        post_message = mock_app._tools["post_message"]
        await post_message(
            model="sale.order", record_id=17, body="Shipping Monday", subtype="comment"
        )

        sent_kwargs = mock_connection.execute_kw.call_args[0][3]
        assert sent_kwargs["subtype_xmlid"] == "mail.mt_comment"

    @pytest.mark.asyncio
    async def test_post_message_empty_body_rejected(self, handler, mock_connection, mock_app):
        """Empty body raises ValidationError before any XML-RPC call."""
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="body must not be empty"):
            await post_message(model="res.partner", record_id=1, body="")
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_message_whitespace_body_rejected(self, handler, mock_connection, mock_app):
        """Whitespace-only body raises ValidationError before any XML-RPC call."""
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="body must not be empty"):
            await post_message(model="res.partner", record_id=1, body="   \n\t ")
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_message_oversized_record_id_rejected(
        self, handler, mock_connection, mock_app
    ):
        """A record id beyond the XML-RPC 32-bit range fails cleanly before any RPC."""
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match=str(2**31)):
            await post_message(model="res.partner", record_id=2**31, body="Hello")
        assert mock_connection.method_calls == []

    @pytest.mark.asyncio
    async def test_post_message_oversized_partner_id_rejected(
        self, handler, mock_connection, mock_app
    ):
        """An oversized partner id in partner_ids fails cleanly before any RPC."""
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match=f"partner ID {2**31}"):
            await post_message(
                model="res.partner", record_id=1, body="Hello", partner_ids=[5, 2**31]
            )
        assert mock_connection.method_calls == []

    @pytest.mark.asyncio
    async def test_post_message_oversized_attachment_id_rejected(
        self, handler, mock_connection, mock_app
    ):
        """An oversized attachment id in attachment_ids fails cleanly before any RPC."""
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match=f"attachment ID {2**31}"):
            await post_message(
                model="res.partner", record_id=1, body="Hello", attachment_ids=[2**31]
            )
        assert mock_connection.method_calls == []

    @pytest.mark.asyncio
    async def test_post_message_body_is_html_forwarded(self, handler, mock_connection, mock_app):
        """body_is_html=True forwards the kwarg so Odoo preserves HTML markup."""
        mock_connection.execute_kw.return_value = 1

        post_message = mock_app._tools["post_message"]
        await post_message(
            model="res.partner",
            record_id=1,
            body="<p>Bold <b>text</b></p>",
            body_is_html=True,
        )

        sent_kwargs = mock_connection.execute_kw.call_args[0][3]
        assert sent_kwargs["body_is_html"] is True

    @pytest.mark.asyncio
    async def test_post_message_subject_forwarded(self, handler, mock_connection, mock_app):
        """subject= is forwarded to message_post when set."""
        mock_connection.execute_kw.return_value = 1

        post_message = mock_app._tools["post_message"]
        await post_message(model="res.partner", record_id=1, body="Hi", subject="Follow-up")

        sent_kwargs = mock_connection.execute_kw.call_args[0][3]
        assert sent_kwargs["subject"] == "Follow-up"

    @pytest.mark.asyncio
    async def test_post_message_subject_omitted_when_none(self, handler, mock_connection, mock_app):
        """subject is absent from message_post kwargs when not provided."""
        mock_connection.execute_kw.return_value = 1

        post_message = mock_app._tools["post_message"]
        await post_message(model="res.partner", record_id=1, body="Hi")

        sent_kwargs = mock_connection.execute_kw.call_args[0][3]
        assert "subject" not in sent_kwargs

    @pytest.mark.asyncio
    async def test_post_message_partner_and_attachment_ids_passed_through(
        self, handler, mock_connection, mock_app
    ):
        """When provided, partner_ids and attachment_ids appear in kwargs."""
        mock_connection.execute_kw.return_value = 1

        post_message = mock_app._tools["post_message"]
        await post_message(
            model="res.partner",
            record_id=1,
            body="Hi",
            partner_ids=[5, 6],
            attachment_ids=[10],
        )

        sent_kwargs = mock_connection.execute_kw.call_args[0][3]
        assert sent_kwargs["partner_ids"] == [5, 6]
        assert sent_kwargs["attachment_ids"] == [10]

    @pytest.mark.asyncio
    async def test_post_message_no_mail_thread_has_no_attribute_branch(
        self, handler, mock_connection, mock_app
    ):
        """'has no attribute' fault → ValidationError mentioning mail.thread."""
        mock_connection.execute_kw.side_effect = OdooConnectionError(
            "'res.country' object has no attribute 'message_post'"
        )
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="mail.thread"):
            await post_message(model="res.country", record_id=1, body="hi")

    @pytest.mark.asyncio
    async def test_post_message_no_mail_thread_attribute_error_branch(
        self, handler, mock_connection, mock_app
    ):
        """XML-RPC fault wrapping AttributeError → ValidationError mentioning mail.thread."""
        mock_connection.execute_kw.side_effect = OdooConnectionError(
            "XML-RPC fault: AttributeError on res.country: message_post not found"
        )
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="mail.thread"):
            await post_message(model="res.country", record_id=1, body="hi")

    @pytest.mark.asyncio
    async def test_post_message_no_mail_thread_method_does_not_exist_branch(
        self, handler, mock_connection, mock_app
    ):
        """Odoo 19 wording 'method ... does not exist' → ValidationError mentioning mail.thread."""
        mock_connection.execute_kw.side_effect = OdooConnectionError(
            "Operation failed: Internal Server Error in The method 'res.country.message_post' does not exist"
        )
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="mail.thread"):
            await post_message(model="res.country", record_id=1, body="hi")

    @pytest.mark.asyncio
    async def test_post_message_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Access denial against 'write' permission surfaces as ValidationError."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError("no write")
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="Access denied"):
            await post_message(model="res.partner", record_id=1, body="hi")
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "write")

    @pytest.mark.asyncio
    async def test_post_message_return_value_list_coerced(self, handler, mock_connection, mock_app):
        """execute_kw returning [42] is coerced to message_id=42."""
        mock_connection.execute_kw.return_value = [42]
        post_message = mock_app._tools["post_message"]
        result = await post_message(model="res.partner", record_id=1, body="hi")
        assert result.message_id == 42

    @pytest.mark.asyncio
    async def test_post_message_return_value_false_rejected(
        self, handler, mock_connection, mock_app
    ):
        """execute_kw returning False raises ValidationError."""
        mock_connection.execute_kw.return_value = False
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="Unexpected return"):
            await post_message(model="res.partner", record_id=1, body="hi")

    @pytest.mark.asyncio
    async def test_post_message_return_value_dict_rejected(
        self, handler, mock_connection, mock_app
    ):
        """execute_kw returning a non-int/non-list-of-int raises ValidationError."""
        mock_connection.execute_kw.return_value = {}
        post_message = mock_app._tools["post_message"]
        with pytest.raises(ValidationError, match="Unexpected return"):
            await post_message(model="res.partner", record_id=1, body="hi")


class TestListModelsTool:
    """Test YOLO-mode list_models which has a completely separate code path."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def yolo_read_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            username="admin",
            password="admin",
            database="test_db",
            yolo_mode="read",
        )

    @pytest.fixture
    def yolo_full_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            username="admin",
            password="admin",
            database="test_db",
            yolo_mode="true",
        )

    @pytest.fixture
    def yolo_handler(self, mock_app, mock_connection, mock_access_controller, yolo_read_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, yolo_read_config)

    @pytest.mark.asyncio
    async def test_list_models_yolo_read_mode(self, yolo_handler, mock_connection, mock_app):
        """Test list_models in YOLO read mode queries ir.model directly."""
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "sale.order", "name": "Sales Order"},
        ]

        list_models = mock_app._tools["list_models"]
        result = await list_models()

        # YOLO mode returns a ModelsResult with yolo_mode as YoloModeInfo
        assert result.yolo_mode is not None
        assert result.yolo_mode.enabled is True
        assert result.yolo_mode.level == "read"
        assert result.yolo_mode.operations.read is True
        assert result.yolo_mode.operations.write is False

        assert result.total == 2
        assert result.models[0].model == "res.partner"
        assert result.models[1].model == "sale.order"

        # Verify ir.model was queried directly
        mock_connection.search_read.assert_called_once()
        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"

    @pytest.mark.asyncio
    async def test_list_models_yolo_full_mode(
        self, mock_app, mock_connection, mock_access_controller, yolo_full_config
    ):
        """Test list_models in YOLO full mode enables write operations."""
        OdooToolHandler(mock_app, mock_connection, mock_access_controller, yolo_full_config)
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
        ]

        list_models = mock_app._tools["list_models"]
        result = await list_models()

        assert result.yolo_mode.level == "true"
        assert result.yolo_mode.operations.read is True
        assert result.yolo_mode.operations.write is True
        assert result.yolo_mode.operations.create is True
        assert result.yolo_mode.operations.unlink is True

    @pytest.mark.asyncio
    async def test_list_models_yolo_query_error(self, yolo_handler, mock_connection, mock_app):
        """Test list_models in YOLO mode when ir.model query fails."""
        mock_connection.search_read.side_effect = Exception("Database error")

        list_models = mock_app._tools["list_models"]
        result = await list_models()

        # Should return error structure, not raise
        assert result.yolo_mode.operations.read is False
        assert result.models == []
        assert result.total == 0


class TestSearchRecordReturnValue:
    """Test that search_records return value is checked, not just mock calls."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_search_with_complex_domain_checks_result(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with complex domain verifies the actual return value."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 2
        mock_connection.search.return_value = [1, 2]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Company A", "is_company": True},
            {"id": 2, "name": "Company B", "is_company": True},
        ]

        search_records = mock_app._tools["search_records"]
        complex_domain = [
            "&",
            ["is_company", "=", True],
            "|",
            ["name", "ilike", "Company"],
            ["email", "!=", False],
        ]
        result = await search_records(model="res.partner", domain=complex_domain, limit=5)

        # Actually verify the return value — the short first page (2 < limit 5)
        # derives the total directly, skipping the count query.
        assert result.model == "res.partner"
        assert result.total == 2
        assert len(result.records) == 2
        assert result.records[0]["name"] == "Company A"
        assert result.records[1]["name"] == "Company B"
        assert result.limit == 5
        assert result.offset == 0


class TestToolEdgeCases:
    """Test edge cases and error paths in tool handlers."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_list_models_access_controller_failure(
        self, handler, mock_access_controller, mock_app
    ):
        """Test list_models raises ValidationError when get_enabled_models raises RuntimeError."""
        mock_access_controller.get_enabled_models.side_effect = RuntimeError(
            "API endpoint unreachable"
        )

        list_models = mock_app._tools["list_models"]

        with pytest.raises(ValidationError) as exc_info:
            await list_models()

        assert "Failed to list models" in str(exc_info.value)
        assert "API endpoint unreachable" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_domain_not_list(self, handler, mock_access_controller, mock_app):
        """Test search_records rejects a JSON string that parses to a dict instead of list."""
        search_records = mock_app._tools["search_records"]

        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner", domain='{"key": "value"}', limit=10)

        assert "Domain must be a list, got dict" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_fields_not_list(self, handler, mock_access_controller, mock_app):
        """Test search_records rejects a JSON string that parses to a str instead of list."""
        search_records = mock_app._tools["search_records"]

        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner", fields='"name"', limit=10)

        assert "Fields must be a list, got str" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_record_generic_exception(self, handler, mock_connection, mock_app):
        """Test create_record wraps unexpected RuntimeError in ValidationError."""
        mock_connection.create.side_effect = RuntimeError("unexpected")

        create_record = mock_app._tools["create_record"]

        with pytest.raises(ValidationError) as exc_info:
            await create_record(model="res.partner", values={"name": "Test"})

        assert "Failed to create record" in str(exc_info.value)
        assert "unexpected" in str(exc_info.value).lower()


class TestParseDomainInput:
    """Direct unit tests for OdooToolHandler._parse_domain_input."""

    @pytest.fixture
    def handler(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}
        app.tool = lambda **kwargs: lambda func: app._tools.setdefault(func.__name__, func)
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        access = MagicMock(spec=AccessController)
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        return OdooToolHandler(app, connection, access, config)

    def test_none_returns_empty_list(self, handler):
        assert handler._parse_domain_input(None) == []

    def test_list_passthrough(self, handler):
        domain = [["is_company", "=", True]]
        assert handler._parse_domain_input(domain) is domain

    def test_valid_json_string(self, handler):
        result = handler._parse_domain_input('[["is_company", "=", true]]')
        assert result == [["is_company", "=", True]]

    def test_python_style_single_quotes(self, handler):
        result = handler._parse_domain_input("[['name', 'ilike', 'foo']]")
        assert result == [["name", "ilike", "foo"]]

    def test_python_capitalized_booleans(self, handler):
        result = handler._parse_domain_input("[['active', '=', True]]")
        assert result == [["active", "=", True]]

    def test_python_literal_eval_fallback(self, handler):
        # Mixed quotes that fail JSON but parse as Python literal
        result = handler._parse_domain_input("[('name', '=', \"O'Reilly\")]")
        assert result == [("name", "=", "O'Reilly")]

    def test_invalid_string_raises(self, handler):
        with pytest.raises(ValidationError) as exc_info:
            handler._parse_domain_input("not a domain at all {[")
        assert "Invalid domain parameter" in str(exc_info.value)

    def test_non_list_string_raises(self, handler):
        with pytest.raises(ValidationError) as exc_info:
            handler._parse_domain_input('{"key": "value"}')
        assert "Domain must be a list, got dict" in str(exc_info.value)

    @pytest.mark.parametrize(
        "domain",
        [
            [],
            [["id", ">", 0]],
            [["id", ">", 0], ["active", "=", True]],
            ["&", ["id", ">", 0], ["active", "=", True]],
            ["|", ["id", ">", 0], ["active", "=", True]],
            ["!", ["id", ">", 0]],
            ["|", "!", ["id", ">", 0], ["active", "=", True]],
            ["|", ["id", ">", 0], "&", ["a", "=", 1], ["b", "=", 2]],
        ],
    )
    def test_balanced_domains_pass_through(self, handler, domain):
        assert handler._parse_domain_input(domain) is domain

    @pytest.mark.parametrize(
        "domain",
        [
            ["|", ["id", ">", 0]],
            ["&", ["id", ">", 0]],
            ["|"],
            ["&", "|", ["id", ">", 0], ["active", "=", True]],
            ["!"],
            ["!", "|", ["id", ">", 0]],
        ],
    )
    def test_unbalanced_domain_rejected(self, handler, domain):
        """A dangling operator would take an appended internal scope as its
        own operand — see _check_domain_balance. Odoo rejects these outright,
        so nothing downstream reports them either.
        """
        with pytest.raises(ValidationError) as exc_info:
            handler._parse_domain_input(domain)
        assert "Unbalanced domain" in str(exc_info.value)

    def test_unbalanced_domain_rejected_from_string(self, handler):
        """The string branch parses separately from the list branch."""
        with pytest.raises(ValidationError) as exc_info:
            handler._parse_domain_input('["|", ["id", ">", 0]]')
        assert "Unbalanced domain" in str(exc_info.value)

    @pytest.mark.parametrize("token", ["and", "OR", "", 1, None])
    def test_unknown_domain_term_rejected(self, handler, token):
        with pytest.raises(ValidationError) as exc_info:
            handler._parse_domain_input([token, ["id", ">", 0]])
        assert "Invalid domain term" in str(exc_info.value)


class TestCallModelMethodTool:
    """Test cases for the gated call_model_method tool."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        connection.performance_manager = MagicMock()
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    def _config(self, *, yolo_mode: str = "off", enable: bool = False) -> OdooConfig:
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            username="admin",
            yolo_mode=yolo_mode,
            enable_method_calls=enable,
        )

    def _enabled_handler(self, mock_app, mock_connection, mock_access_controller):
        return OdooToolHandler(
            mock_app,
            mock_connection,
            mock_access_controller,
            self._config(yolo_mode="true", enable=True),
        )

    # --- Registration gating ---

    def test_tool_not_registered_when_disabled_default(
        self, mock_app, mock_connection, mock_access_controller
    ):
        OdooToolHandler(mock_app, mock_connection, mock_access_controller, self._config())
        assert "call_model_method" not in mock_app._tools

    def test_tool_not_registered_when_yolo_read_even_with_enable(
        self, mock_app, mock_connection, mock_access_controller
    ):
        OdooToolHandler(
            mock_app,
            mock_connection,
            mock_access_controller,
            self._config(yolo_mode="read", enable=True),
        )
        assert "call_model_method" not in mock_app._tools

    def test_tool_not_registered_when_yolo_off_even_with_enable(
        self, mock_app, mock_connection, mock_access_controller
    ):
        OdooToolHandler(
            mock_app,
            mock_connection,
            mock_access_controller,
            self._config(yolo_mode="off", enable=True),
        )
        assert "call_model_method" not in mock_app._tools

    def test_tool_not_registered_when_yolo_full_without_enable(
        self, mock_app, mock_connection, mock_access_controller
    ):
        OdooToolHandler(
            mock_app,
            mock_connection,
            mock_access_controller,
            self._config(yolo_mode="true", enable=False),
        )
        assert "call_model_method" not in mock_app._tools

    def test_tool_registered_when_both_flags_on(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        assert "call_model_method" in mock_app._tools

    # --- Happy path ---

    @pytest.mark.asyncio
    async def test_happy_path_native_args(self, mock_app, mock_connection, mock_access_controller):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = {"type": "ir.actions.act_window_close"}

        call_model_method = mock_app._tools["call_model_method"]
        result = await call_model_method(
            model="account.move",
            method="action_post",
            arguments=[[42]],
        )

        assert result.success is True
        assert result.result == {"type": "ir.actions.act_window_close"}
        assert result.message == "Successfully called account.move.action_post"
        mock_access_controller.validate_model_access.assert_called_once_with(
            "account.move", "write"
        )
        mock_connection.execute_kw.assert_called_once_with(
            "account.move", "action_post", [[42]], {}
        )

    @pytest.mark.asyncio
    async def test_oversized_record_id_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """An oversized id in the recordset-ids argument fails before any RPC."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)

        call_model_method = mock_app._tools["call_model_method"]
        with pytest.raises(ValidationError, match=str(2**31)):
            await call_model_method(
                model="account.move", method="action_post", arguments=[[42, 2**31]]
            )

        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_and_negative_ints_pass_validation(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """0/negatives-within-range are legit business-method args, not rejected."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True

        await mock_app._tools["call_model_method"](
            model="res.partner", method="some_method", arguments=[[0, -5, 3]]
        )

        mock_connection.execute_kw.assert_called_once_with(
            "res.partner", "some_method", [[0, -5, 3]], {}
        )

    @pytest.mark.asyncio
    async def test_int_above_marshalling_range_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """An int above 2**31-1 exceeds XML-RPC marshalling and fails before RPC."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)

        with pytest.raises(ValidationError, match=str(2**31)):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="some_method", arguments=[[2**31]]
            )

        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_int_below_marshalling_range_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """An int below -2**31 exceeds XML-RPC marshalling and fails before RPC."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)

        with pytest.raises(ValidationError, match=str(-(2**31) - 1)):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="some_method", arguments=[[-(2**31) - 1]]
            )

        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversized_int_in_later_positional_arg_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """The bounds walk covers every positional arg, not just the ids list."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)

        with pytest.raises(ValidationError, match=r"arguments\[1\]"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="some_method", arguments=[[1], 2**31]
            )

        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversized_int_in_nested_list_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """The bounds walk recurses into nested containers."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)

        with pytest.raises(ValidationError, match=str(2**31)):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="some_method", arguments=[[1], ["batch", [2**31]]]
            )

        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_oversized_int_in_kwargs_value_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """keyword_arguments values are bounds-checked too (incl. nested dicts)."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)

        with pytest.raises(ValidationError, match=r"keyword_arguments"):
            await mock_app._tools["call_model_method"](
                model="res.partner",
                method="some_method",
                arguments=[[1]],
                keyword_arguments={"context": {"big": 2**31}},
            )

        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_native_kwargs_passed_through(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True

        await mock_app._tools["call_model_method"](
            model="res.partner",
            method="some_action",
            arguments=[[1]],
            keyword_arguments={"context": {"lang": "en_US"}},
        )

        mock_connection.execute_kw.assert_called_once_with(
            "res.partner", "some_action", [[1]], {"context": {"lang": "en_US"}}
        )

    @pytest.mark.asyncio
    async def test_json_string_arguments_parsed(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True

        await mock_app._tools["call_model_method"](
            model="sale.order",
            method="action_confirm",
            arguments="[[7]]",
        )

        mock_connection.execute_kw.assert_called_once_with(
            "sale.order", "action_confirm", [[7]], {}
        )

    @pytest.mark.asyncio
    async def test_json_string_kwargs_parsed(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True

        await mock_app._tools["call_model_method"](
            model="res.partner",
            method="x",
            arguments=[[1]],
            keyword_arguments='{"context": {}}',
        )

        mock_connection.execute_kw.assert_called_once_with(
            "res.partner", "x", [[1]], {"context": {}}
        )

    @pytest.mark.asyncio
    async def test_arguments_default_to_empty_list_when_none(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True

        await mock_app._tools["call_model_method"](model="res.partner", method="some_method")

        mock_connection.execute_kw.assert_called_once_with("res.partner", "some_method", [], {})

    # --- Argument-parsing error cases ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_arg, expected",
        [
            ("not json {[", "Invalid arguments parameter"),
            ("null", "must be a list"),
            ("42", "must be a list"),
            ('"foo"', "must be a list"),
            ('{"k": 1}', "must be a list"),
        ],
    )
    async def test_invalid_arguments_string(
        self, mock_app, mock_connection, mock_access_controller, bad_arg, expected
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match=expected):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="x", arguments=bad_arg
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_kwargs, expected",
        [
            ("not json", "Invalid keyword_arguments"),
            ("null", "must be a dict"),
            ("[1,2]", "must be a dict"),
        ],
    )
    async def test_invalid_keyword_arguments_string(
        self, mock_app, mock_connection, mock_access_controller, bad_kwargs, expected
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match=expected):
            await mock_app._tools["call_model_method"](
                model="res.partner",
                method="x",
                arguments=[[1]],
                keyword_arguments=bad_kwargs,
            )

    @pytest.mark.asyncio
    async def test_unsupported_native_type_for_arguments(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match="arguments must be a list or JSON-string"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="x", arguments=42
            )

    @pytest.mark.asyncio
    async def test_unsupported_native_type_for_kwargs(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(
            ValidationError, match="keyword_arguments must be a dict or JSON-string"
        ):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="x", arguments=[[1]], keyword_arguments=42
            )

    # --- Validation guards ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_method",
        [
            "_compute_x",  # leading underscore
            "__init__",  # dunder
            "foo._private",  # dotted
            "foo.bar",  # dotted (even fully public-looking)
            "9bad",  # leading digit
            "has-dash",  # invalid identifier char
            "with space",  # whitespace inside
        ],
    )
    async def test_non_public_method_rejected(
        self, mock_app, mock_connection, mock_access_controller, bad_method
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match="public ASCII Python identifiers"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method=bad_method, arguments=[[1]]
            )
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "good_method",
        [
            "action_post",
            "toggle_active",
            "action_confirm",
            "message_subscribe",
            "x",  # single-letter still valid
        ],
    )
    async def test_public_method_accepted(
        self, mock_app, mock_connection, mock_access_controller, good_method
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True
        await mock_app._tools["call_model_method"](
            model="res.partner", method=good_method, arguments=[[1]]
        )
        mock_connection.execute_kw.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_model_rejected(self, mock_app, mock_connection, mock_access_controller):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match="model must not be empty"):
            await mock_app._tools["call_model_method"](
                model="   ", method="action_post", arguments=[[1]]
            )

    @pytest.mark.asyncio
    async def test_empty_method_rejected(self, mock_app, mock_connection, mock_access_controller):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match="method must not be empty"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="", arguments=[[1]]
            )

    @pytest.mark.asyncio
    async def test_access_denied_translates(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_access_controller.validate_model_access.side_effect = AccessControlError("denied")
        with pytest.raises(ValidationError, match="Access denied"):
            await mock_app._tools["call_model_method"](
                model="sale.order", method="action_confirm", arguments=[[1]]
            )

    @pytest.mark.asyncio
    async def test_connection_error_translates(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.side_effect = OdooConnectionError("boom")
        with pytest.raises(ValidationError, match="Connection error"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="x", arguments=[[1]]
            )

    @pytest.mark.asyncio
    async def test_void_return_surfaces_as_success_with_none(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """``execute_kw`` returning None (e.g. toggle_active) wraps as success(result=None).

        The connection layer already translates Odoo's "cannot marshal None" fault
        into a plain ``None`` return; see ``test_odoo_connection`` for that level.
        """
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = None

        result = await mock_app._tools["call_model_method"](
            model="res.partner", method="toggle_active", arguments=[[1]]
        )

        assert result.success is True
        assert result.result is None

    @pytest.mark.asyncio
    async def test_not_authenticated_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.is_authenticated = False
        with pytest.raises(ValidationError, match="Not authenticated"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="x", arguments=[[1]]
            )

    @pytest.mark.asyncio
    async def test_audit_log_emitted_on_success(
        self, mock_app, mock_connection, mock_access_controller, caplog
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True

        with caplog.at_level("INFO", logger="mcp_server_odoo.tools"):
            await mock_app._tools["call_model_method"](
                model="account.move",
                method="action_post",
                arguments=[[42]],
                keyword_arguments={"context": {"lang": "en_US"}},
            )

        audit = [r for r in caplog.records if "call_model_method invoked" in r.message]
        assert audit, "expected audit log line"
        msg = audit[0].getMessage()
        assert "model=account.move" in msg
        assert "method=action_post" in msg
        assert "args_len=1" in msg
        assert "kwargs_keys=['context']" in msg

    @pytest.mark.asyncio
    async def test_xmlrpc_binary_coerced_to_base64(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """``xmlrpc.client.Binary`` is coerced to a base64 string (Pydantic-safe)."""
        import xmlrpc.client

        from mcp_server_odoo.schemas import CallModelMethodResult

        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = [
            {"id": 1, "image_1920": xmlrpc.client.Binary(b"hello")}
        ]

        result = await mock_app._tools["call_model_method"](
            model="res.partner", method="export_snapshot", arguments=[[1], ["image_1920"]]
        )

        assert isinstance(result, CallModelMethodResult)
        # aGVsbG8= is base64("hello")
        assert result.result == [{"id": 1, "image_1920": "aGVsbG8="}]
        # And the whole thing actually serializes via Pydantic.
        result.model_dump_json()

    @pytest.mark.asyncio
    async def test_xmlrpc_datetime_coerced_to_string(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """``xmlrpc.client.DateTime`` is coerced to its ISO-string form."""
        import xmlrpc.client

        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        dt = xmlrpc.client.DateTime("20250101T12:34:56")
        mock_connection.execute_kw.return_value = {"create_date": dt}

        result = await mock_app._tools["call_model_method"](
            model="res.partner", method="some_method", arguments=[[1]]
        )

        assert result.result == {"create_date": "20250101T12:34:56"}
        result.model_dump_json()  # Pydantic must accept the coerced value

    @pytest.mark.asyncio
    async def test_oversize_arguments_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """JSON-string ``arguments`` over the size cap is rejected before parsing."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        # 1.1 MB JSON string — past the cap; refuse before parsing.
        oversize = "[" + "1," * 600_000 + "1]"
        with pytest.raises(ValidationError, match="exceeds"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="x", arguments=oversize
            )

    @pytest.mark.asyncio
    async def test_oversize_keyword_arguments_rejected(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        oversize = "{" + '"k":' + '"' + ("a" * 1_100_000) + '"' + "}"
        with pytest.raises(ValidationError, match="exceeds"):
            await mock_app._tools["call_model_method"](
                model="res.partner",
                method="x",
                arguments=[[1]],
                keyword_arguments=oversize,
            )

    # --- Hardening denylists ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model, method, expected",
        [
            ("ir.actions.server", "run", "elevated privileges"),
            ("ir.cron", "method_direct_trigger", "elevated privileges"),
            ("res.partner", "web_read", r"web_\* data-access family"),
            ("res.partner", "create", "ORM data-access primitive"),
            # Name backstop on non-blocked models: accurate message, not the
            # ORM-primitive wording
            ("res.partner", "run", "privileged server actions"),
            ("res.partner", "method_direct_trigger", "privileged server actions"),
        ],
    )
    async def test_denylisted_calls_rejected_with_distinct_messages(
        self, mock_app, mock_connection, mock_access_controller, model, method, expected
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match=expected):
            await mock_app._tools["call_model_method"](model=model, method=method, arguments=[[1]])
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        [
            "ir.actions",
            "ir.actions.act_window",
            "ir.actions.report",
            "ir.cron",
        ],
    )
    async def test_blocked_model_prefixes_rejected_regardless_of_method(
        self, mock_app, mock_connection, mock_access_controller, model
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match="elevated privileges"):
            await mock_app._tools["call_model_method"](
                model=model, method="some_business_method", arguments=[[1]]
            )
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_ir_models_not_blocked(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """The model block is scoped to ir.actions/ir.cron — no blanket ir.% block."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = True

        await mock_app._tools["call_model_method"](
            model="ir.attachment", method="action_something", arguments=[[1]]
        )

        mock_connection.execute_kw.assert_called_once_with(
            "ir.attachment", "action_something", [[1]], {}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method",
        # Parametrize over the denylist itself so dropping or mistyping a
        # member fails CI. `_`-prefixed entries (e.g. `_write`) are excluded:
        # the public-identifier regex rejects them earlier with a different
        # message, so they never reach the "ORM data-access primitive" branch.
        sorted(m for m in _BLOCKED_METHOD_CALLS if not m.startswith("_")),
    )
    async def test_orm_primitives_rejected(
        self, mock_app, mock_connection, mock_access_controller, method
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match="ORM data-access primitive"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method=method, arguments=[[1]]
            )
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_web_prefix_rejected_for_future_methods(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """The web_* check is a prefix, covering methods not known today."""
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        with pytest.raises(ValidationError, match=r"web_\* data-access family"):
            await mock_app._tools["call_model_method"](
                model="res.partner", method="web_future_thing", arguments=[[1]]
            )
        mock_connection.execute_kw.assert_not_called()

    # --- Result-size guard ---

    @pytest.mark.asyncio
    async def test_oversized_list_result_truncated_with_marker(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = list(range(150))

        result = await mock_app._tools["call_model_method"](
            model="res.partner", method="action_bulk", arguments=[[1]]
        )

        assert result.success is True
        assert result.result == list(range(100))
        assert "result truncated to 100 of 150 items" in result.message

    @pytest.mark.asyncio
    async def test_list_result_at_cap_not_truncated(
        self, mock_app, mock_connection, mock_access_controller
    ):
        self._enabled_handler(mock_app, mock_connection, mock_access_controller)
        mock_connection.execute_kw.return_value = list(range(100))

        result = await mock_app._tools["call_model_method"](
            model="res.partner", method="action_bulk", arguments=[[1]]
        )

        assert result.result == list(range(100))
        assert "truncated" not in result.message


class TestSensitiveFieldStripping:
    """Credential-like fields are stripped from bulk (__all__) reads only."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        # No field metadata: binary swap and related summaries stay inert
        connection.fields_get.return_value = {}
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="k",
            database="d",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_get_record_all_fields_strips_sensitive(self, handler, mock_connection):
        """fields=["__all__"] omits credential-like fields and notes the withholding."""
        mock_connection.read.return_value = [
            {"id": 1, "name": "Test", "api_key": "sk-123", "webhook_secret": "shh"}
        ]

        result = await handler._handle_get_record_tool("res.partner", 1, ["__all__"])

        assert "api_key" not in result.record
        assert "webhook_secret" not in result.record
        assert result.record["name"] == "Test"
        assert result.metadata is not None
        assert "withheld" in result.metadata.note
        assert "api_key" in result.metadata.note
        assert "webhook_secret" in result.metadata.note

    @pytest.mark.asyncio
    async def test_get_record_all_fields_no_sensitive_no_note(self, handler, mock_connection):
        """fields=["__all__"] without sensitive fields keeps metadata absent."""
        mock_connection.read.return_value = [{"id": 1, "name": "Test"}]

        result = await handler._handle_get_record_tool("res.partner", 1, ["__all__"])

        assert result.record == {"id": 1, "name": "Test"}
        assert result.metadata is None

    @pytest.mark.asyncio
    async def test_get_record_explicit_sensitive_field_honored(self, handler, mock_connection):
        """Explicitly requesting a credential-like field by name returns it."""
        mock_connection.read.return_value = [{"id": 1, "api_key": "sk-123"}]

        result = await handler._handle_get_record_tool("res.partner", 1, ["api_key"])

        assert result.record["api_key"] == "sk-123"
        assert result.metadata is None
        mock_connection.read.assert_called_once_with(
            "res.partner", [1], ["api_key"], {"bin_size": True}
        )

    @pytest.mark.asyncio
    async def test_search_all_fields_strips_sensitive(self, handler, mock_connection):
        """search with fields=["__all__"] strips credential-like fields + sets note."""
        mock_connection.search_count.return_value = 2
        mock_connection.search.return_value = [1, 2]
        mock_connection.read.return_value = [
            {"id": 1, "name": "A", "api_key": "sk-1"},
            {"id": 2, "name": "B", "user_password": "pw"},
        ]

        result = await handler._handle_search_tool("res.partner", None, ["__all__"], 10, 0, None)

        for record in result["records"]:
            assert "api_key" not in record
            assert "user_password" not in record
        assert "withheld" in result["note"]
        assert "api_key" in result["note"]
        assert "user_password" in result["note"]

    @pytest.mark.asyncio
    async def test_search_wrapper_carries_note_on_search_result(
        self, handler, mock_app, mock_connection
    ):
        """The note survives SearchResult construction in the registered tool —
        a renamed/removed schema field would silently drop it (pydantic v2
        ignores unknown kwargs)."""
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "A", "api_key": "sk-1"}]

        result = await mock_app._tools["search_records"](model="res.partner", fields=["__all__"])

        assert "api_key" not in result.records[0]
        assert result.note is not None
        assert "withheld" in result.note
        assert "api_key" in result.note

    @pytest.mark.asyncio
    async def test_search_explicit_sensitive_field_honored(self, handler, mock_connection):
        """Explicit field lists are never stripped; note stays unset."""
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "api_key": "sk-1"}]

        result = await handler._handle_search_tool("res.partner", None, ["api_key"], 10, 0, None)

        assert result["records"][0]["api_key"] == "sk-1"
        assert result["note"] is None

    @pytest.mark.asyncio
    async def test_search_all_fields_no_sensitive_note_none(self, handler, mock_connection):
        """search __all__ without sensitive fields leaves the note unset."""
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "A"}]

        result = await handler._handle_search_tool("res.partner", None, ["__all__"], 10, 0, None)

        assert result["records"][0] == {"id": 1, "name": "A"}
        assert result["note"] is None

    @pytest.mark.asyncio
    async def test_get_record_smart_defaults_fallback_strips_sensitive(
        self, handler, mock_connection
    ):
        """Smart defaults unavailable (fields_get fails) → all-fields read is
        still a bulk path, so credential-like fields are stripped + noted."""
        mock_connection.fields_get.side_effect = Exception("metadata unavailable")
        mock_connection.read.return_value = [{"id": 1, "name": "Test", "api_key": "sk-123"}]

        result = await handler._handle_get_record_tool("res.partner", 1, None)

        assert "api_key" not in result.record
        assert result.record["name"] == "Test"
        assert result.metadata is not None
        assert "withheld" in result.metadata.note
        assert "api_key" in result.metadata.note
        # Honest fallback metadata: all fields were read, so the note must
        # not claim a limited selection
        assert result.metadata.field_selection_method == "all_fields_fallback"
        assert "Limited fields" not in result.metadata.note

    @pytest.mark.asyncio
    async def test_get_record_smart_defaults_fallback_metadata_honest(
        self, handler, mock_connection
    ):
        """Smart selection unavailable → metadata reflects the all-fields
        fallback instead of claiming 'Limited fields returned'."""
        mock_connection.fields_get.side_effect = Exception("metadata unavailable")
        mock_connection.read.return_value = [{"id": 1, "name": "Test"}]

        result = await handler._handle_get_record_tool("res.partner", 1, None)

        assert result.metadata is not None
        assert result.metadata.field_selection_method == "all_fields_fallback"
        assert "All fields returned" in result.metadata.note
        assert "Limited fields" not in result.metadata.note

    @pytest.mark.asyncio
    async def test_search_smart_defaults_fallback_strips_sensitive(self, handler, mock_connection):
        """Same bulk-path stripping on search when smart defaults fall back."""
        mock_connection.fields_get.side_effect = Exception("metadata unavailable")
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "A", "user_password": "pw"}]

        result = await handler._handle_search_tool("res.partner", None, None, 10, 0, None)

        assert "user_password" not in result["records"][0]
        assert "withheld" in result["note"]
        assert "user_password" in result["note"]


class TestBinaryValueSwap:
    """Populated binary values in tool results are swapped for odoo:// URIs."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        # Field metadata identifying the binary/image fields
        connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "image_128": {"type": "binary", "string": "Image 128"},
            "avatar_1024": {"type": "image", "string": "Avatar"},
        }
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069",
            api_key="k",
            database="d",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    @pytest.mark.asyncio
    async def test_get_record_swaps_populated_binary_for_uri(self, handler, mock_connection):
        """Populated binary fields become resource URIs; reads use bin_size."""
        # bin_size read: populated binaries come back as size placeholders
        mock_connection.read.return_value = [{"id": 7, "name": "Test", "image_128": "12.5 KB"}]

        result = await handler._handle_get_record_tool("res.partner", 7, ["name", "image_128"])

        assert result.record["image_128"] == "odoo://res.partner/record/7/image_128"
        # The read must pass the bin_size context
        assert mock_connection.read.call_args[0][3] == {"bin_size": True}

    @pytest.mark.asyncio
    async def test_get_record_empty_binary_stays_false(self, handler, mock_connection):
        """Empty binary fields (False) are not swapped."""
        mock_connection.read.return_value = [{"id": 7, "name": "Test", "image_128": False}]

        result = await handler._handle_get_record_tool("res.partner", 7, ["name", "image_128"])

        assert result.record["image_128"] is False

    @pytest.mark.asyncio
    async def test_search_swaps_populated_binary_for_uri(self, handler, mock_connection):
        """Each search result record gets its own record-field URIs."""
        mock_connection.search_count.return_value = 2
        mock_connection.search.return_value = [1, 2]
        mock_connection.read.return_value = [
            {"id": 1, "name": "A", "image_128": "12.5 KB", "avatar_1024": False},
            {"id": 2, "name": "B", "image_128": "3 KB", "avatar_1024": "1 KB"},
        ]

        result = await handler._handle_search_tool(
            "res.partner", None, ["name", "image_128", "avatar_1024"], 10, 0, None
        )

        records = result["records"]
        assert records[0]["image_128"] == "odoo://res.partner/record/1/image_128"
        assert records[0]["avatar_1024"] is False
        assert records[1]["image_128"] == "odoo://res.partner/record/2/image_128"
        assert records[1]["avatar_1024"] == "odoo://res.partner/record/2/avatar_1024"
        assert mock_connection.read.call_args[0][3] == {"bin_size": True}

    @pytest.mark.asyncio
    async def test_swap_skipped_when_metadata_unavailable(self, handler, mock_connection):
        """fields_get failure → values pass through unchanged, no error."""
        mock_connection.fields_get.side_effect = Exception("metadata unavailable")
        mock_connection.read.return_value = [{"id": 7, "name": "Test", "image_128": "12.5 KB"}]

        result = await handler._handle_get_record_tool("res.partner", 7, ["name", "image_128"])

        assert result.record["image_128"] == "12.5 KB"

    @pytest.mark.asyncio
    async def test_swap_degrades_when_metadata_is_unavailable(self, handler, mock_connection):
        """A failed fields_get leaves the bin_size placeholder in place rather
        than aborting the read. No retry: an immediate re-dial cannot outlast a
        hung socket and would double the timeout on a path that already
        degrades gracefully."""
        mock_connection.fields_get.side_effect = RuntimeError("metadata down")
        mock_connection.read.return_value = [{"id": 7, "name": "Test", "image_128": "12.5 KB"}]

        result = await handler._handle_get_record_tool("res.partner", 7, ["name", "image_128"])

        assert result.record["image_128"] == "12.5 KB"

    @pytest.mark.asyncio
    async def test_search_swap_skipped_without_valid_record_id(self, handler, mock_connection):
        """A record without a usable integer id cannot be given a URI — the
        value passes through unchanged instead of building a bogus one."""
        mock_connection.search_count.return_value = 2
        mock_connection.search.return_value = [1, 2]
        mock_connection.read.return_value = [
            {"name": "No id", "image_128": "12.5 KB"},  # id key missing
            {"id": 0, "name": "Zero id", "image_128": "3 KB"},  # non-positive id
        ]

        result = await handler._handle_search_tool(
            "res.partner", None, ["name", "image_128"], 10, 0, None
        )

        assert result["records"][0]["image_128"] == "12.5 KB"
        assert result["records"][1]["image_128"] == "3 KB"

    @pytest.mark.asyncio
    async def test_all_fields_read_swaps_after_stripping(self, handler, mock_connection):
        """__all__ path: sensitive stripping and binary swap both apply."""
        mock_connection.read.return_value = [
            {"id": 7, "name": "Test", "image_128": "12.5 KB", "api_key": "sk-1"}
        ]

        result = await handler._handle_get_record_tool("res.partner", 7, ["__all__"])

        assert "api_key" not in result.record
        assert result.record["image_128"] == "odoo://res.partner/record/7/image_128"

    @pytest.mark.asyncio
    async def test_credential_named_binary_stripped_not_swapped(self, handler, mock_connection):
        """__all__ path: a binary-typed field named like a credential is stripped
        (strip runs before the swap), never surfaced as a fetchable URI."""
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "image_128": {"type": "binary", "string": "Image 128"},
            "webhook_secret": {"type": "binary", "string": "Webhook Secret"},
        }
        mock_connection.read.return_value = [
            {"id": 7, "name": "Test", "image_128": "12.5 KB", "webhook_secret": "2 KB"}
        ]

        result = await handler._handle_get_record_tool("res.partner", 7, ["__all__"])

        # Stripped, not swapped to a fetchable URI, and named in the withheld note
        assert "webhook_secret" not in result.record
        assert "webhook_secret" in result.metadata.note
        # A non-credential binary still swaps normally
        assert result.record["image_128"] == "odoo://res.partner/record/7/image_128"

    @pytest.mark.asyncio
    async def test_get_record_attachment_datas_swaps_to_attachment_uri(
        self, handler, mock_connection
    ):
        """ir.attachment.datas gets the attachment URI; other binary fields
        on ir.attachment keep the generic record-field URI."""
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "datas": {"type": "binary", "string": "File Content"},
            "thumbnail": {"type": "image", "string": "Thumbnail"},
        }
        mock_connection.read.return_value = [
            {"id": 42, "name": "report.pdf", "datas": "12.5 KB", "thumbnail": "1 KB"}
        ]

        result = await handler._handle_get_record_tool(
            "ir.attachment", 42, ["name", "datas", "thumbnail"]
        )

        assert result.record["datas"] == "odoo://attachment/42"
        assert result.record["thumbnail"] == "odoo://ir.attachment/record/42/thumbnail"

    @pytest.mark.asyncio
    async def test_search_attachment_datas_swaps_to_attachment_uri(self, handler, mock_connection):
        """Each ir.attachment search result gets an odoo://attachment/{id} URI for datas."""
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "datas": {"type": "binary", "string": "File Content"},
        }
        mock_connection.search_count.return_value = 2
        mock_connection.search.return_value = [1, 2]
        mock_connection.read.return_value = [
            {"id": 1, "name": "a.pdf", "datas": "12.5 KB"},
            {"id": 2, "name": "b.pdf", "datas": False},
        ]

        result = await handler._handle_search_tool(
            "ir.attachment", None, ["name", "datas"], 10, 0, None
        )

        records = result["records"]
        assert records[0]["datas"] == "odoo://attachment/1"
        assert records[1]["datas"] is False

    @pytest.mark.asyncio
    async def test_get_record_url_attachment_datas_swaps_to_uri(self, handler, mock_connection):
        """A url-type attachment (type='url', datas=False) still points at the
        attachment resource, which serves its stored URL as text/uri-list."""
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "datas": {"type": "binary", "string": "File Content"},
            "type": {"type": "selection", "string": "Type"},
        }
        mock_connection.read.return_value = [
            {"id": 42, "name": "link", "datas": False, "type": "url"}
        ]

        result = await handler._handle_get_record_tool(
            "ir.attachment", 42, ["name", "datas", "type"]
        )

        assert result.record["datas"] == "odoo://attachment/42"

    @pytest.mark.asyncio
    async def test_get_record_empty_binary_attachment_datas_stays_false(
        self, handler, mock_connection
    ):
        """A binary-type attachment with no data (type='binary', datas=False)
        stays False — following an attachment URI would error 'holds no data'."""
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "datas": {"type": "binary", "string": "File Content"},
            "type": {"type": "selection", "string": "Type"},
        }
        mock_connection.read.return_value = [
            {"id": 42, "name": "empty", "datas": False, "type": "binary"}
        ]

        result = await handler._handle_get_record_tool(
            "ir.attachment", 42, ["name", "datas", "type"]
        )

        assert result.record["datas"] is False

    @pytest.mark.asyncio
    async def test_get_record_url_attachment_without_datas_requested_gains_no_key(
        self, handler, mock_connection
    ):
        """fields=['name', 'type'] on a url attachment: the swap must never
        INJECT a `datas` key the caller did not request."""
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "datas": {"type": "binary", "string": "File Content"},
            "type": {"type": "selection", "string": "Type"},
        }
        mock_connection.read.return_value = [{"id": 42, "name": "link", "type": "url"}]

        result = await handler._handle_get_record_tool("ir.attachment", 42, ["name", "type"])

        assert "datas" not in result.record

    @pytest.mark.asyncio
    async def test_get_record_url_attachment_without_type_keeps_datas_false(
        self, handler, mock_connection
    ):
        """fields=['datas'] alone on a url attachment: without `type` the
        url-vs-empty split is unknowable, so the falsy datas stays False
        (documented behavior) instead of guessing a URI."""
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "datas": {"type": "binary", "string": "File Content"},
            "type": {"type": "selection", "string": "Type"},
        }
        mock_connection.read.return_value = [{"id": 42, "datas": False}]

        result = await handler._handle_get_record_tool("ir.attachment", 42, ["datas"])

        assert result.record["datas"] is False


class TestBinarySwapAndRelatedBudget:
    """Guards on the two read-path enrichments added in 0.8.0."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def valid_config(self):
        return OdooConfig(
            url="http://localhost:8069", api_key="k", database="d", default_limit=10, max_limit=100
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    def test_dict_valued_binary_field_is_not_swapped(self, handler):
        """Odoo declares non-stored widget fields as Binary but returns a dict
        (sale.order.tax_totals). bin_size does not apply, so swapping it for a
        URI would drop the data AND advertise a URI whose read fails."""
        record = {
            "id": 18,
            "tax_totals": {"amount_total": 15895.88, "subtotals": []},
            "image_1920": "80.00 bytes",
        }
        handler._replace_binary_values(
            "sale.order", record, {"tax_totals", "image_1920"}, record_id=18
        )

        assert record["tax_totals"] == {"amount_total": 15895.88, "subtotals": []}
        # A real base64 payload is still swapped
        assert record["image_1920"] == "odoo://sale.order/record/18/image_1920"

    def test_unroutable_field_name_does_not_abort_the_read(self, handler):
        """A field name the URI grammar rejects has no servable URI; raising
        would abort the whole get_record/search_records call over one field."""
        record = {"id": 1, "_private_image": "12.5 KB", "image_1920": "80.00 bytes"}

        handler._replace_binary_values(
            "res.partner", record, {"_private_image", "image_1920"}, record_id=1
        )

        assert record["_private_image"] == "12.5 KB", "value left untouched"
        assert record["image_1920"] == "odoo://res.partner/record/1/image_1920"

    def test_empty_binary_field_stays_false(self, handler):
        record = {"id": 5, "image_1920": False}
        handler._replace_binary_values("res.partner", record, {"image_1920"}, record_id=5)
        assert record["image_1920"] is False

    def test_related_summaries_capped_per_record(
        self, handler, mock_connection, mock_access_controller
    ):
        """Every resolved field costs an access check plus a read RPC, so a
        relation-heavy record must not fan out without bound."""
        from mcp_server_odoo.tools import MAX_RELATED_SUMMARY_FIELDS

        field_count = MAX_RELATED_SUMMARY_FIELDS + 7
        mock_connection.fields_get.return_value = {
            f"rel_{i}": {"type": "many2many", "relation": "res.partner"} for i in range(field_count)
        }
        record = {f"rel_{i}": [1, 2] for i in range(field_count)}
        mock_connection.read.return_value = [
            {"id": 1, "display_name": "A"},
            {"id": 2, "display_name": "B"},
        ]
        mock_access_controller.validate_model_access.return_value = None

        summaries = handler._resolve_related_summaries("res.partner", record)

        assert len(summaries) == MAX_RELATED_SUMMARY_FIELDS
        assert mock_connection.read.call_count == MAX_RELATED_SUMMARY_FIELDS
        # Unresolved fields keep their ids untouched in the record
        assert record[f"rel_{field_count - 1}"] == [1, 2]

    def test_budget_counts_attempts_not_successes(
        self, handler, mock_connection, mock_access_controller
    ):
        """A relation the caller cannot read still costs a round trip before
        it fails, so budgeting on successful resolutions would let a record
        full of unreadable relations fan out without bound."""
        from mcp_server_odoo.tools import MAX_RELATED_SUMMARY_FIELDS

        field_count = MAX_RELATED_SUMMARY_FIELDS + 12
        mock_connection.fields_get.return_value = {
            f"rel_{i}": {"type": "many2many", "relation": "res.partner"} for i in range(field_count)
        }
        record = {f"rel_{i}": [1, 2] for i in range(field_count)}
        # Every relation is denied — nothing ever lands in `summaries`
        mock_access_controller.validate_model_access.side_effect = Exception("denied")

        summaries = handler._resolve_related_summaries("res.partner", record)

        assert summaries is None, "no field resolved"
        assert (
            mock_access_controller.validate_model_access.call_count == MAX_RELATED_SUMMARY_FIELDS
        ), "the budget must still cap the round trips spent failing"


class TestDomainIntBounds:
    """An oversized int in a domain used to raise OverflowError mid-marshal
    and surface as 'Connection error: Operation failed: Int exceeds XML-RPC
    limits' — a transport-flavoured message for plain bad input, which is
    exactly what the bounds guard exists to replace."""

    @pytest.fixture
    def handler(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        return OdooToolHandler(app, connection, MagicMock(spec=AccessController), config)

    @pytest.mark.parametrize(
        "domain",
        [
            [["id", "=", 2**40]],
            [["id", "in", [1, 2**40]]],
            '[["id", "=", 1099511627776]]',
        ],
    )
    def test_oversized_int_in_domain_is_a_validation_error(self, handler, domain):
        with pytest.raises(ValidationError, match="32-bit marshalling range"):
            handler._parse_domain_input(domain)

    def test_in_range_domains_still_parse(self, handler):
        assert handler._parse_domain_input([["id", "=", 2147483647]]) == [["id", "=", 2147483647]]
        assert handler._parse_domain_input([["is_company", "=", True]]) == [
            ["is_company", "=", True]
        ]


class TestAllFieldsMetadataReportsTotal:
    """An ["__all__"] read that withheld credential-like fields builds
    FieldSelectionMetadata on a path where total_fields was never resolved,
    so it used to report total_fields_available: null."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.mark.asyncio
    async def test_all_fields_read_reports_total_fields_available(self, mock_app):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        fields_meta = {
            "id": {"type": "integer"},
            "name": {"type": "char"},
            "smtp_host": {"type": "char"},
            "smtp_pass": {"type": "char"},
        }
        connection.fields_get.return_value = fields_meta
        connection.read.return_value = [
            {"id": 1, "name": "SMTP", "smtp_host": "mail.example.com", "smtp_pass": "hunter2"}
        ]
        access = MagicMock(spec=AccessController)
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        # Constructing the handler registers the tools onto mock_app
        OdooToolHandler(mock_app, connection, access, config)

        get_record = mock_app._tools["get_record"]
        result = await get_record(model="ir.mail_server", record_id=1, fields=["__all__"])

        assert "smtp_pass" not in result.record, "credential withheld"
        assert result.metadata is not None
        assert result.metadata.field_selection_method == "all"
        assert result.metadata.total_fields_available == len(fields_meta), (
            "an __all__ read still knows how many fields the model has"
        )
        assert "smtp_pass" in result.metadata.note


class TestResourceTemplateReadFilter:
    """Every listed template is read-only, so a model the caller cannot READ
    must not be advertised."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.mark.asyncio
    async def test_write_only_model_not_advertised(self, mock_app):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        access = MagicMock(spec=AccessController)
        access.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact", "operations": {"read": True}},
            {"model": "x.writeonly", "name": "Write Only", "operations": {"read": False}},
            {"model": "x.legacy", "name": "No Operations Key"},
        ]
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        handler = OdooToolHandler(mock_app, connection, access, config)

        result = await handler._handle_list_resource_templates_tool()

        assert "res.partner" in result["enabled_models"]
        assert "x.writeonly" not in result["enabled_models"]
        # A payload without the key predates the flag — assume readable
        assert "x.legacy" in result["enabled_models"]


class TestDeeplyNestedParameterStrings:
    """A 2 KB "[[[[...]]]]" string must be refused as an invalid parameter.

    Rejection keys on the string's own nesting depth, never on the parser
    failing: CPython 3.12 raised the JSON scanner's recursion ceiling, so
    `json.loads` raises RecursionError on 3.11 and earlier but accepts the
    same 1000-deep input on 3.12+. Keying on that made this input an
    "invalid parameter" on one interpreter and a stack-exhausting success on
    another. A byte cap would not have helped; the input is tiny.
    """

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def handler(self, mock_app):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        connection.search.return_value = []
        connection.search_count.return_value = 0
        connection.fields_get.return_value = {"id": {"type": "integer"}}
        access = MagicMock(spec=AccessController)
        access.validate_model_access.return_value = None
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        return OdooToolHandler(mock_app, connection, access, config)

    NESTED = "[" * 1000 + "]" * 1000

    @pytest.mark.asyncio
    async def test_domain_string_reports_invalid_domain(self, handler):
        with pytest.raises(ValidationError) as exc:
            await handler._handle_search_tool("res.partner", self.NESTED, None, 5, 0, None)

        assert "Invalid domain parameter" in str(exc.value)
        assert "nested deeper than" in str(exc.value)
        assert "recursion" not in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_fields_string_reports_invalid_fields(self, handler):
        with pytest.raises(ValidationError) as exc:
            await handler._handle_search_tool("res.partner", None, self.NESTED, 5, 0, None)

        assert "Invalid fields parameter" in str(exc.value)
        assert "nested deeper than" in str(exc.value)
        assert "recursion" not in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_brackets_inside_string_values_do_not_trip_the_guard(self, handler):
        """Depth is counted quote-aware, so a value that merely CONTAINS
        brackets — a URL, a serialized blob in a char field — is not mistaken
        for nesting."""
        domain = '[["name", "=", "' + "[" * 200 + '"]]'

        await handler._handle_search_tool("res.partner", domain, None, 5, 0, None)

    @pytest.mark.asyncio
    async def test_ordinary_nested_domain_is_accepted(self, handler):
        """A realistic domain nests ~3 deep and must pass untouched."""
        domain = '["|", ["id", "in", [1, 2, 3]], ["name", "=", "x"]]'

        await handler._handle_search_tool("res.partner", domain, None, 5, 0, None)


class TestAttachmentScopeDomain:
    """`ir.attachment` rows carry `url` and `index_content` (the extracted
    document text), so the enabled-model allowlist has to cover attachment
    METADATA and not only payloads: enable `ir.attachment` alone and every
    document on the database would otherwise be searchable, including ones
    hanging off models deliberately left out of the allowlist.
    """

    @pytest.fixture
    def access(self):
        controller = MagicMock(spec=AccessController)
        # No "operations" block: the shape an older MCP module returns, so
        # read permission is resolved per model.
        controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "ir.attachment", "name": "Attachment"},
        ]
        controller.get_model_permissions.side_effect = lambda model: ModelPermissions(
            model=model, enabled=True, can_read=True
        )
        return controller

    @pytest.fixture
    def config(self):
        return OdooConfig(url="http://localhost:8069", api_key="k", database="d")

    def test_scopes_to_enabled_models(self, config, access):
        assert attachment_scope_domain(config, access) == [
            "|",
            ("res_model", "=", False),
            ("res_model", "in", ["res.partner", "ir.attachment"]),
        ]

    def test_yolo_mode_is_unscoped(self, access):
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="k",
            database="d",
            username="admin",
            yolo_mode="read",
        )
        assert attachment_scope_domain(config, access) is None

    def test_unreportable_allowlist_fails_closed(self, config, access):
        """Swallowing this would disable the scope on every surface at once —
        the one outcome a security control must not have. The caller's ladder
        turns it into a retryable "could not verify access" instead.
        """
        access.get_enabled_models.side_effect = AccessControlUnavailableError("boom")
        with pytest.raises(AccessControlUnavailableError):
            attachment_scope_domain(config, access)

    def test_empty_allowlist_admits_only_standalone_attachments(self, config, access):
        """Nothing enabled means nothing an attachment may hang off. Returning
        None here would read as "no scope needed" and expose every row.
        """
        access.get_enabled_models.return_value = []
        assert attachment_scope_domain(config, access) == [("res_model", "=", False)]

    def test_enabled_but_unreadable_model_is_excluded(self, config, access):
        """Enablement and read permission are different endpoints. A model the
        caller may not read must not admit its attachments — that would
        sidestep validate_model_access via ir.attachment.
        """
        access.get_model_permissions.side_effect = lambda model: ModelPermissions(
            model=model, enabled=True, can_read=(model != "res.partner")
        )

        assert attachment_scope_domain(config, access) == [
            "|",
            ("res_model", "=", False),
            ("res_model", "in", ["ir.attachment"]),
        ]

    def test_operations_block_is_used_without_a_permission_request(self, config, access):
        """Newer modules ship the flag in /mcp/models itself, so the per-model
        request is not paid at all.
        """
        access.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact", "operations": {"read": True}},
            {"model": "hr.payslip", "name": "Payslip", "operations": {"read": False}},
        ]

        assert attachment_scope_domain(config, access) == [
            "|",
            ("res_model", "=", False),
            ("res_model", "in", ["res.partner"]),
        ]
        access.get_model_permissions.assert_not_called()

    def test_no_readable_model_admits_only_standalone_attachments(self, config, access):
        """Every enabled model unreadable is the same position as nothing
        enabled: only attachments with no res_model can qualify.
        """
        access.get_model_permissions.side_effect = lambda model: ModelPermissions(
            model=model, enabled=True, can_read=False
        )

        assert attachment_scope_domain(config, access) == [("res_model", "=", False)]

    def test_unreportable_permission_fails_closed(self, config, access):
        """Same reasoning as the allowlist itself: a permission that cannot be
        resolved must not quietly widen the scope.
        """
        access.get_model_permissions.side_effect = AccessControlUnavailableError("boom")

        with pytest.raises(AccessControlUnavailableError):
            attachment_scope_domain(config, access)

    def test_appended_not_and_prefixed(self, config, access):
        """A hand-written leading "&" would bind only the first of a
        multi-leaf caller domain and leave the rest dangling; Odoo's
        normalize_domain inserts the ANDs for a flat sequence instead.
        """
        caller = [("mimetype", "=", "application/pdf"), ("public", "=", False)]
        combined = list(caller) + attachment_scope_domain(config, access)
        assert combined[:2] == caller
        assert combined[2] == "|"


class TestAttachmentGatingInTools:
    """The tool surface must apply the same attachment gate as the resources."""

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        connection.search.return_value = []
        connection.search_count.return_value = 0
        connection.fields_get.return_value = {"id": {"type": "integer", "string": "ID"}}
        return connection

    @pytest.fixture
    def access(self):
        controller = MagicMock(spec=AccessController)
        controller.get_enabled_models.return_value = [{"model": "res.partner", "name": "Contact"}]
        return controller

    @pytest.fixture
    def handler(self, mock_app, connection, access):
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        return OdooToolHandler(mock_app, connection, access, config)

    @pytest.mark.asyncio
    async def test_search_records_scopes_attachments(self, handler, connection):
        await handler._handle_search_tool(
            "ir.attachment", [("public", "=", True)], None, 5, 0, None
        )

        domain = connection.search.call_args[0][1]
        assert domain == [
            ("public", "=", True),
            "|",
            ("res_model", "=", False),
            ("res_model", "in", ["res.partner"]),
        ]

    @pytest.mark.asyncio
    async def test_search_count_uses_the_same_scoped_domain(self, handler, connection):
        """The count must match the rows, or pagination reports a total the
        caller can never page to.
        """
        await handler._handle_search_tool("ir.attachment", None, None, 5, 0, None)

        assert connection.search_count.call_args[0][1] == connection.search.call_args[0][1]

    @pytest.mark.asyncio
    async def test_other_models_are_untouched(self, handler, connection):
        await handler._handle_search_tool("res.partner", [("id", ">", 1)], None, 5, 0, None)

        assert connection.search.call_args[0][1] == [("id", ">", 1)]

    @pytest.mark.asyncio
    async def test_aggregate_records_scopes_attachments(self, handler, connection):
        """The third tool-side call site of the scope — counting attachments
        grouped by anything would otherwise report totals across models the
        caller cannot read."""
        connection.get_major_version = MagicMock(return_value=19)
        connection.execute_kw.return_value = []

        await handler._handle_aggregate_records_tool(
            "ir.attachment", ["res_model"], ["__count"], [("public", "=", True)], None, 10, 0
        )

        # execute_kw(model, "formatted_read_group", [domain], kwargs)
        domain = connection.execute_kw.call_args[0][2][0]
        assert domain == [
            ("public", "=", True),
            "|",
            ("res_model", "=", False),
            ("res_model", "in", ["res.partner"]),
        ]

    @pytest.mark.asyncio
    async def test_unbalanced_domain_cannot_capture_the_scope(self, handler, connection):
        """["|", leaf] is invalid on its own and only becomes well-formed once
        the scope is appended — at which point the caller's dangling "|" takes
        the scope's OR-subtree as its second operand and the allowlist is
        satisfied by the caller's own leaf. Refused before any RPC.
        """
        with pytest.raises(ValidationError) as exc:
            await handler._handle_search_tool(
                "ir.attachment", ["|", ("id", ">", 0)], None, 5, 0, None
            )

        assert "Unbalanced domain" in str(exc.value)
        connection.search.assert_not_called()
        connection.search_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_unbalanced_domain_refused_by_aggregate_too(self, handler, connection):
        """The second call site appends the same scope the same way."""
        connection.get_major_version = MagicMock(return_value=19)

        with pytest.raises(ValidationError) as exc:
            await handler._handle_aggregate_records_tool(
                "ir.attachment", ["res_model"], ["__count"], ["|", ("id", ">", 0)], None, 10, 0
            )

        assert "Unbalanced domain" in str(exc.value)
        connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_record_refuses_attachment_on_denied_model(self, handler, connection, access):
        connection.search_read.return_value = [{"id": 7, "res_model": "hr.payslip"}]

        def gate(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("Model 'hr.payslip' is not enabled for MCP access")

        access.validate_model_access.side_effect = gate

        with pytest.raises(ValidationError) as exc:
            await handler._handle_get_record_tool("ir.attachment", 7, None)

        # Surfaced verbatim, not swallowed by the generic handler: without a
        # dedicated MCPPermissionError branch this became "Failed to get
        # record: <generic>" and lost the model name the caller needs.
        assert "hr.payslip" in str(exc.value)
        connection.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_record_allows_attachment_on_enabled_model(self, handler, connection, access):
        connection.search_read.return_value = [{"id": 7, "res_model": "res.partner"}]
        connection.read.return_value = [{"id": 7, "name": "quote.pdf"}]
        access.validate_model_access.return_value = None

        result = await handler._handle_get_record_tool("ir.attachment", 7, ["id", "name"])

        assert result.record["id"] == 7
        access.validate_model_access.assert_any_call("res.partner", "read")

    @pytest.mark.asyncio
    async def test_standalone_attachment_needs_only_the_attachment_gate(
        self, handler, connection, access
    ):
        connection.search_read.return_value = [{"id": 7, "res_model": False}]
        connection.read.return_value = [{"id": 7, "name": "note.txt"}]
        access.validate_model_access.return_value = None

        await handler._handle_get_record_tool("ir.attachment", 7, ["id", "name"])

        assert access.validate_model_access.call_count == 1

    @pytest.mark.asyncio
    async def test_unverifiable_access_is_not_reported_as_a_denial(
        self, handler, connection, access
    ):
        """AccessControlUnavailableError subclasses AccessControlError, so a
        transport outage would otherwise be surfaced as "not accessible via
        MCP" — a permanent-sounding answer to a retryable failure.
        """
        connection.search_read.return_value = [{"id": 7, "res_model": "hr.payslip"}]

        def gate(model, operation):
            if model == "hr.payslip":
                raise AccessControlUnavailableError("connection refused")

        access.validate_model_access.side_effect = gate

        with pytest.raises(ValidationError) as exc:
            await handler._handle_get_record_tool("ir.attachment", 7, None)

        assert "Could not verify access" in str(exc.value)
        assert "not accessible via MCP" not in str(exc.value)


class TestAttachmentGatingOnWrites:
    """The read gate is bypassable if the write paths are not gated too: an
    ungated update_record can repoint an excluded model's attachment at an
    allowed one, after which the read gate waves it through.
    """

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        connection.search_read.return_value = [{"id": 7, "res_model": "hr.payslip"}]
        connection.read.return_value = [{"id": 7, "display_name": "payslip.pdf"}]
        connection.write.return_value = True
        connection.unlink.return_value = True
        connection.create.return_value = 9
        return connection

    @pytest.fixture
    def access(self):
        controller = MagicMock(spec=AccessController)

        def gate(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("Model 'hr.payslip' is not enabled for MCP access")

        controller.validate_model_access.side_effect = gate
        return controller

    @pytest.fixture
    def handler(self, mock_app, connection, access):
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        return OdooToolHandler(mock_app, connection, access, config)

    @pytest.mark.asyncio
    async def test_update_refuses_attachment_on_denied_model(self, handler, connection):
        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_update_record_tool("ir.attachment", 7, {"name": "x.pdf"})

        connection.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_cannot_repoint_a_denied_attachment_into_view(self, handler, connection):
        """The escalation the gate exists to stop: move it to an allowed model
        and the read gate would then permit reading it."""
        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_update_record_tool(
                "ir.attachment", 7, {"res_model": "res.partner"}
            )

        connection.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_cannot_plant_onto_a_denied_model(self, handler, connection):
        connection.search_read.return_value = [{"id": 7, "res_model": "res.partner"}]

        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_update_record_tool(
                "ir.attachment", 7, {"res_model": "hr.payslip"}
            )

        connection.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_refuses_attachment_on_denied_model(self, handler, connection):
        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_delete_record_tool("ir.attachment", 7)

        connection.unlink.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_refuses_attachment_on_denied_model(self, handler, connection):
        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_create_record_tool(
                "ir.attachment", {"name": "x.pdf", "res_model": "hr.payslip", "res_id": 1}
            )

        connection.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_message_refuses_a_denied_attachment(self, handler, connection):
        """message_post repoints the attachments it is handed onto the thread
        record, so an ungated attachment_ids moves the document into view."""
        with pytest.raises(ValidationError, match="hr.payslip"):
            await handler._handle_post_message_tool(
                "res.partner", 1, "hi", "note", "comment", None, [7], False
            )

        connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_attachment_writes_go_through(self, handler, connection, access):
        connection.search_read.return_value = [{"id": 7, "res_model": "res.partner"}]

        result = await handler._handle_update_record_tool("ir.attachment", 7, {"name": "x.pdf"})

        assert result["success"] is True
        connection.write.assert_called_once()

    @pytest.mark.asyncio
    async def test_standalone_attachment_needs_only_the_attachment_gate(self, handler, connection):
        connection.search_read.return_value = [{"id": 7, "res_model": False}]

        result = await handler._handle_delete_record_tool("ir.attachment", 7)

        assert result["success"] is True


class TestSmartDefaultsEmptySelection:
    """Odoo's check_field_access_rights replaces a FALSY field list with every
    readable field, so `read(ids, [])` is an all-fields read. It has to take
    the same branch as None or the credential strip is skipped on a bulk read
    and the metadata claims a limited set that was never applied.
    """

    @pytest.fixture
    def mock_app(self):
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def handler(self, mock_app):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        connection.fields_get.return_value = {}
        connection.read.return_value = [{"id": 1, "openai_api_key": "sk-secret"}]
        connection.search.return_value = [1]
        connection.search_count.return_value = 1
        access = MagicMock(spec=AccessController)
        access.validate_model_access.return_value = None
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        return OdooToolHandler(mock_app, connection, access, config)

    @pytest.mark.asyncio
    async def test_get_record_strips_credentials(self, handler, monkeypatch):
        monkeypatch.setattr(handler, "_get_smart_default_fields", lambda model: [])

        result = await handler._handle_get_record_tool("x.model", 1, None)

        assert handler.connection.read.call_args[0][2] is None
        assert "openai_api_key" not in result.record
        assert result.metadata.field_selection_method == "all_fields_fallback"

    @pytest.mark.asyncio
    async def test_search_records_strips_credentials(self, handler, monkeypatch):
        monkeypatch.setattr(handler, "_get_smart_default_fields", lambda model: [])

        result = await handler._handle_search_tool("x.model", None, None, 5, 0, None)

        assert handler.connection.read.call_args[0][2] is None
        assert "openai_api_key" not in result["records"][0]
        assert "openai_api_key" in result["note"]
