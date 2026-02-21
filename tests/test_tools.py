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
        """Test that tools are registered with FastMCP."""
        # Check that all three tools are registered
        assert "search_records" in mock_app._tools
        assert "get_record" in mock_app._tools
        assert "list_models" in mock_app._tools

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


class TestToolIntegration:
    """Integration tests for tools with real server components."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_search_tool_with_real_server(self):
        """Test search tool with real server components."""
        # This test would require a running Odoo instance
        # Skipping for unit tests
        pytest.skip("Integration test requires running Odoo instance")

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_tools_in_mcp_context(self):
        """Test tools in full MCP context."""
        # This test would validate tools work correctly in MCP protocol
        # Skipping for unit tests
        pytest.skip("Integration test requires MCP setup")
