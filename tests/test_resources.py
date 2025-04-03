"""Tests for MCP resource handlers."""

import unittest
from unittest.mock import MagicMock

from mcp import Resource

from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.resource_handlers import (
    BrowseResourceHandler,
    CountResourceHandler,
    FieldsResourceHandler,
    RecordResourceHandler,
    ResourceHandlerRegistry,
    SearchResourceHandler,
)


class TestResourceHandlers(unittest.TestCase):
    """Test cases for resource handlers."""

    def setUp(self):
        """Set up test environment."""
        # Create a mock OdooConnection
        self.odoo = MagicMock(spec=OdooConnection)

        # Create handler instances
        self.record_handler = RecordResourceHandler(self.odoo)
        self.search_handler = SearchResourceHandler(
            self.odoo, default_limit=10, max_limit=50
        )
        self.browse_handler = BrowseResourceHandler(self.odoo)
        self.count_handler = CountResourceHandler(self.odoo)
        self.fields_handler = FieldsResourceHandler(self.odoo)

        # Create registry
        self.registry = ResourceHandlerRegistry(
            self.odoo, default_limit=10, max_limit=50
        )

    def test_record_handler_can_handle(self):
        """Test RecordResourceHandler can_handle method."""
        # Valid URIs
        self.assertTrue(
            self.record_handler.can_handle(
                Resource(uri="odoo://res.partner/record/1", name="Record Resource")
            )
        )
        self.assertTrue(
            self.record_handler.can_handle(
                Resource(uri="odoo://product.template/record/42", name="Product Record")
            )
        )

        # Invalid URIs
        self.assertFalse(
            self.record_handler.can_handle(
                Resource(uri="odoo://res.partner/search", name="Search Resource")
            )
        )
        self.assertFalse(
            self.record_handler.can_handle(
                Resource(uri="odoo://res.partner/record/abc", name="Invalid Record")
            )
        )

    def test_search_handler_can_handle(self):
        """Test SearchResourceHandler can_handle method."""
        # Valid URIs
        self.assertTrue(
            self.search_handler.can_handle(
                Resource(uri="odoo://res.partner/search", name="Search Resource")
            )
        )
        self.assertTrue(
            self.search_handler.can_handle(
                Resource(
                    uri="odoo://res.partner/search?domain=[]", name="Search With Domain"
                )
            )
        )

        # Invalid URIs
        self.assertFalse(
            self.search_handler.can_handle(
                Resource(uri="odoo://res.partner/record/1", name="Record Resource")
            )
        )
        self.assertFalse(
            self.search_handler.can_handle(
                Resource(uri="odoo://res.partner/browse", name="Browse Resource")
            )
        )

    def test_browse_handler_can_handle(self):
        """Test BrowseResourceHandler can_handle method."""
        # Valid URIs
        self.assertTrue(
            self.browse_handler.can_handle(
                Resource(uri="odoo://res.partner/browse", name="Browse Resource")
            )
        )
        self.assertTrue(
            self.browse_handler.can_handle(
                Resource(
                    uri="odoo://res.partner/browse?ids=1,2,3", name="Browse With IDs"
                )
            )
        )

        # Invalid URIs
        self.assertFalse(
            self.browse_handler.can_handle(
                Resource(uri="odoo://res.partner/record/1", name="Record Resource")
            )
        )
        self.assertFalse(
            self.browse_handler.can_handle(
                Resource(uri="odoo://res.partner/search", name="Search Resource")
            )
        )

    def test_count_handler_can_handle(self):
        """Test CountResourceHandler can_handle method."""
        # Valid URIs
        self.assertTrue(
            self.count_handler.can_handle(
                Resource(uri="odoo://res.partner/count", name="Count Resource")
            )
        )
        self.assertTrue(
            self.count_handler.can_handle(
                Resource(
                    uri="odoo://res.partner/count?domain=[]", name="Count With Domain"
                )
            )
        )

        # Invalid URIs
        self.assertFalse(
            self.count_handler.can_handle(
                Resource(uri="odoo://res.partner/record/1", name="Record Resource")
            )
        )
        self.assertFalse(
            self.count_handler.can_handle(
                Resource(uri="odoo://res.partner/search", name="Search Resource")
            )
        )

    def test_fields_handler_can_handle(self):
        """Test FieldsResourceHandler can_handle method."""
        # Valid URIs
        self.assertTrue(
            self.fields_handler.can_handle(
                Resource(uri="odoo://res.partner/fields", name="Fields Resource")
            )
        )

        # Invalid URIs
        self.assertFalse(
            self.fields_handler.can_handle(
                Resource(uri="odoo://res.partner/record/1", name="Record Resource")
            )
        )
        self.assertFalse(
            self.fields_handler.can_handle(
                Resource(
                    uri="odoo://res.partner/fields/name", name="Invalid Fields Resource"
                )
            )
        )

    def test_registry_dispatches_to_correct_handler(self):
        """Test that the registry dispatches to the correct handler."""
        # Mock handlers
        self.registry.handlers = [MagicMock() for _ in range(3)]

        # Set up first handler to handle the resource
        self.registry.handlers[0].can_handle.return_value = True
        expected_result = MagicMock()
        self.registry.handlers[0].handle.return_value = expected_result

        # Set up other handlers to not handle the resource
        for i in range(1, 3):
            self.registry.handlers[i].can_handle.return_value = False

        # Call handle_resource
        resource = Resource(uri="test://resource", name="Test Resource")
        result = self.registry.handle_resource(resource)

        # Check that the first handler was called
        self.registry.handlers[0].can_handle.assert_called_once_with(resource)
        self.registry.handlers[0].handle.assert_called_once_with(resource)

        # Check that the result is correct
        self.assertEqual(result, expected_result)

    def test_registry_raises_error_if_no_handler(self):
        """Test that the registry raises an error if no handler can handle the resource."""
        # Set up all handlers to not handle the resource
        self.registry.handlers = [MagicMock() for _ in range(3)]
        for handler in self.registry.handlers:
            handler.can_handle.return_value = False

        # Call handle_resource
        resource = Resource(uri="test://resource", name="Test Resource")

        # Check that an error is raised
        with self.assertRaises(Exception):
            self.registry.handle_resource(resource)

        # Check that all handlers were checked
        for handler in self.registry.handlers:
            handler.can_handle.assert_called_once_with(resource)
            handler.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
