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
from mcp_server_odoo.tools import OdooToolHandler, register_tools


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

    def test_handler_initialization(self, handler, mock_app):
        """Test handler is properly initialized."""
        assert handler.app == mock_app
        assert handler.connection is not None
        assert handler.access_controller is not None
        assert handler.config is not None

    def test_tools_registered(self, handler, mock_app):
        """Test that all tools are registered with FastMCP."""
        expected_tools = {
            "search_records",
            "get_record",
            "list_models",
            "create_record",
            "update_record",
            "delete_record",
            "list_resource_templates",
        }
        assert set(mock_app._tools.keys()) == expected_tools

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

        assert "Invalid search criteria format" in str(exc_info.value)

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

        await search_records(model="res.partner", domain=complex_domain, limit=5)

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
    async def test_search_records_with_defaults(
        self, handler, mock_connection, mock_access_controller, mock_app, valid_config
    ):
        """Test search_records with default values."""
        # Setup mocks
        mock_connection.search_count.return_value = 0
        mock_connection.search.return_value = []
        mock_connection.read.return_value = []

        # Get the registered search_records function
        search_records = mock_app._tools["search_records"]

        # Call with minimal params
        result = await search_records(model="res.partner")

        # Verify defaults were applied (SearchResult is a Pydantic model)
        assert result.limit == valid_config.default_limit
        assert result.offset == 0
        assert result.total == 0
        assert result.records == []

        # Verify domain default
        mock_connection.search_count.assert_called_with("res.partner", [])

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

        # Should use default limit since 500 > max_limit (SearchResult is a Pydantic model)
        assert result.limit == valid_config.default_limit

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

        # Verify context.info was called
        ctx.info.assert_called()
        # First call should mention the model
        first_call_msg = ctx.info.call_args_list[0][0][0]
        assert "res.partner" in first_call_msg

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

    @pytest.mark.asyncio
    async def test_list_models_calls_context_info_and_progress(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that list_models sends context info and progress."""
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
        ctx.report_progress.assert_called()

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

        # Create a context that raises on every call
        ctx = AsyncMock()
        ctx.info.side_effect = RuntimeError("transport broken")
        ctx.report_progress.side_effect = RuntimeError("transport broken")

        search_records = mock_app._tools["search_records"]
        # Should succeed despite broken context
        result = await search_records(model="res.partner", fields=["name"], limit=10, ctx=ctx)
        assert result.total == 1
        assert len(result.records) == 1


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


class TestRegisterTools:
    """Test cases for register_tools function."""

    def test_register_tools_success(self):
        """Test successful registration of tools."""
        # Create mocks
        mock_app = MagicMock(spec=FastMCP)
        mock_connection = MagicMock(spec=OdooConnection)
        mock_access_controller = MagicMock(spec=AccessController)
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_key",
            database="test_db",
        )

        # Register tools
        handler = register_tools(mock_app, mock_connection, mock_access_controller, config)

        # Verify handler is returned
        assert isinstance(handler, OdooToolHandler)
        assert handler.app == mock_app
        assert handler.connection == mock_connection
        assert handler.access_controller == mock_access_controller
        assert handler.config == config
