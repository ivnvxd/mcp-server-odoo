"""Tests for resource handlers."""

import unittest
from unittest.mock import MagicMock, patch

from mcp import Resource

from mcp_server_odoo.resource_handlers import (
    BrowseResourceHandler,
    CountResourceHandler,
    FieldsResourceHandler,
    RecordResourceHandler,
    ResourceHandlerError,
    ResourceHandlerRegistry,
    SearchResourceHandler,
)


class TestResourceHandlers(unittest.TestCase):
    """Test basic resource handler functionality."""

    def setUp(self):
        """Set up test environment."""
        self.odoo = MagicMock()

        # Create a concrete implementation of base resource handler for testing
        class TestResourceHandler:
            def __init__(self, odoo):
                self.odoo = odoo

            def can_handle(self, resource):
                return True

            def handle(self, resource):
                return {"test": "result"}

            def _parse_query_params(self, resource):
                import urllib.parse

                parsed_url = urllib.parse.urlparse(str(resource.uri))
                query_params = urllib.parse.parse_qs(parsed_url.query)
                return {k: v[0] if len(v) == 1 else v for k, v in query_params.items()}

        self.test_handler = TestResourceHandler(self.odoo)

    def test_parse_query_params(self):
        """Test query parameter parsing."""
        # Create a Resource with name (required parameter)
        resource = Resource(
            uri="odoo://res.partner/search?param1=value1&param2=value2",
            name="Test Resource",
        )

        # Parse parameters
        params = self.test_handler._parse_query_params(resource)

        # Check parameter values
        self.assertEqual(params["param1"], "value1")
        self.assertEqual(params["param2"], "value2")

    def test_parse_query_params_with_arrays(self):
        """Test query parameter parsing with arrays."""
        # Create a Resource with name (required parameter)
        resource = Resource(
            uri="odoo://res.partner/search?ids=1,2,3&names=a,b,c", name="Test Resource"
        )

        # Parse parameters
        params = self.test_handler._parse_query_params(resource)

        # Check that comma-separated values are preserved
        self.assertEqual(params["ids"], "1,2,3")
        self.assertEqual(params["names"], "a,b,c")


class TestResourceHandlerRegistryFunctions(unittest.TestCase):
    """Test resource handler registry functionality."""

    def setUp(self):
        """Set up test environment."""
        self.odoo = MagicMock()
        self.registry = ResourceHandlerRegistry(
            odoo=self.odoo,
            default_limit=10,
            max_limit=100,
        )

    def test_handler_dispatch(self):
        """Test that the registry dispatches to the correct handler."""
        # Get the handlers from the registry's instance
        handlers = self.registry.handlers

        # Create a mock Resource
        resource = Resource(uri="odoo://res.partner/record/1", name="Test Resource")

        # Mock the handlers to isolate the test
        for handler in handlers:
            handler.can_handle = MagicMock(return_value=False)
            handler.handle = MagicMock()

        # Make the first handler handle this resource
        handlers[0].can_handle.return_value = True
        handlers[0].handle.return_value = {"is_error": False}

        # Test handling
        result = self.registry.handle_resource(resource)

        # Verify the first handler was called
        handlers[0].handle.assert_called_once_with(resource)
        self.assertEqual(result, {"is_error": False})

    def test_no_handler_found(self):
        """Test error when no handler is found."""
        # Get the handlers from the registry
        handlers = self.registry.handlers

        # Make sure no handler can handle this resource
        for handler in handlers:
            handler.can_handle = MagicMock(return_value=False)

        # Create a mock Resource
        resource = Resource(uri="odoo://unknown/operation", name="Unknown Resource")

        # Test handling - should raise error
        with self.assertRaises(ResourceHandlerError):
            self.registry.handle_resource(resource)


