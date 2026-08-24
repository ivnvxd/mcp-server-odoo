"""Tests for search resource functionality."""

import json
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.error_handling import (
    MCPPermissionError,
    ValidationError,
)
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError
from mcp_server_odoo.resources import OdooResourceHandler


@pytest.fixture
def mock_config():
    """Create a mock configuration."""
    config = Mock(spec=OdooConfig)
    config.default_limit = 10
    config.max_limit = 100
    return config


@pytest.fixture
def mock_connection():
    """Create a mock Odoo connection."""
    conn = Mock(spec=OdooConnection)
    conn.is_authenticated = True
    return conn


@pytest.fixture
def mock_access_controller():
    """Create a mock access controller."""
    controller = Mock(spec=AccessController)
    return controller


@pytest.fixture
def mock_app():
    """Create a mock FastMCP app."""
    app = Mock(spec=FastMCP)
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


@pytest.fixture
def real_config():
    """Load real configuration from .env file."""
    return load_config()


@pytest.fixture
def real_connection(real_config):
    """Create a real Odoo connection."""
    return OdooConnection(real_config)


class TestSearchResource:
    """Test search resource functionality."""

    @pytest.mark.asyncio
    async def test_search_basic(self, resource_handler, mock_connection, mock_access_controller):
        """Test basic search without parameters."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 5
        mock_connection.search.return_value = [1, 2, 3, 4, 5]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Partner 1", "email": "p1@example.com"},
            {"id": 2, "name": "Partner 2", "email": "p2@example.com"},
            {"id": 3, "name": "Partner 3", "email": "p3@example.com"},
            {"id": 4, "name": "Partner 4", "email": "p4@example.com"},
            {"id": 5, "name": "Partner 5", "email": "p5@example.com"},
        ]
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name"},
            "email": {"type": "char", "string": "Email"},
        }

        # Execute search
        result = await resource_handler._handle_search("res.partner", None, None, None, None, None)

        # Verify calls
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        # The count always runs: a short page cannot be assumed complete
        mock_connection.search_count.assert_called_once()
        mock_connection.search.assert_called_once_with(
            "res.partner", [], limit=10, offset=0, order=None
        )
        # Without an explicit field list the handler reads only the fields the
        # one-line summary can render — "email" is never displayed here
        mock_connection.read.assert_called_once_with(
            "res.partner", [1, 2, 3, 4, 5], ["name"], {"bin_size": True}
        )

        # Check result format
        assert "Search Results: res.partner" in result
        assert "Showing records 1-5 of 5" in result
        assert "Partner 1" in result
        assert "Partner 5" in result

    @pytest.mark.asyncio
    async def test_search_with_domain(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with domain filter."""
        # Setup domain
        domain = [["is_company", "=", True]]
        domain_encoded = quote(json.dumps(domain))

        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 5
        mock_connection.search.return_value = [1, 3]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Company A", "is_company": True},
            {"id": 3, "name": "Company B", "is_company": True},
        ]
        mock_connection.fields_get.return_value = {}

        # Execute search
        result = await resource_handler._handle_search(
            "res.partner", domain_encoded, None, None, None, None
        )

        # Verify domain was parsed and used
        mock_connection.search_count.assert_called_once()
        mock_connection.search.assert_called_once_with(
            "res.partner", domain, limit=10, offset=0, order=None
        )

        # Check result contains domain info
        assert "Search criteria: is_company = True" in result
        assert "Company A" in result
        assert "Company B" in result

    @pytest.mark.asyncio
    async def test_search_with_fields(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with specific fields."""
        fields = "name,email,phone"

        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Test Partner", "email": "test@example.com", "phone": "+1234567890"}
        ]
        mock_connection.fields_get.return_value = {}

        # Execute search
        result = await resource_handler._handle_search(
            "res.partner", None, fields, None, None, None
        )

        # Verify fields were parsed and used
        mock_connection.read.assert_called_once_with(
            "res.partner", [1], ["name", "email", "phone"], {"bin_size": True}
        )

        # Check result shows fields
        assert "Fields: name, email, phone" in result
        assert "email: test@example.com" in result
        assert "phone: +1234567890" in result

    @pytest.mark.asyncio
    async def test_search_with_pagination(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with pagination parameters."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 50  # Total records
        mock_connection.search.return_value = [11, 12, 13, 14, 15]  # Page 2 results
        mock_connection.read.return_value = [
            {"id": i, "name": f"Partner {i}"} for i in range(11, 16)
        ]
        mock_connection.fields_get.return_value = {}

        # Execute search with pagination
        result = await resource_handler._handle_search(
            "res.partner",
            None,
            None,
            5,
            10,
            None,  # limit=5, offset=10
        )

        # Verify pagination in calls; offset > 0 still needs the exact count
        mock_connection.search.assert_called_once_with(
            "res.partner", [], limit=5, offset=10, order=None
        )
        mock_connection.search_count.assert_called_once_with("res.partner", [])

        # Check pagination info in result
        assert "Page 3 of 10" in result  # Page 3 because offset 10 with limit 5
        assert "Showing records 11-15 of 50" in result
        assert "→ Next page:" in result
        assert "← Previous page:" in result
        # Navigation must reference the search_records tool, never an
        # unroutable odoo://...?query URI (FastMCP cannot route query params)
        assert "search_records tool with offset=15" in result
        assert "search_records tool with offset=5" in result
        assert "odoo://res.partner/search?" not in result

    @pytest.mark.asyncio
    async def test_search_fields_get_fallback_strips_credential_fields(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Fallback all-fields read (fields_get down) still strips credential-like fields."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        # fields_get raises, so _get_safe_fields returns None → read ALL fields
        mock_connection.fields_get.side_effect = Exception("fields_get unavailable")
        mock_connection.read.return_value = [
            {"id": 1, "name": "Fallback Partner", "api_key": "sk-secret-123"}
        ]

        result = await resource_handler._handle_search("res.partner", None, None, None, None, None)

        # read fell back to no field list
        mock_connection.read.assert_called_once_with("res.partner", [1], None, {"bin_size": True})
        assert "Fallback Partner" in result
        # ... but the credential-like field never surfaces
        assert "api_key" not in result
        assert "sk-secret-123" not in result
        # ... and the withholding is visible in the text, not just debug-logged
        # (wording shared with the tools surface via field_security.withheld_note)
        assert "1 credential-like field(s) withheld" in result
        assert "request them by name via the get_record/search_records tools" in result
        # Resource framing points at the tools' fields parameter (resources
        # themselves have no fields parameter to honor the advice with)

    @pytest.mark.asyncio
    async def test_default_search_reads_only_summary_fields(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """A default search renders one summary line per record, so it reads
        only the fields that line can use — not every safe field. Nothing else
        is displayed, so there is no withholding trailer to emit either."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Partner 1"}]
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name"},
            "display_name": {"type": "char", "string": "Display Name"},
            "email": {"type": "char", "string": "Email"},
            "comment": {"type": "html", "string": "Notes"},
            "access_token": {"type": "char", "string": "Access Token"},
        }

        result = await resource_handler._handle_search("res.partner", None, None, None, None, None)

        assert mock_connection.read.call_args[0][2] == ["display_name", "name"]
        assert "Partner 1" in result
        assert "credential-like field(s) withheld" not in result

    @pytest.mark.asyncio
    async def test_search_explicit_fields_include_credential_field(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """An explicit fields= list is honored — even for credential-like names."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 1
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test", "api_key": "sk-secret-123"}]
        mock_connection.fields_get.return_value = {}

        result = await resource_handler._handle_search(
            "res.partner", None, "name,api_key", None, None, None
        )

        # The explicitly requested credential field is read and returned as-is
        mock_connection.read.assert_called_once_with(
            "res.partner", [1], ["name", "api_key"], {"bin_size": True}
        )
        assert "sk-secret-123" in result
        assert "withheld" not in result

    @pytest.mark.asyncio
    async def test_record_read_excludes_html_but_keeps_binary_fields(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """_get_safe_fields skips html/private fields but keeps STORED binary
        fields — the read runs under bin_size, so binaries arrive as size
        placeholders rendered as fetchable URIs. Exercised through the record
        resource: the search resource reads only summary fields now."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Partner 1"}]
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name"},
            "image_1920": {"type": "binary", "string": "Image"},
            "comment": {"type": "html", "string": "Notes"},
            "_private": {"type": "char", "string": "Private"},
        }

        await resource_handler._handle_record_retrieval("res.partner", "1")

        fields_read = mock_connection.read.call_args[0][2]
        assert fields_read == ["name", "image_1920"]

    @pytest.mark.asyncio
    async def test_search_with_order(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with order parameter."""
        order = "name desc, id asc"

        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 3
        mock_connection.search.return_value = [3, 1, 2]  # Ordered IDs
        mock_connection.read.return_value = [
            {"id": 3, "name": "Zebra Corp"},
            {"id": 1, "name": "Alpha Inc"},
            {"id": 2, "name": "Beta LLC"},
        ]
        mock_connection.fields_get.return_value = {}

        # Execute search
        result = await resource_handler._handle_search("res.partner", None, None, None, None, order)

        # Verify order was used
        mock_connection.search.assert_called_once_with(
            "res.partner", [], limit=10, offset=0, order="name desc, id asc"
        )

        # Results should show in order
        assert result.index("Zebra Corp") < result.index("Alpha Inc")

    @pytest.mark.asyncio
    async def test_search_empty_results(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with no results."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 0
        mock_connection.search.return_value = []
        mock_connection.fields_get.return_value = {}

        # Execute search
        result = await resource_handler._handle_search("res.partner", None, None, None, None, None)

        # Should not call read for empty results
        mock_connection.read.assert_not_called()

        # Check result message
        assert "No records found matching the criteria" in result
        assert "of 0" in result

    @pytest.mark.asyncio
    async def test_search_access_denied(self, resource_handler, mock_access_controller):
        """Test search with access denied."""
        # Setup access denial
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Model 'sale.order' is not enabled for MCP access"
        )

        # Execute search and expect permission error
        with pytest.raises(MCPPermissionError) as exc_info:
            await resource_handler._handle_search("sale.order", None, None, None, None, None)

        assert "Access denied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_connection_error(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with connection error."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search.side_effect = OdooConnectionError("Connection lost")

        # Execute search and expect error
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_search("res.partner", None, None, None, None, None)

        assert "Connection error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_generic_error_sanitized(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """A generic mid-handler error must not leak an internal file path."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search.return_value = [1]
        mock_connection.read.side_effect = RuntimeError(
            "Internal failure while reading /opt/odoo/internal/x.py record data"
        )

        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_search("res.partner", None, None, None, None, None)

        message = str(exc_info.value)
        assert "Failed to search records" in message
        assert "/opt/odoo/internal/x.py" not in message

    @pytest.mark.asyncio
    async def test_search_limit_validation(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search limit parameter validation."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 10
        mock_connection.search.return_value = list(range(1, 11))
        mock_connection.read.return_value = [{"id": i} for i in range(1, 11)]
        mock_connection.fields_get.return_value = {}

        # Test with negative limit (should use default)
        await resource_handler._handle_search("res.partner", None, None, -5, None, None)
        mock_connection.search.assert_called_with("res.partner", [], limit=10, offset=0, order=None)

        # Test with limit over max (should cap at max)
        mock_connection.search.reset_mock()
        await resource_handler._handle_search("res.partner", None, None, 200, None, None)
        mock_connection.search.assert_called_with(
            "res.partner", [], limit=100, offset=0, order=None
        )

    @pytest.mark.asyncio
    async def test_search_invalid_domain(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with invalid domain parameter."""
        # Invalid JSON domain
        invalid_domain = quote("not-valid-json")

        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 5
        mock_connection.search.return_value = [1, 2, 3, 4, 5]
        mock_connection.read.return_value = [{"id": i} for i in range(1, 6)]
        mock_connection.fields_get.return_value = {}

        # Should handle gracefully and use empty domain
        await resource_handler._handle_search("res.partner", invalid_domain, None, None, None, None)

        # Should use empty domain (short first page → no count call)
        mock_connection.search.assert_called_once_with(
            "res.partner", [], limit=10, offset=0, order=None
        )

    @pytest.mark.asyncio
    async def test_search_large_dataset_summary(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test search with large dataset shows summary."""
        # Setup mocks for large dataset
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 500  # Large dataset
        mock_connection.search.return_value = list(range(1, 11))
        mock_connection.read.return_value = [
            {"id": i, "name": f"Partner {i}"} for i in range(1, 11)
        ]
        mock_connection.fields_get.return_value = {}

        # Execute search
        result = await resource_handler._handle_search("res.partner", None, None, None, None, None)

        # A full first page cannot reveal the total — the count call runs
        mock_connection.search_count.assert_called_once_with("res.partner", [])

        # Should include dataset summary
        assert "Dataset Summary:" in result
        assert "Total records: 500" in result
        # Only shows filter suggestion when domain is present, which isn't the case here


class TestSearchResourceIntegration:
    """Integration tests for search resource with real Odoo."""

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_search_real_partners(self, real_config, real_connection):
        """Test search with real Odoo connection."""
        # Setup real components
        app = Mock(spec=FastMCP)
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

        # Connect and authenticate
        real_connection.connect()
        try:
            real_connection.authenticate()
        except OdooConnectionError as e:
            if "429" in str(e) or "Too many requests" in str(e).lower():
                pytest.skip("Rate limited by server")
            raise

        # Execute real search
        try:
            result = await handler._handle_search(
                "res.partner",
                quote(json.dumps([["is_company", "=", True]])),  # Search for companies
                "name,email,country_id",  # Specific fields
                5,  # Limit
                0,  # Offset
                "name asc",  # Order
            )
        except ValidationError as e:
            if "429" in str(e) or "Too many requests" in str(e).lower() or "Rate limit" in str(e):
                pytest.skip("Rate limited by server")
            raise

        # Verify result structure
        assert "Search Results: res.partner" in result
        assert "Search criteria:" in result
        assert "is_company = True" in result
        assert "Fields: name, email, country_id" in result
        assert "Page 1 of" in result

        # Should have actual partner data — verify at least one record was returned
        # Formatter uses "[1] Name" format for search results
        assert "[1]" in result


class TestSearchReadFailure:
    """Test search resource when read fails after search succeeds."""

    @pytest.mark.asyncio
    async def test_search_read_failure_after_search_success(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test that OdooConnectionError during read is wrapped as ValidationError."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.search_count.return_value = 3
        mock_connection.search.return_value = [1, 2, 3]
        # read raises OdooConnectionError after search succeeded
        mock_connection.read.side_effect = OdooConnectionError("Connection reset during read")

        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_search("res.partner", None, None, None, None, None)

        assert "Connection error" in str(exc_info.value)
        assert "Connection reset during read" in str(exc_info.value)


class TestSearchNotAuthenticated:
    """Test search resource when not authenticated."""

    @pytest.mark.asyncio
    async def test_search_not_authenticated(self, resource_handler, mock_connection):
        """Test that _handle_search raises ValidationError when not authenticated."""
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_search("res.partner", None, None, None, None, None)

        assert "Not authenticated with Odoo" in str(exc_info.value)
