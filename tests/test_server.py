"""Tests for the MCP server implementation."""

import os
import unittest
from unittest.mock import MagicMock, patch

import pytest
import requests

from mcp_server_odoo.server import MCPOdooServer


class TestMCPOdooServer(unittest.TestCase):
    """Test cases for the MCP Odoo Server."""

    def setUp(self):
        """Set up test environment."""
        # Mock the OdooConnection
        self.odoo_mock = MagicMock()
        self.odoo_patcher = patch(
            "mcp_server_odoo.server.OdooConnection", return_value=self.odoo_mock
        )
        self.odoo_class_mock = self.odoo_patcher.start()

        # Mock FastMCP
        self.fastmcp_mock = MagicMock()
        self.fastmcp_patcher = patch(
            "mcp_server_odoo.server.FastMCP", return_value=self.fastmcp_mock
        )
        self.fastmcp_class_mock = self.fastmcp_patcher.start()

        # Create the server instance
        self.server = MCPOdooServer(
            odoo_url="http://test.odoo.com",
            odoo_db="test_db",
            odoo_token="test_token",
        )

    def tearDown(self):
        """Clean up after tests."""
        self.odoo_patcher.stop()
        self.fastmcp_patcher.stop()

    def test_initialization(self):
        """Test server initialization."""
        # Check that the server's attributes were set correctly
        self.assertEqual(self.server.odoo_url, "http://test.odoo.com")
        self.assertEqual(self.server.odoo_db, "test_db")
        self.assertEqual(self.server.odoo_token, "test_token")
        self.assertEqual(self.server.default_limit, 50)
        self.assertEqual(self.server.max_limit, 100)

        # Check that the components were initialized
        self.assertIsNotNone(self.server.odoo)
        self.assertIsNotNone(self.server.mcp)

        # Check FastMCP was initialized with correct name
        self.fastmcp_class_mock.assert_called_once_with("Odoo")

        # Check odoo connection test was called
        self.odoo_mock.test_connection.assert_called_once()

    def test_register_methods_called(self):
        """Test that resource and tool registration methods are called."""
        # Check that registration methods were called
        self.assertTrue(hasattr(self.server, "_register_resources"))
        self.assertTrue(hasattr(self.server, "_register_tools"))

    def test_start_method(self):
        """Test the start method."""
        # Create a fresh mock
        self.server.mcp = MagicMock()

        # Call start method
        self.server.start()

        # Verify FastMCP's run method was called
        self.server.mcp.run.assert_called_once()

    def test_error_handling_during_start(self):
        """Test error handling in the start method."""
        # Create a fresh mock that raises an exception
        self.server.mcp = MagicMock()
        self.server.mcp.run.side_effect = Exception("Test error")

        # Call start method and expect exception to propagate
        with self.assertRaises(Exception):
            self.server.start()


@pytest.fixture
def clean_env_vars():
    """Clean up test environment variables."""
    # Save original env vars
    original_env = {}
    keys_to_preserve = [
        "ODOO_URL",
        "ODOO_DB",
        "ODOO_MCP_TOKEN",
        "ODOO_USERNAME",
        "ODOO_PASSWORD",
        "ODOO_MCP_LOG_LEVEL",
        "ODOO_MCP_DEFAULT_LIMIT",
        "ODOO_MCP_MAX_LIMIT",
    ]

    for key in keys_to_preserve:
        if key in os.environ:
            original_env[key] = os.environ[key]
            del os.environ[key]

    yield

    # Restore original env vars
    for key in keys_to_preserve:
        if key in os.environ:
            del os.environ[key]

    for key, value in original_env.items():
        os.environ[key] = value


@pytest.fixture
def mock_main_module():
    """Mock __main__ module for testing."""
    with (
        patch("sys.exit"),
        patch("mcp_server_odoo.server.MCPOdooServer") as mock_server_class,
    ):
        mock_server = MagicMock()
        mock_server_class.return_value = mock_server
        yield mock_server_class