class TestRecordResourceHandlerFunctions(unittest.TestCase):
    """Test record resource handler functionality."""

    def setUp(self):
        """Set up test environment."""
        self.odoo = MagicMock()
        self.handler = RecordResourceHandler(self.odoo)

    def test_can_handle_valid_uri(self):
        """Test handler recognizes valid URIs."""
        # Valid URIs
        resource1 = Resource(uri="odoo://res.partner/record/1", name="Partner Record")
        resource2 = Resource(
            uri="odoo://product.product/record/42", name="Product Record"
        )

        self.assertTrue(self.handler.can_handle(resource1))
        self.assertTrue(self.handler.can_handle(resource2))

    def test_can_handle_invalid_uri(self):
        """Test handler rejects invalid URIs."""
        # Invalid URIs
        resource1 = Resource(uri="odoo://res.partner/search", name="Partner Search")
        resource2 = Resource(uri="odoo://res.partner/record/abc", name="Invalid ID")
        resource3 = Resource(uri="odoo://res.partner/record/", name="Missing ID")

        self.assertFalse(self.handler.can_handle(resource1))
        self.assertFalse(self.handler.can_handle(resource2))
        self.assertFalse(self.handler.can_handle(resource3))

    def test_handle_success(self):
        """Test successful record handling with patched formatter."""
        # Mock the Odoo read method
        self.odoo.read.return_value = [{"id": 1, "name": "Test Partner"}]

        # Create a mock Resource
        resource = Resource(uri="odoo://res.partner/record/1", name="Partner Record")

        # Use patch to mock the formatter function
        with patch("mcp_server_odoo.resource_handlers.format_record") as mock_format:
            mock_format.return_value = "Formatted record"

            # Handle the resource
            result = self.handler.handle(resource)

            # Check result
            self.assertFalse(result["is_error"])
            self.assertEqual(result["content"][0]["text"], "Formatted record")

            # Verify the Odoo read was called with the right parameters
            self.odoo.read.assert_called_once_with("res.partner", [1])

    def test_handle_record_not_found(self):
        """Test error when record is not found."""
        # Mock the Odoo read method to return empty list
        self.odoo.read.return_value = []

        # Create a mock Resource
        resource = Resource(
            uri="odoo://res.partner/record/9999", name="Non-existent Record"
        )

        # Should raise a ResourceHandlerError
        with self.assertRaises(ResourceHandlerError) as context:
            self.handler.handle(resource)

        # Check error message
        self.assertIn("Record not found", str(context.exception))


class TestSearchResourceHandlerFunctions(unittest.TestCase):
    """Test search resource handler functionality."""

    def setUp(self):
        """Set up test environment."""
        self.odoo = MagicMock()
        self.handler = SearchResourceHandler(
            odoo=self.odoo,
            default_limit=10,
            max_limit=100,
        )

    def test_can_handle_valid_uri(self):
        """Test handler recognizes valid URIs."""
        # Valid URIs
        resource1 = Resource(uri="odoo://res.partner/search", name="Partner Search")
        resource2 = Resource(
            uri="odoo://product.product/search?domain=[('active','=',True)]",
            name="Product Search",
        )

        self.assertTrue(self.handler.can_handle(resource1))
        self.assertTrue(self.handler.can_handle(resource2))

    def test_can_handle_invalid_uri(self):
        """Test handler rejects invalid URIs."""
        # Invalid URIs
        resource1 = Resource(uri="odoo://res.partner/record/1", name="Partner Record")
        resource2 = Resource(uri="odoo://res.partner/counts", name="Invalid Operation")

        self.assertFalse(self.handler.can_handle(resource1))
        self.assertFalse(self.handler.can_handle(resource2))

    def test_handle_success(self):
        """Test successful search with patched formatter."""
        # Mock Odoo responses
        self.odoo.count.return_value = 2
        self.odoo.search.return_value = [
            {"id": 1, "name": "Partner 1"},
            {"id": 2, "name": "Partner 2"},
        ]

        # Create a mock Resource
        resource = Resource(
            uri="odoo://res.partner/search?limit=2", name="Search Resource"
        )

        # Use patch to mock the formatter function
        with patch(
            "mcp_server_odoo.resource_handlers.format_search_results"
        ) as mock_format:
            mock_format.return_value = "Formatted search results"

            # Handle the resource
            result = self.handler.handle(resource)

            # Check result
            self.assertFalse(result["is_error"])
            self.assertEqual(result["content"][0]["text"], "Formatted search results")

            # Verify Odoo methods were called
            self.odoo.count.assert_called_once()
            self.odoo.search.assert_called_once()


class TestCountResourceHandlerFunctions(unittest.TestCase):
    """Test count resource handler functionality."""

    def setUp(self):
        """Set up test environment."""
        self.odoo = MagicMock()
        self.handler = CountResourceHandler(self.odoo)

    def test_can_handle_valid_uri(self):
        """Test handler recognizes valid URIs."""
        # Valid URIs
        resource1 = Resource(uri="odoo://res.partner/count", name="Partner Count")
        resource2 = Resource(
            uri="odoo://product.product/count?domain=[('active','=',True)]",
            name="Product Count",
        )

        self.assertTrue(self.handler.can_handle(resource1))
        self.assertTrue(self.handler.can_handle(resource2))

    def test_can_handle_invalid_uri(self):
        """Test handler rejects invalid URIs."""
        # Invalid URIs
        resource1 = Resource(uri="odoo://res.partner/record/1", name="Partner Record")
        resource2 = Resource(uri="odoo://res.partner/search", name="Partner Search")

        self.assertFalse(self.handler.can_handle(resource1))
        self.assertFalse(self.handler.can_handle(resource2))

    def test_handle_success(self):
        """Test successful count operation."""
        # Mock Odoo response
        self.odoo.count.return_value = 42

        # Create a mock Resource
        resource = Resource(uri="odoo://res.partner/count", name="Count Resource")

        # Handle the resource
        result = self.handler.handle(resource)

        # Check result
        self.assertFalse(result["is_error"])
        self.assertIn("42", result["content"][0]["text"])

        # Verify Odoo method was called with empty domain by default
        self.odoo.count.assert_called_once_with("res.partner", [])


