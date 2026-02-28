"""Tests for basic MCP resource handling."""

from unittest.mock import Mock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import (
    NotFoundError,
    ValidationError,
)
from mcp_server_odoo.error_handling import (
    PermissionError as MCPPermissionError,
)
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError
from mcp_server_odoo.resources import OdooResourceHandler, register_resources


@pytest.fixture
def test_config():
    """Create test configuration."""
    # Load real config from environment for integration tests
    from mcp_server_odoo.config import get_config

    return get_config()


@pytest.fixture
def mock_config():
    """Create mock configuration."""
    config = Mock(spec=OdooConfig)
    config.default_limit = 10
    config.max_limit = 100
    return config


@pytest.fixture
def mock_connection():
    """Create mock OdooConnection with realistic field metadata."""
    conn = Mock(spec=OdooConnection)
    conn.is_authenticated = True
    conn.search = Mock()
    conn.read = Mock()
    # Provide realistic field metadata so safe-field filtering is actually exercised
    conn.fields_get = Mock(
        return_value={
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "display_name": {"type": "char", "string": "Display Name"},
            "email": {"type": "char", "string": "Email"},
            "is_company": {"type": "boolean", "string": "Is a Company"},
            "country_id": {"type": "many2one", "string": "Country", "relation": "res.country"},
            "child_ids": {"type": "one2many", "string": "Contacts", "relation": "res.partner"},
            "phone": {"type": "char", "string": "Phone"},
            # Fields that SHOULD be filtered out by safe-field logic
            "image_1920": {"type": "binary", "string": "Image"},
            "image_128": {"type": "binary", "string": "Image 128"},
            "website_description": {"type": "html", "string": "Website Description"},
            "_serialized_data": {"type": "serialized", "string": "Serialized Data"},
            "__last_update": {"type": "datetime", "string": "Last Modified on"},
        }
    )
    return conn


@pytest.fixture
def mock_access_controller():
    """Create mock AccessController."""
    controller = Mock(spec=AccessController)
    controller.validate_model_access = Mock()
    return controller


@pytest.fixture
def mock_app():
    """Create mock FastMCP app."""
    app = Mock(spec=FastMCP)
    app.resource = Mock()
    # Make resource decorator return the function as-is
    app.resource.return_value = lambda func: func
    return app


@pytest.fixture
def resource_handler(mock_app, mock_connection, mock_access_controller, mock_config):
    """Create OdooResourceHandler instance."""
    return OdooResourceHandler(mock_app, mock_connection, mock_access_controller, mock_config)


