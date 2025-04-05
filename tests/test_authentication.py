"""Tests for authentication methods in the Odoo connection."""

import unittest
from unittest.mock import MagicMock, patch

from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


class TestAuthentication(unittest.TestCase):
    """Test authentication methods with and without username/password."""

    def setUp(self):
        """Set up test data for each test."""
        self.url = "http://test.example.com"
        self.db = "test_db"
        self.token = "test_token"
        self.username = "test_user"
        self.password = "test_password"

        # Create mock for ServerProxy
        self.mock_common = MagicMock()
        self.mock_object = MagicMock()

        # Add test.db attribute to mock_object so it passes the assert check
        self.mock_object.execute_kw.return_value = 5

    @patch("xmlrpc.client.ServerProxy")
    def test_auth_with_username_password(self, mock_server_proxy):
        """Test authentication with username and password."""
        # Configure mocks
        mock_server_proxy.side_effect = [self.mock_common, self.mock_object]
        self.mock_common.authenticate.return_value = 5  # User ID

        # Create connection with username and password
        connection = OdooConnection(
            self.url, self.db, self.token, self.username, self.password
        )

        # Test authentication
        uid = connection._authenticate()

        # Verify
        self.assertEqual(uid, 5, "Should return the authenticated user ID")
        self.mock_common.authenticate.assert_called_once_with(
            self.db, self.username, self.password, {}
        )
        self.mock_object.execute_kw.assert_called_once()

        # Extract and verify parameters from the call
        args = self.mock_object.execute_kw.call_args[0]
        self.assertEqual(args[0], self.db, "Database name should match")
        self.assertEqual(args[1], 5, "User ID should match")
        self.assertEqual(args[2], self.password, "Password should match")
        self.assertEqual(args[3], "mcp.server", "Model should be mcp.server")
        self.assertEqual(
            args[4], "authenticate_token", "Method should be authenticate_token"
        )
        self.assertEqual(args[5], [self.token], "Token should be in args list")

    @patch("xmlrpc.client.ServerProxy")
    def test_auth_without_username_password(self, mock_server_proxy):
        """Test authentication without username and password."""
        # Configure mocks
        mock_server_proxy.side_effect = [self.mock_common, self.mock_object]

        # Create connection without username and password
        connection = OdooConnection(self.url, self.db, self.token)

        # Test authentication
        uid = connection._authenticate()

        # Verify
        self.assertEqual(uid, 5, "Should return the authenticated user ID")
        self.mock_common.authenticate.assert_not_called()
        self.mock_object.execute_kw.assert_called_once()

        # Extract and verify parameters from the call
        args = self.mock_object.execute_kw.call_args[0]
        self.assertEqual(args[0], self.db, "Database name should match")
        self.assertEqual(args[1], 1, "Default admin ID should be 1")
        self.assertEqual(args[2], "admin", "Password should be 'admin'")
        self.assertEqual(args[3], "mcp.server", "Model should be mcp.server")
        self.assertEqual(
            args[4], "authenticate_token", "Method should be authenticate_token"
        )
        self.assertEqual(args[5], [self.token], "Token should be in args list")

    @patch("xmlrpc.client.ServerProxy")
    def test_auth_with_invalid_token(self, mock_server_proxy):
        """Test authentication with invalid token."""
        # Configure mocks
        mock_server_proxy.side_effect = [self.mock_common, self.mock_object]
        self.mock_object.execute_kw.return_value = False  # Invalid token

        # Create connection without username and password
        connection = OdooConnection(self.url, self.db, self.token)

        # Test authentication should raise exception
        with self.assertRaises(OdooConnectionError):
            connection._authenticate()

    @patch("xmlrpc.client.ServerProxy")
    def test_auth_with_username_authentication_failure(self, mock_server_proxy):
        """Test error when username authentication fails."""
        # Configure mocks
        mock_server_proxy.side_effect = [self.mock_common, self.mock_object]
        self.mock_common.authenticate.return_value = False  # Auth failed

        # Create connection with username and password
        connection = OdooConnection(
            self.url, self.db, self.token, self.username, self.password
        )

        # Test authentication should raise exception
        with self.assertRaises(OdooConnectionError):
            connection._authenticate()

    @patch("xmlrpc.client.ServerProxy")
    def test_connection_retry_with_empty_password(self, mock_server_proxy):
        """Test execute_with_retry works with empty password."""
        # Configure mocks
        mock_server_proxy.side_effect = [self.mock_common, self.mock_object]
        self.mock_object.execute_kw.return_value = {"result": "success"}

        # Create connection without username and password
        connection = OdooConnection(self.url, self.db, self.token)

        # Test execute_with_retry
        result = connection._execute_with_retry("test.model", "test_method", [1, 2, 3])

        # Verify
        self.assertEqual(result, {"result": "success"})
        self.mock_object.execute_kw.assert_called_once()

        # Extract and verify parameters from the call
        args = self.mock_object.execute_kw.call_args[0]
        self.assertEqual(args[0], self.db, "Database name should match")
        self.assertEqual(args[1], 1, "Default admin ID should be 1")
        self.assertEqual(args[2], "admin", "Password should be 'admin'")
        self.assertEqual(args[3], "test.model", "Model should match")
        self.assertEqual(args[4], "test_method", "Method should match")


if __name__ == "__main__":
    unittest.main()