def test_env_var_config_defaults(clean_env_vars, mock_main_module):
    """Test config from environment variables with defaults."""
    # Setup minimal required env vars
    os.environ["ODOO_URL"] = "http://test.odoo.com"
    os.environ["ODOO_DB"] = "test_db"
    os.environ["ODOO_MCP_TOKEN"] = "test_token"

    # Import after setting env vars
    from mcp_server_odoo.__main__ import get_config, main

    # Mock args
    args_mock = MagicMock()
    args_mock.url = None
    args_mock.db = None
    args_mock.token = None
    args_mock.username = None
    args_mock.password = None
    args_mock.log_level = None
    args_mock.default_limit = None
    args_mock.max_limit = None
    args_mock.env_file = None

    # Get config
    config = get_config(args_mock)

    # Run main
    main()

    # Check server initialization
    mock_main_module.assert_called_once_with(
        odoo_url="http://test.odoo.com",
        odoo_db="test_db",
        odoo_token="test_token",
        odoo_username=None,
        odoo_password=None,
        default_limit=50,
        max_limit=100,
    )

    # Check default values
    assert config["default_limit"] == 50
    assert config["max_limit"] == 100
    assert config["log_level"] == "INFO"


def test_env_var_config_custom_limits(clean_env_vars, mock_main_module):
    """Test config from environment variables with custom limits."""
    # Setup all env vars including custom limits
    os.environ["ODOO_URL"] = "http://test.odoo.com"
    os.environ["ODOO_DB"] = "test_db"
    os.environ["ODOO_MCP_TOKEN"] = "test_token"
    os.environ["ODOO_USERNAME"] = "admin"
    os.environ["ODOO_PASSWORD"] = "password"
    os.environ["ODOO_MCP_LOG_LEVEL"] = "DEBUG"
    os.environ["ODOO_MCP_DEFAULT_LIMIT"] = "25"
    os.environ["ODOO_MCP_MAX_LIMIT"] = "200"

    # Import after setting env vars
    from mcp_server_odoo.__main__ import get_config, main

    # Directly test get_config
    args_mock = MagicMock()
    args_mock.url = None
    args_mock.db = None
    args_mock.token = None
    args_mock.username = None
    args_mock.password = None
    args_mock.log_level = None
    args_mock.default_limit = None
    args_mock.max_limit = None
    args_mock.env_file = None

    # Get config directly
    config = get_config(args_mock)

    # Verify config values
    assert config["url"] == "http://test.odoo.com"
    assert config["db"] == "test_db"
    assert config["token"] == "test_token"
    assert config["username"] == "admin"
    assert config["password"] == "password"
    assert config["default_limit"] == 25
    assert config["max_limit"] == 200

    # Run main with patched functions
    with (
        patch("mcp_server_odoo.__main__.parse_args", return_value=args_mock),
        patch("mcp_server_odoo.__main__.MCPOdooServer") as mock_server,
    ):
        # Set up mock server
        mock_server_instance = MagicMock()
        mock_server.return_value = mock_server_instance

        main()

        # Verify server initialization
        mock_server.assert_called_once_with(
            odoo_url="http://test.odoo.com",
            odoo_db="test_db",
            odoo_token="test_token",
            odoo_username="admin",
            odoo_password="password",
            default_limit=25,
            max_limit=200,
        )
        mock_server_instance.start.assert_called_once()


def test_missing_required_config(clean_env_vars, mock_main_module):
    """Test handling of missing required config."""
    # No env vars set - should cause error

    # Import main module
    from mcp_server_odoo.__main__ import get_config

    # Mock args with missing required items
    args_mock = MagicMock()
    args_mock.url = None  # Missing URL
    args_mock.db = "test_db"
    args_mock.token = "test_token"
    args_mock.username = None
    args_mock.password = None
    args_mock.log_level = None
    args_mock.default_limit = None
    args_mock.max_limit = None
    args_mock.env_file = None

    # get_config should raise ValueError
    with pytest.raises(ValueError):
        get_config(args_mock)


