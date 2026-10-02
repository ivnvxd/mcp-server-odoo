"""Tests for advanced resource operations (browse, count, fields)."""

import json
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.error_handling import (
    ValidationError,
)
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.resources import OdooResourceHandler

# Import skip_on_rate_limit decorator
from .test_xmlrpc_operations import skip_on_rate_limit


@pytest.fixture
def mock_config():
    """Create a mock configuration."""
    config = Mock(spec=OdooConfig)
    config.default_limit = 10
    config.max_limit = 100
    return config


def _realistic_fields_get():
    """Realistic fields_get return value for testing safe-field filtering."""
    return {
        "id": {"type": "integer", "string": "ID"},
        "name": {"type": "char", "string": "Name"},
        "email": {"type": "char", "string": "Email"},
        "is_company": {"type": "boolean", "string": "Is a Company"},
        "phone": {"type": "char", "string": "Phone"},
        "country_id": {"type": "many2one", "string": "Country", "relation": "res.country"},
        # Fields that should be filtered out
        "image_1920": {"type": "binary", "string": "Image"},
        "website_description": {"type": "html", "string": "Website Description"},
        "__last_update": {"type": "datetime", "string": "Last Modified on"},
    }


@pytest.fixture
def mock_connection():
    """Create a mock Odoo connection with realistic field metadata."""
    conn = Mock(spec=OdooConnection)
    conn.is_authenticated = True
    conn.fields_get = Mock(return_value=_realistic_fields_get())
    return conn


@pytest.fixture
def mock_access_controller():
    """Create a mock access controller."""
    controller = Mock(spec=AccessController)
    return controller


@pytest.fixture
def mock_app():
    """Create a mock MCPServer app."""
    app = Mock(spec=MCPServer)
    app.resource = Mock()

    # Store registered handlers
    app._handlers = {}

    def resource_decorator(uri_pattern, **kwargs):
        def decorator(func):
            app._handlers[uri_pattern] = func
            return func

        return decorator

    app.resource.side_effect = resource_decorator
    return app


@pytest.fixture
def resource_handler(mock_app, mock_connection, mock_access_controller, mock_config):
    """Create a resource handler instance."""
    return OdooResourceHandler(mock_app, mock_connection, mock_access_controller, mock_config)


