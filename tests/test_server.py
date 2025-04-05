"""Tests for the MCP server implementation."""

import os
import unittest
from unittest.mock import MagicMock, patch

import pytest
from mcp import Resource
import mcp.server
import mcp.server.stdio

from mcp_server_odoo.server import MCPOdooServer, odoo_lifespan


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

    def test_initialization_with_defaults(self):
        """Test server initialization with default values."""
        # Create a server with minimal required parameters
        server = MCPOdooServer(
            odoo_url="http://test.odoo.com",
            odoo_db="test_db",
            odoo_token="test_token",
        )

        # Check default values were applied
        self.assertEqual(server.default_limit, 20)
        self.assertEqual(server.max_limit, 100)

    def test_get_capabilities(self):
        """Test that the server returns correct capabilities."""
        capabilities = self.server.get_capabilities()

        # Check resource capabilities
        self.assertIsNotNone(capabilities.resources)
        self.assertTrue(capabilities.resources.listResources)

        # Check that tools and prompts are disabled
        self.assertIsNone(capabilities.tools)
        self.assertIsNone(capabilities.prompts)

        # Check logging capability
        self.assertIsNotNone(capabilities.logging)
        self.assertEqual(capabilities.logging.verbosity, "info")


@pytest.mark.asyncio
async def test_odoo_lifespan():
    """Test the odoo_lifespan context manager."""
    # Mock the server
    server_mock = MagicMock()

    # Mock OdooConnection
    odoo_mock = MagicMock()

    # Mock ResourceHandlerRegistry
    registry_mock = MagicMock()

    with (
        patch(
            "mcp_server_odoo.server.OdooConnection", return_value=odoo_mock
        ) as odoo_class_mock,
        patch(
            "mcp_server_odoo.server.ResourceHandlerRegistry", return_value=registry_mock
        ) as registry_class_mock,
    ):
        # Test successful lifespan execution
        async with odoo_lifespan(
            server_mock, "http://test.odoo.com", "test_db", "test_token"
        ) as context:
            # Check that connection was tested
            odoo_mock.test_connection.assert_called_once()

            # Check that context contains expected objects
            assert context.odoo == odoo_mock
            assert context.registry == registry_mock

            # Check registry was created with correct parameters
            registry_class_mock.assert_called_once_with(
                odoo=odoo_mock, default_limit=20, max_limit=100
            )


@pytest.mark.asyncio
async def test_odoo_lifespan_error_handling():
    """Test error handling in odoo_lifespan."""
    # Mock the server
    server_mock = MagicMock()

    # Mock OdooConnection
    odoo_mock = MagicMock()
    odoo_mock.test_connection.side_effect = Exception("Connection failed")

    with patch("mcp_server_odoo.server.OdooConnection", return_value=odoo_mock):
        # Test exception during lifespan execution
        with pytest.raises(Exception, match="Connection failed"):
            async with odoo_lifespan(
                server_mock, "http://test.odoo.com", "test_db", "test_token"
            ):
                pass


def test_env_var_config_defaults(env_vars_cleanup):
    """Test environment variable configuration with defaults."""
    # Reset environment variables
    for key in list(os.environ.keys()):
        if key.startswith("ODOO_"):
            del os.environ[key]

    # Set basic environment variables
    os.environ["ODOO_URL"] = "http://env.odoo.com"
    os.environ["ODOO_DB"] = "env_db"
    os.environ["ODOO_MCP_TOKEN"] = "env_token"

    # Import the module fresh
    from importlib import import_module
    import sys

    if "mcp_server_odoo.__main__" in sys.modules:
        del sys.modules["mcp_server_odoo.__main__"]
    main_module = import_module("mcp_server_odoo.__main__")

    # Create mock args
    args = MagicMock()
    args.url = None
    args.db = None
    args.token = None
    args.log_level = None
    args.default_limit = None
    args.max_limit = None
    args.env_file = None

    # Call get_config
    config = main_module.get_config(args)

    # Verify config
    assert config["url"] == "http://env.odoo.com"
    assert config["db"] == "env_db"
    assert config["token"] == "env_token"
    assert "default_limit" not in config or config["default_limit"] == 20
    assert "max_limit" not in config or config["max_limit"] == 100


def test_env_var_config_custom_limits(env_vars_cleanup):
    """Test environment variable configuration with custom limits."""
    # Reset environment variables
    for key in list(os.environ.keys()):
        if key.startswith("ODOO_"):
            del os.environ[key]

    # Set environment variables with custom limits
    os.environ["ODOO_URL"] = "http://env.odoo.com"
    os.environ["ODOO_DB"] = "env_db"
    os.environ["ODOO_MCP_TOKEN"] = "env_token"
    os.environ["ODOO_MCP_DEFAULT_LIMIT"] = "30"
    os.environ["ODOO_MCP_MAX_LIMIT"] = "200"

    # Import the module fresh
    from importlib import import_module
    import sys

    if "mcp_server_odoo.__main__" in sys.modules:
        del sys.modules["mcp_server_odoo.__main__"]
    main_module = import_module("mcp_server_odoo.__main__")

    # Create mock args
    args = MagicMock()
    args.url = None
    args.db = None
    args.token = None
    args.log_level = None
    args.default_limit = None
    args.max_limit = None
    args.env_file = None

    # Call get_config
    config = main_module.get_config(args)

    # Verify config
    assert config["url"] == "http://env.odoo.com"
    assert config["db"] == "env_db"
    assert config["token"] == "env_token"
    assert config["default_limit"] == 30
    assert config["max_limit"] == 200


def test_missing_required_config(env_vars_cleanup):
    """Test error handling when required configuration is missing."""
    # Reset environment variables
    for key in list(os.environ.keys()):
        if key.startswith("ODOO_"):
            del os.environ[key]

    # Import the main function directly
    from importlib import import_module
    import sys

    if "mcp_server_odoo.__main__" in sys.modules:
        del sys.modules["mcp_server_odoo.__main__"]
    main_module = import_module("mcp_server_odoo.__main__")

    # Create mock args with missing URL
    args = MagicMock()
    args.url = None
    args.db = "test_db"
    args.token = "test_token"
    args.log_level = None
    args.default_limit = None
    args.max_limit = None
    args.env_file = None

    # Call get_config and check for ValueError
    with pytest.raises(ValueError) as excinfo:
        main_module.get_config(args)

    # Check error message
    assert "Missing required configuration" in str(excinfo.value)
    assert "ODOO_URL" in str(excinfo.value)


if __name__ == "__main__":
    unittest.main()