class TestOdooResourceHandler:
    """Test OdooResourceHandler functionality."""

    def test_init(self, mock_app, mock_connection, mock_access_controller, mock_config):
        """Test handler initialization."""
        handler = OdooResourceHandler(
            mock_app, mock_connection, mock_access_controller, mock_config
        )

        assert handler.app == mock_app
        assert handler.connection == mock_connection
        assert handler.access_controller == mock_access_controller
        assert handler.config == mock_config

        # Check that resources were registered
        assert mock_app.resource.call_count >= 1

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_success(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test successful record retrieval with safe-field filtering."""
        # Setup mocks
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [
            {
                "id": 1,
                "name": "Test Partner",
                "display_name": "Test Partner",
                "email": "test@example.com",
                "is_company": True,
                "country_id": (1, "United States"),
                "child_ids": [2, 3, 4],
                "phone": False,
            }
        ]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        # Verify access control was called
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        mock_connection.search.assert_called_once_with("res.partner", [("id", "=", 1)])

        # Verify safe-field filtering: binary/html/serialized/private fields excluded
        read_call_args = mock_connection.read.call_args
        assert read_call_args is not None
        # read should be called with (model, ids, safe_fields)
        assert read_call_args[0][0] == "res.partner"
        assert read_call_args[0][1] == [1]
        safe_fields = read_call_args[0][2]
        assert isinstance(safe_fields, list)
        # Binary fields must be excluded
        assert "image_1920" not in safe_fields
        assert "image_128" not in safe_fields
        # HTML fields must be excluded
        assert "website_description" not in safe_fields
        # Serialized fields must be excluded
        assert "_serialized_data" not in safe_fields
        # Private fields (starting with __) must be excluded
        assert "__last_update" not in safe_fields
        # Normal fields must be included
        assert "name" in safe_fields
        assert "email" in safe_fields
        assert "is_company" in safe_fields

        # Check result format
        assert "Record: res.partner/1" in result
        assert "Name: Test Partner" in result
        assert "=" * 50 in result

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_not_found(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test record not found error."""
        # Setup mocks
        mock_connection.search.return_value = []

        # Test retrieval
        with pytest.raises(NotFoundError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "999")

        assert "Record not found: res.partner with ID 999 does not exist" in str(exc_info.value)

        # Verify calls
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        mock_connection.search.assert_called_once_with("res.partner", [("id", "=", 999)])
        mock_connection.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_invalid_id(self, resource_handler):
        """Test invalid record ID."""
        # Test with non-numeric ID
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "abc")

        assert "Invalid record ID 'abc'" in str(exc_info.value)

        # Test with negative ID
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "-5")

        assert "Record ID must be positive" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_permission_denied(
        self, resource_handler, mock_access_controller
    ):
        """Test permission denied error."""
        # Setup mock to raise permission error
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied for res.partner"
        )

        # Test retrieval
        with pytest.raises(MCPPermissionError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Access denied:" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_not_authenticated(
        self, resource_handler, mock_connection
    ):
        """Test error when not authenticated."""
        # Setup mock
        mock_connection.is_authenticated = False

        # Test retrieval
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Not authenticated with Odoo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_connection_error(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test connection error during retrieval."""
        # Setup mock to raise connection error
        mock_connection.search.side_effect = OdooConnectionError("Connection lost")

        # Test retrieval
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Connection error:" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_read_returns_empty(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test NotFoundError when search finds ID but read returns empty list."""
        # search finds the record ID, but read returns nothing
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = []

        with pytest.raises(NotFoundError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Record not found" in str(exc_info.value)
        assert "res.partner" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_record_retrieval_all_fields_unsafe(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test fallback to unfiltered read when all fields are binary/image (unsafe)."""
        # fields_get returns ONLY binary fields, so safe_fields will be empty
        mock_connection.fields_get.return_value = {
            "image_1920": {"type": "binary", "string": "Image"},
            "image_128": {"type": "binary", "string": "Image 128"},
            "__last_update": {"type": "datetime", "string": "Last Modified on"},
        }
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Binary Partner"}]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        # Since all fields are unsafe, safe_fields is empty => fallback to no filter
        mock_connection.read.assert_called_once_with("res.partner", [1])
        assert "Binary Partner" in result

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_fields_get_fallback(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test fallback to reading all fields when fields_get raises an exception."""
        mock_connection.search.return_value = [1]
        # fields_get raises an exception, triggering the fallback path
        mock_connection.fields_get.side_effect = Exception("fields_get unavailable")
        mock_connection.read.return_value = [{"id": 1, "name": "Fallback Partner"}]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        # read should have been called without a field list (fallback)
        mock_connection.read.assert_called_once_with("res.partner", [1])

        # Result should still contain the record data
        assert "res.partner" in result
        assert "Fallback Partner" in result


class TestRegisterResources:
    """Test register_resources function."""

    def test_register_resources(
        self, mock_app, mock_connection, mock_access_controller, mock_config
    ):
        """Test resource registration."""
        handler = register_resources(mock_app, mock_connection, mock_access_controller, mock_config)

        assert isinstance(handler, OdooResourceHandler)
        assert handler.app == mock_app
        assert handler.connection == mock_connection
        assert handler.access_controller == mock_access_controller
        assert handler.config == mock_config

        # Check that resources were registered
        assert mock_app.resource.call_count >= 1


class TestResourceIntegration:
    """Integration tests with real Odoo server."""

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_real_record_retrieval(self, test_config):
        """Test record retrieval with real server."""
        # Create real connection
        connection = OdooConnection(test_config)
        connection.connect()
        connection.authenticate()

        # Create access controller
        access_controller = AccessController(test_config)

        # Create FastMCP app
        app = FastMCP("test-app")

        # Register resources
        handler = register_resources(app, connection, access_controller, test_config)

        try:
            # Search for a partner record
            partner_ids = connection.search("res.partner", [], limit=1)

            if partner_ids:
                # Test retrieval
                result = await handler._handle_record_retrieval("res.partner", str(partner_ids[0]))

                # Verify result format
                assert f"Record: res.partner/{partner_ids[0]}" in result
                assert "Name:" in result
                assert "=" * 50 in result  # Separator line

        finally:
            connection.disconnect()

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_real_record_not_found(self, test_config):
        """Test record not found with real server."""
        # Create real connection
        connection = OdooConnection(test_config)
        connection.connect()
        connection.authenticate()

        # Create access controller
        access_controller = AccessController(test_config)

        # Create FastMCP app
        app = FastMCP("test-app")

        # Register resources
        handler = register_resources(app, connection, access_controller, test_config)

        try:
            # Test with non-existent ID
            with pytest.raises(NotFoundError):
                await handler._handle_record_retrieval("res.partner", "999999999")

        finally:
            connection.disconnect()

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_real_permission_denied(self, test_config):
        """Test permission denied with real server."""
        # Create real connection
        connection = OdooConnection(test_config)
        connection.connect()
        connection.authenticate()

        # Create access controller
        access_controller = AccessController(test_config)

        # Create FastMCP app
        app = FastMCP("test-app")

        # Register resources
        handler = register_resources(app, connection, access_controller, test_config)

        try:
            # Use a fake model that fails in both modes: permission denied
            # in standard mode, connection error in YOLO mode
            with pytest.raises((MCPPermissionError, ValidationError)):
                await handler._handle_record_retrieval("nonexistent.model.xyz", "1")

        finally:
            connection.disconnect()