class TestBrowseResourceHandlerFunctions(unittest.TestCase):
    """Test browse resource handler functionality."""

    def setUp(self):
        """Set up test environment."""
        self.odoo = MagicMock()
        self.handler = BrowseResourceHandler(self.odoo)

    def test_can_handle_valid_uri(self):
        """Test handler recognizes valid URIs."""
        # Valid URIs
        resource1 = Resource(
            uri="odoo://res.partner/browse?ids=1,2,3", name="Partner Browse"
        )
        resource2 = Resource(
            uri="odoo://product.product/browse?ids=42", name="Product Browse"
        )

        self.assertTrue(self.handler.can_handle(resource1))
        self.assertTrue(self.handler.can_handle(resource2))

    def test_can_handle_invalid_uri(self):
        """Test handler rejects invalid URIs."""
        # Invalid URIs
        resource1 = Resource(uri="odoo://res.partner/record/1", name="Partner Record")
        resource2 = Resource(uri="odoo://res.partner/search", name="Partner Search")

        self.assertFalse(self.handler.can_handle(resource1))
        self.assertFalse(self.handler.can_handle(resource2))

    def test_handle_success(self):
        """Test successful browse with patched formatter."""
        # Mock Odoo response
        self.odoo.read.return_value = [
            {"id": 1, "name": "Partner 1"},
            {"id": 2, "name": "Partner 2"},
        ]

        # Create a mock Resource
        resource = Resource(
            uri="odoo://res.partner/browse?ids=1,2", name="Browse Resource"
        )

        # Use patch to mock the formatter function
        with patch("mcp_server_odoo.resource_handlers.format_record") as mock_format:
            mock_format.return_value = "Formatted record"

            # Handle the resource
            result = self.handler.handle(resource)

            # Check result
            self.assertFalse(result["is_error"])
            self.assertEqual(
                result["content"][0]["text"], "Formatted record\n\nFormatted record"
            )

            # Verify Odoo method was called with the right parameters
            self.odoo.read.assert_called_once_with("res.partner", [1, 2], None)


class TestFieldsResourceHandlerFunctions(unittest.TestCase):
    """Test fields resource handler functionality."""

    def setUp(self):
        """Set up test environment."""
        self.odoo = MagicMock()
        self.handler = FieldsResourceHandler(self.odoo)

    def test_can_handle_valid_uri(self):
        """Test handler recognizes valid URIs."""
        # Valid URIs
        resource1 = Resource(uri="odoo://res.partner/fields", name="Partner Fields")
        resource2 = Resource(uri="odoo://product.product/fields", name="Product Fields")

        self.assertTrue(self.handler.can_handle(resource1))
        self.assertTrue(self.handler.can_handle(resource2))

    def test_can_handle_invalid_uri(self):
        """Test handler rejects invalid URIs."""
        # Invalid URIs
        resource1 = Resource(uri="odoo://res.partner/record/1", name="Partner Record")
        resource2 = Resource(uri="odoo://res.partner/field", name="Invalid Fields")

        self.assertFalse(self.handler.can_handle(resource1))
        self.assertFalse(self.handler.can_handle(resource2))

    def test_handle_success(self):
        """Test successful fields operation."""
        # Mock Odoo response
        self.odoo.get_model_fields.return_value = {
            "name": {"type": "char", "string": "Name"},
            "email": {"type": "char", "string": "Email"},
        }

        # Create a mock Resource
        resource = Resource(uri="odoo://res.partner/fields", name="Fields Resource")

        # Handle the resource
        result = self.handler.handle(resource)

        # Check result
        self.assertFalse(result["is_error"])
        self.assertIn("Fields for res.partner", result["content"][0]["text"])

        # Verify Odoo method was called with the right parameters
        self.odoo.get_model_fields.assert_called_once_with("res.partner")


if __name__ == "__main__":
    unittest.main()
