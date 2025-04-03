"""Tests for the MCP server implementation."""

import unittest
from unittest.mock import MagicMock, patch

from mcp import Resource
import mcp.server

from mcp_server_odoo.server import MCPOdooServer


class TestMCPOdooServer(unittest.TestCase):
    """Test cases for the MCP Odoo Server."""

    def setUp(self):
        """Set up test environment."""
        # Mock the OdooConnection first
        self.odoo_mock = MagicMock()
        self.odoo_patcher = patch(
            "mcp_server_odoo.server.OdooConnection", return_value=self.odoo_mock
        )
        self.odoo_class_mock = self.odoo_patcher.start()

        # Mock the ResourceHandlerRegistry
        self.registry_mock = MagicMock()
        self.registry_patcher = patch(
            "mcp_server_odoo.server.ResourceHandlerRegistry",
            return_value=self.registry_mock,
        )
        self.registry_class_mock = self.registry_patcher.start()

        # Mock the Server class
        self.mcp_server_mock = MagicMock()
        self.mcp_server_patcher = patch.object(
            mcp.server, "Server", return_value=self.mcp_server_mock
        )
        self.mcp_server_class_mock = self.mcp_server_patcher.start()

        # Create the server instance
        self.server = MCPOdooServer(
            odoo_url="http://test.odoo.com",
            odoo_db="test_db",
            odoo_token="test_token",
        )

    def tearDown(self):
        """Clean up after tests."""
        self.odoo_patcher.stop()
        self.registry_patcher.stop()
        self.mcp_server_patcher.stop()

    def test_initialization(self):
        """Test server initialization."""
        # Check that the server's attributes were set correctly
        self.assertEqual(self.server.odoo_url, "http://test.odoo.com")
        self.assertEqual(self.server.odoo_db, "test_db")
        self.assertEqual(self.server.odoo_token, "test_token")
        self.assertEqual(self.server.default_limit, 20)
        self.assertEqual(self.server.max_limit, 100)

        # Check that the components were initialized
        self.assertIsNotNone(self.server.odoo)
        self.assertIsNotNone(self.server.registry)
        self.assertIsNotNone(self.server.server)

        # Check that the resource handler was set
        self.assertEqual(self.server.server.on_resource, self.server._handle_resource)

    def test_handle_resource(self):
        """Test resource handling."""
        # Create a test resource
        resource = Resource(uri="odoo://res.partner/record/1", name="Test Resource")

        # Set up registry mock to return a resource content
        expected_content = {
            "is_error": False,
            "content": [{"type": "text", "text": "Test content"}],
        }
        self.registry_mock.handle_resource.return_value = expected_content

        # Call the handler
        result = self.server._handle_resource(resource)

        # Check that the registry was called
        self.registry_mock.handle_resource.assert_called_once_with(resource)

        # Check the result
        self.assertEqual(result, expected_content)

    def test_handle_resource_error(self):
        """Test error handling in resource handler."""
        # Create a test resource
        resource = Resource(uri="odoo://res.partner/record/1", name="Test Resource")

        # Set up registry mock to raise an exception
        self.registry_mock.handle_resource.side_effect = ValueError("Test error")

        # Call the handler
        result = self.server._handle_resource(resource)

        # Check that the registry was called
        self.registry_mock.handle_resource.assert_called_once_with(resource)

        # Check the result
        self.assertTrue(result["is_error"])
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIn("Error", result["content"][0]["text"])
        self.assertIn("Test error", result["content"][0]["text"])

    @patch("mcp_server_odoo.server.sys.exit")
    @patch("mcp_server_odoo.server.asyncio.run")
    def test_start(self, asyncio_run_mock, exit_mock):
        """Test server start method."""
        # Mock asyncio.run to just return a value instead of trying to run the coroutine
        asyncio_run_mock.return_value = None

        # Save the original run method to restore it later
        original_run = self.server.server.run

        try:
            # Replace server.run with a MagicMock to avoid coroutine warning
            self.server.server.run = MagicMock()

            # Call start
            self.server.start()

            # Check that asyncio.run was called
            asyncio_run_mock.assert_called_once()
            # Check that exit wasn't called
            exit_mock.assert_not_called()
        finally:
            # Restore the original run method
            self.server.server.run = original_run


if __name__ == "__main__":
    unittest.main()