def test_db_autodetection(clean_env_vars, mock_main_module):
    """Test database auto-detection."""
    # Set minimal env vars but omit DB
    os.environ["ODOO_URL"] = "http://test.odoo.com"
    os.environ["ODOO_MCP_TOKEN"] = "test_token"

    # Import main module
    from mcp_server_odoo.__main__ import detect_default_database, main

    # Mock the detect_default_database function to return a known value
    original_detect = detect_default_database

    def mock_detect(url):
        assert url == "http://test.odoo.com"
        return "auto_detected_db"

    # Patch the function
    from mcp_server_odoo import __main__ as main_module

    main_module.detect_default_database = mock_detect

    try:
        # Create mock args
        args_mock = MagicMock()
        args_mock.url = None
        args_mock.db = None
        args_mock.token = None
        args_mock.username = None
        args_mock.password = None
        args_mock.log_level = None
        args_mock.default_limit = None
        args_mock.max_limit = None
        args_mock.env_file = None

        # Run main with patched functions
        with (
            patch("mcp_server_odoo.__main__.parse_args", return_value=args_mock),
            patch("mcp_server_odoo.__main__.MCPOdooServer") as mock_server,
        ):
            # Set up mock server
            mock_server_instance = MagicMock()
            mock_server.return_value = mock_server_instance

            main()

            # Verify server initialization
            mock_server.assert_called_once_with(
                odoo_url="http://test.odoo.com",
                odoo_db="auto_detected_db",
                odoo_token="test_token",
                odoo_username=None,
                odoo_password=None,
                default_limit=50,
                max_limit=100,
            )
            mock_server_instance.start.assert_called_once()
    finally:
        # Restore original function
        main_module.detect_default_database = original_detect


def test_db_autodetection_failure(clean_env_vars, mock_main_module):
    """Test handling of failed database auto-detection."""
    # Set minimal env vars but omit DB
    os.environ["ODOO_URL"] = "http://test.odoo.com"
    os.environ["ODOO_MCP_TOKEN"] = "test_token"

    # Import main module
    from mcp_server_odoo.__main__ import main

    # Create mock args
    args_mock = MagicMock()
    args_mock.url = None
    args_mock.db = None
    args_mock.token = None
    args_mock.username = None
    args_mock.password = None
    args_mock.log_level = None
    args_mock.default_limit = None
    args_mock.max_limit = None
    args_mock.env_file = None

    # Mock the config object that would be returned by get_config
    config_mock = {
        "url": "http://test.odoo.com",
        "db": "",  # Empty db value to simulate auto-detection
        "token": "test_token",
        "username": None,
        "password": None,
        "default_limit": 50,
        "max_limit": 100,
    }

    # Run main with mocked functions
    with (
        patch("sys.exit") as mock_exit,
        patch("mcp_server_odoo.__main__.parse_args", return_value=args_mock),
        patch("mcp_server_odoo.__main__.get_config", return_value=config_mock),
        patch(
            "mcp_server_odoo.__main__.detect_default_database", return_value=""
        ),  # Return empty db
    ):
        main()
        # Verify it exited with error code 1
        mock_exit.assert_called_once_with(1)


def test_detect_default_database(clean_env_vars, mock_main_module):
    """Test the actual detect_default_database function."""
    from mcp_server_odoo.__main__ import detect_default_database

    # Test successful detection from JSON response
    with patch("requests.get") as mock_get:
        # Mock a successful response with database list
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": ["test_db", "other_db"]}
        mock_get.return_value = mock_response

        result = detect_default_database("http://test.odoo.com")
        assert result == "test_db"
        assert mock_get.call_count == 1
        mock_get.assert_called_with("http://test.odoo.com/web/database/list", timeout=5)

    # Test fallback to login page
    with patch("requests.get") as mock_get:
        # First response is not JSON
        first_response = MagicMock()
        first_response.status_code = 200
        first_response.json.side_effect = ValueError("Not JSON")

        # Second response has DB in HTML
        second_response = MagicMock()
        second_response.status_code = 200
        second_response.text = '<input name="db" value="login_page_db">'

        def get_side_effect(url, **kwargs):
            if "database/list" in url:
                return first_response
            return second_response

        mock_get.side_effect = get_side_effect

        result = detect_default_database("http://test.odoo.com")
        assert result == "login_page_db"
        assert mock_get.call_count == 2

    # Test all detection methods fail
    with patch("requests.get") as mock_get:
        # First request fails
        mock_get.side_effect = requests.RequestException("Connection error")

        result = detect_default_database("http://test.odoo.com")
        assert result == ""
        # It will try both endpoints, so expect 2 calls
        assert mock_get.call_count == 2


if __name__ == "__main__":
    unittest.main()