class TestCountResource:
    """Test count resource functionality."""

    @pytest.mark.asyncio
    async def test_count_all_records(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test counting all records without domain."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 150

        # Execute count
        result = await resource_handler._handle_count("res.partner", None)

        # Verify calls
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        mock_connection.search_count.assert_called_once_with("res.partner", [])

        # Check result format
        assert "Count Result: res.partner" in result
        assert "Search criteria: All records" in result
        assert "Total count: 150 record(s)" in result

    @pytest.mark.asyncio
    async def test_count_with_domain(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test counting with domain filter."""
        # Setup domain
        domain = [["is_company", "=", True]]
        domain_encoded = quote(json.dumps(domain))

        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 45

        # Execute count
        result = await resource_handler._handle_count("res.partner", domain_encoded)

        # Verify domain was parsed and used
        mock_connection.search_count.assert_called_once_with("res.partner", domain)

        # Check result contains domain info
        assert "Search criteria: is_company = True" in result
        assert "Total count: 45 record(s)" in result

    @pytest.mark.asyncio
    async def test_count_complex_domain(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test counting with complex domain."""
        # Setup complex domain
        domain = [
            "|",
            ["customer_rank", ">", 0],
            "&",
            ["is_company", "=", True],
            ["active", "=", True],
        ]
        domain_encoded = quote(json.dumps(domain))

        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 25

        # Execute count
        result = await resource_handler._handle_count("res.partner", domain_encoded)

        # Check result contains complex domain
        assert "| customer_rank > 0 & is_company = True active = True" in result
        assert "Total count: 25 record(s)" in result

    @pytest.mark.asyncio
    async def test_count_zero_results(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test counting with no matching records."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 0

        # Execute count
        result = await resource_handler._handle_count(
            "res.partner", quote(json.dumps([["name", "=", "NonExistent"]]))
        )

        # Check result
        assert "Total count: 0 record(s)" in result


class TestFieldsResource:
    """Test fields resource functionality."""

    @pytest.mark.asyncio
    async def test_fields_basic(self, resource_handler, mock_connection, mock_access_controller):
        """Test basic field retrieval."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.fields_get.return_value = {
            "name": {
                "type": "char",
                "string": "Name",
                "required": True,
                "help": "The partner name",
            },
            "email": {"type": "char", "string": "Email", "required": False},
            "is_company": {
                "type": "boolean",
                "string": "Is a Company",
                "help": "Check if the contact is a company, otherwise it is a person",
            },
            "partner_id": {
                "type": "many2one",
                "string": "Related Partner",
                "relation": "res.partner",
            },
        }

        # Execute fields
        result = await resource_handler._handle_fields("res.partner")

        # Verify calls
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        mock_connection.fields_get.assert_called_once_with("res.partner")

        # Check result format
        assert "Field Definitions: res.partner" in result
        assert "Total fields: 4" in result
        assert "CHAR Fields (2):" in result
        assert "BOOLEAN Fields (1):" in result
        assert "MANY2ONE Fields (1):" in result

        # Check field details
        assert "name:" in result
        assert "Label: Name" in result
        assert "Required: True" in result
        assert "Help: The partner name" in result

        assert "partner_id:" in result
        assert "Related Model: res.partner" in result

    @pytest.mark.asyncio
    async def test_fields_with_selection(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test fields with selection type."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.fields_get.return_value = {
            "state": {
                "type": "selection",
                "string": "Status",
                "selection": [
                    ("draft", "Draft"),
                    ("open", "Open"),
                    ("done", "Done"),
                    ("cancel", "Cancelled"),
                ],
                "required": True,
            },
            "type": {
                "type": "selection",
                "string": "Type",
                "selection": [
                    ("contact", "Contact"),
                    ("invoice", "Invoice Address"),
                    ("delivery", "Delivery Address"),
                    ("other", "Other"),
                    ("private", "Private"),
                ],
            },
        }

        # Execute fields
        result = await resource_handler._handle_fields("res.partner")

        # Check selection fields
        assert "SELECTION Fields (2):" in result
        assert "state:" in result
        assert "Options: draft (Draft), open (Open), done (Done), cancel (Cancelled)" in result
        assert "type:" in result
        assert (
            "Options: contact (Contact), invoice (Invoice Address), delivery (Delivery Address), other (Other), private (Private)"
            in result
        )

    @pytest.mark.asyncio
    async def test_fields_with_numeric(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test fields with numeric types."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.fields_get.return_value = {
            "credit_limit": {"type": "float", "string": "Credit Limit", "digits": (16, 2)},
            "color": {"type": "integer", "string": "Color Index"},
            "debit": {"type": "monetary", "string": "Total Receivable", "readonly": True},
        }

        # Execute fields
        result = await resource_handler._handle_fields("account.partner")

        # Check numeric fields
        assert "FLOAT Fields (1):" in result
        assert "credit_limit:" in result
        assert "Precision: (16, 2)" in result

        assert "INTEGER Fields (1):" in result
        assert "color:" in result

        assert "MONETARY Fields (1):" in result
        assert "debit:" in result
        assert "Readonly: True" in result


class TestFieldsEdgeCases:
    """Test edge cases for fields resource."""

    @pytest.mark.asyncio
    async def test_fields_with_many_selections(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test that selection fields with 6+ options show count instead of listing."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.fields_get.return_value = {
            "priority": {
                "type": "selection",
                "string": "Priority",
                "selection": [
                    ("0", "Normal"),
                    ("1", "Low"),
                    ("2", "Medium"),
                    ("3", "High"),
                    ("4", "Very High"),
                    ("5", "Critical"),
                ],
            },
        }

        result = await resource_handler._handle_fields("project.task")

        # With 6 options (>5), should show count instead of listing each option
        assert "6 choices available" in result
        # Should NOT list individual options
        assert "Normal" not in result

    @pytest.mark.asyncio
    async def test_fields_connection_error(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test that OdooConnectionError in fields_get is wrapped as ValidationError."""
        from mcp_server_odoo.odoo_connection import OdooConnectionError

        mock_access_controller.validate_model_access.return_value = None
        mock_connection.fields_get.side_effect = OdooConnectionError("Server unreachable")

        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_fields("res.partner")

        assert "Connection error" in str(exc_info.value)
        assert "Server unreachable" in str(exc_info.value)


class TestCountNotAuthenticated:
    """Test count resource when not authenticated."""

    @pytest.mark.asyncio
    async def test_count_not_authenticated(self, resource_handler, mock_connection):
        """Test that _handle_count raises ValidationError when not authenticated."""
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_count("res.partner", None)

        assert "Not authenticated with Odoo" in str(exc_info.value)


class TestFieldsNotAuthenticated:
    """Test fields resource when not authenticated."""

    @pytest.mark.asyncio
    async def test_fields_not_authenticated(self, resource_handler, mock_connection):
        """Test that _handle_fields raises ValidationError when not authenticated."""
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_fields("res.partner")

        assert "Not authenticated with Odoo" in str(exc_info.value)


class TestAdvancedResourceIntegration:
    """Integration tests for advanced resources with real Odoo."""

    @pytest.mark.asyncio
    @pytest.mark.mcp
    @skip_on_rate_limit
    async def test_count_real_records(self, real_config, real_connection):
        """Test count with real Odoo connection."""
        # Setup real components
        app = Mock(spec=MCPServer)
        app.resource = Mock()
        app._handlers = {}

        def resource_decorator(uri_pattern, **kwargs):
            def decorator(func):
                app._handlers[uri_pattern] = func
                return func

            return decorator

        app.resource.side_effect = resource_decorator

        access_controller = AccessController(real_config)
        handler = OdooResourceHandler(app, real_connection, access_controller, real_config)

        # Authenticate
        real_connection.connect()
        real_connection.authenticate()

        # Count all partners
        result_all = await handler._handle_count("res.partner", None)
        assert "Search criteria: All records" in result_all
        assert "Total count:" in result_all

        # Count companies only
        domain = [["is_company", "=", True]]
        result_companies = await handler._handle_count("res.partner", quote(json.dumps(domain)))
        assert "is_company = True" in result_companies
        assert "Total count:" in result_companies

    @pytest.mark.asyncio
    @pytest.mark.mcp
    @skip_on_rate_limit
    async def test_fields_real_model(self, real_config, real_connection):
        """Test fields with real Odoo model."""
        # Setup real components
        app = Mock(spec=MCPServer)
        app.resource = Mock()
        app._handlers = {}

        def resource_decorator(uri_pattern, **kwargs):
            def decorator(func):
                app._handlers[uri_pattern] = func
                return func

            return decorator

        app.resource.side_effect = resource_decorator

        access_controller = AccessController(real_config)
        handler = OdooResourceHandler(app, real_connection, access_controller, real_config)

        # Authenticate
        real_connection.connect()
        real_connection.authenticate()

        # Get fields for res.partner
        result = await handler._handle_fields("res.partner")

        # Verify result contains expected fields
        assert "Field Definitions: res.partner" in result
        assert "Total fields:" in result

        # Check for common partner fields
        assert "name:" in result
        assert "email:" in result
        assert "is_company:" in result

        # Check field types are categorized
        assert "CHAR Fields" in result
        assert "BOOLEAN Fields" in result
        assert "MANY2ONE Fields" in result


@pytest.fixture
def real_config():
    """Load real configuration from .env file."""
    return load_config()


@pytest.fixture
def real_connection(real_config):
    """Create a real Odoo connection."""
    return OdooConnection(real_config)
