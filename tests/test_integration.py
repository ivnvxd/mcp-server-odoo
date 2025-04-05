"""Integration tests for MCP-Odoo server.

These tests verify that the MCP-Odoo server can connect to Odoo with
and without username/password authentication.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


class TestIntegration(unittest.TestCase):
    """Integration tests for MCP-Odoo server."""

    def setUp(self):
        """Set up test values."""
        # Use environment variables or defaults for integration tests
        self.url = os.environ.get("TEST_ODOO_URL", "http://localhost:8069")
        self.db = os.environ.get("TEST_ODOO_DB", "mcp")
        self.token = os.environ.get("TEST_ODOO_TOKEN", "test_token")
        self.username = os.environ.get("TEST_ODOO_USERNAME", "admin")
        self.password = os.environ.get("TEST_ODOO_PASSWORD", "admin")

    @unittest.skip("Skip unless running against real Odoo instance")
    def test_connection_with_username_password(self):
        """Test connection with username and password."""
        connection = OdooConnection(
            self.url, self.db, self.token, self.username, self.password
        )

        # Test server connection
        connection.common_endpoint.version()

        # Test authentication (will raise exception if it fails)
        uid = connection._authenticate()
        self.assertTrue(uid > 0, "Should return a valid user ID")

    @unittest.skip("Skip unless running against real Odoo instance")
    def test_connection_without_username_password(self):
        """Test connection without username and password."""
        connection = OdooConnection(self.url, self.db, self.token)

        # Test server connection
        connection.common_endpoint.version()

        # Test authentication (will raise exception if it fails)
        uid = connection._authenticate()
        self.assertTrue(uid > 0, "Should return a valid user ID")

    def test_fallback_mechanism(self):
        """Test that both auth methods are tried with proper error handling."""
        # Create a mock object to handle the ServerProxy initialization
        server_proxy_mock = MagicMock()

        # Instead of raising on instantiation, raise on method calls
        common_mock = MagicMock()
        common_mock.authenticate.side_effect = ConnectionError("Connection refused")

        object_mock = MagicMock()
        object_mock.execute_kw.side_effect = ConnectionError("Connection refused")

        # Define the side effect function for the ServerProxy mock
        def side_effect(url):
            if "common" in url:
                return common_mock
            return object_mock

        server_proxy_mock.side_effect = side_effect

        with patch("xmlrpc.client.ServerProxy", server_proxy_mock):
            # Create connection
            connection = OdooConnection(
                self.url, self.db, self.token, self.username, self.password
            )

            # Test authentication should raise proper exception
            with self.assertRaises(OdooConnectionError) as exc_info:
                connection._authenticate()

            # Verify the exception message
            self.assertIn("Authentication failed", str(exc_info.exception))


if __name__ == "__main__":
    unittest.main()
