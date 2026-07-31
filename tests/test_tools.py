"""Test suite for MCP tools functionality."""

from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import (
    ValidationError,
)
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError
from mcp_server_odoo.tools import OdooToolHandler


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
            "list_models",
            "create_record",
            "update_record",
            "delete_record",
            "post_message",
            "aggregate_records",
            "list_resource_templates",
            "export_records",
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
        assert len(result["templates"]) == 4

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

        def slow_search_count(*args, **kwargs):
            time.sleep(0.2)  # blocks its worker thread, must not block the loop
            return 0

        mock_connection.search_count.side_effect = slow_search_count
        mock_connection.search.return_value = []
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
        mock_connection.search_count.side_effect = OdooConnectionError("Connection lost")

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Call the tool and expect error
        with pytest.raises(ValidationError) as exc_info:
            await search_records(model="res.partner")

        assert "Connection error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_records_with_domain_operators(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with Odoo domain operators like |, &, !."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 10
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

        # Verify result (SearchResult is a Pydantic model)
        assert result.model == "res.partner"
        assert result.total == 10
        assert len(result.records) == 3

        # Verify the domain was passed correctly
        mock_connection.search_count.assert_called_with("res.partner", domain_with_or)
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
        mock_connection.search_count.assert_called_with("res.partner", expected_domain)
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
        mock_connection.search_count.assert_called_with("res.partner", expected_domain)

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
        mock_connection.read.assert_called_with("res.partner", [15], ["name", "is_company", "id"])

    @pytest.mark.asyncio
    async def test_search_records_with_complex_domain(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test search_records with complex nested domain operators."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 5
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

        # Verify the result
        assert result.model == "res.partner"
        assert result.total == 5
        assert len(result.records) == 2

        # Verify the domain was passed correctly
        mock_connection.search_count.assert_called_with("res.partner", complex_domain)
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
        mock_connection.read.assert_called_once_with("res.partner", [123], ["name", "email"])

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

        mock_connection.search_count.assert_called_with("res.partner", [])
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

        # Create a context that raises on every call. The report_progress
        # side_effect still exercises the surviving intermediate _ctx_progress
        # call in search_records (the "Found N records" 1/3 update).
        ctx = AsyncMock()
        ctx.info.side_effect = RuntimeError("transport broken")
        ctx.report_progress.side_effect = RuntimeError("transport broken")

        search_records = mock_app._tools["search_records"]
        # Should succeed despite broken context
        result = await search_records(model="res.partner", fields=["name"], limit=10, ctx=ctx)
        assert result.total == 1
        assert len(result.records) == 1
        # Confirm the surviving non-terminal progress call was attempted (and
        # its RuntimeError was swallowed by _ctx_progress' except branch).
        ctx.report_progress.assert_called()

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

        mock_access_controller.validate_model_access.assert_called_once_with("sale.order", "read")
        mock_connection.execute_kw.assert_called_once_with(
            "sale.order",
            "formatted_read_group",
            [[["state", "in", ["sale", "done"]]]],
            {
                "groupby": ["date_order:month"],
                "aggregates": ["amount_total:sum"],
                "limit": 10,
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
    async def test_empty_groupby_rejected(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None

        aggregate_records = mock_app._tools["aggregate_records"]
        with pytest.raises(ValidationError) as exc_info:
            await aggregate_records(model="res.partner", groupby=[])

        assert "groupby must not be empty" in str(exc_info.value)
        mock_connection.execute_kw.assert_not_called()

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
        assert passed_kwargs["limit"] == 10  # default_limit from valid_config fixture

    @pytest.mark.asyncio
    async def test_limit_capped_at_max(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.return_value = []

        aggregate_records = mock_app._tools["aggregate_records"]
        await aggregate_records(model="res.partner", groupby=["country_id"], limit=10000)

        passed_kwargs = mock_connection.execute_kw.call_args.args[3]
        assert passed_kwargs["limit"] == 100  # max_limit from valid_config fixture

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
    async def test_aggregate_rename_skips_groupby_collision(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Don't rename an aggregate whose bare field collides with a groupby key."""
        mock_access_controller.validate_model_access.return_value = None
        # If both groupby and aggregates reference 'partner_id', the groupby
        # value occupies that key already — leave it alone, even though
        # this means the count_distinct value is shadowed.
        mock_connection.execute_kw.return_value = [
            {
                "partner_id": [1, "Acme"],
                "__count": 5,
            },
        ]

        aggregate_records = mock_app._tools["aggregate_records"]
        result = await aggregate_records(
            model="sale.order",
            groupby=["partner_id"],
            aggregates=["partner_id:count_distinct"],
        )

        bucket = result.groups[0]
        # The groupby key is preserved; we don't clobber it
        assert bucket["partner_id"] == [1, "Acme"]

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
    async def test_update_record_empty_values(self, handler, mock_app):
        """Test update_record rejects empty values."""
        update_record = mock_app._tools["update_record"]
        with pytest.raises(ValidationError, match="No values provided"):
            await update_record(model="res.partner", record_id=1, values={})

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
        mock_connection.search_count.return_value = 5
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

        # Actually verify the return value
        assert result.model == "res.partner"
        assert result.total == 5
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
            "name_search",
            "read",
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
            model="res.partner", method="read", arguments=[[1], ["image_1920"]]
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
