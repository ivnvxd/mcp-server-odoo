"""Tests for XML-RPC communication layer in OdooConnection.

Focuses on execute_kw/execute wrappers, error mapping (XML-RPC faults,
timeouts), and credential routing. CRUD operation tests (search, read,
create, write, unlink) are in test_odoo_connection_crud.py.
"""

import os
import socket
from functools import wraps
from unittest.mock import Mock
from xmlrpc.client import Fault

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError

from .conftest import ODOO_SERVER_AVAILABLE


def skip_on_rate_limit(func):
    """Decorator to skip test if rate limited. Works with both sync and async tests."""
    import asyncio

    if asyncio.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except (OdooConnectionError, Fault) as e:
                if "429" in str(e) or "too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

        return async_wrapper

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (OdooConnectionError, Fault) as e:
            if "429" in str(e) or "too many requests" in str(e).lower():
                pytest.skip("Rate limited by server")
            raise

    return wrapper


class TestXMLRPCOperations:
    """Test XML-RPC operations functionality."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="test_api_key",
            database=os.getenv("ODOO_DB"),
        )

    @pytest.fixture
    def authenticated_connection(self, config):
        """Create authenticated connection."""
        conn = OdooConnection(config)
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = os.getenv("ODOO_DB", "db")
        conn._auth_method = "api_key"
        return conn

    def test_execute_not_authenticated(self, config):
        """Test execute raises error when not authenticated."""
        conn = OdooConnection(config)
        conn._connected = True

        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            conn.execute("res.partner", "search", [])

    def test_execute_not_connected(self, config):
        """Test execute raises error when not connected."""
        conn = OdooConnection(config)
        conn._authenticated = True

        with pytest.raises(OdooConnectionError, match="Not connected"):
            conn.execute("res.partner", "search", [])

    def test_execute_kw_xmlrpc_fault(self, authenticated_connection):
        """Test execute_kw handles XML-RPC fault."""
        # Mock object proxy
        mock_proxy = Mock()
        mock_proxy.execute_kw.side_effect = Fault(1, "Access Denied")
        authenticated_connection._object_proxy = mock_proxy

        # Should raise error with sanitized message
        with pytest.raises(
            OdooConnectionError,
            match="Access denied: Invalid credentials or insufficient permissions",
        ):
            authenticated_connection.execute_kw("res.partner", "unlink", [[1]], {})

    def test_execute_kw_timeout(self, authenticated_connection):
        """Test execute_kw handles timeout."""
        # Mock object proxy
        mock_proxy = Mock()
        mock_proxy.execute_kw.side_effect = socket.timeout()
        authenticated_connection._object_proxy = mock_proxy

        # Should raise timeout error
        with pytest.raises(OdooConnectionError, match="timeout"):
            authenticated_connection.execute_kw("res.partner", "search", [[]], {})

    def test_password_auth_uses_password(self, config):
        """Test that password auth uses password for execute_kw."""
        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password="admin123",
            database=os.getenv("ODOO_DB"),
        )
        conn = OdooConnection(config)
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = os.getenv("ODOO_DB", "db")
        conn._auth_method = "password"

        # Mock object proxy
        mock_proxy = Mock()
        mock_proxy.execute_kw.return_value = []
        conn._object_proxy = mock_proxy

        # Execute
        conn.search("res.partner", [])

        # Verify password was used
        mock_proxy.execute_kw.assert_called_once_with(
            os.getenv("ODOO_DB", "db"), 2, "admin123", "res.partner", "search", [[]], {}
        )


@pytest.mark.yolo
@pytest.mark.skipif(not ODOO_SERVER_AVAILABLE, reason="Odoo server not available")
class TestXMLRPCOperationsIntegration:
    """Integration tests with real Odoo server."""

    @pytest.fixture
    def real_config(self):
        """Create configuration with real credentials."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key=os.getenv("ODOO_API_KEY") or None,
            username=os.getenv("ODOO_USER") or None,
            password=os.getenv("ODOO_PASSWORD") or None,
            database=None,  # Auto-select
            yolo_mode=os.getenv("ODOO_YOLO", "off"),
        )

    @skip_on_rate_limit
    def test_real_search_partners(self, real_config):
        """Test searching partners on real server."""
        with OdooConnection(real_config) as conn:
            conn.authenticate()

            # Search for companies
            partner_ids = conn.search("res.partner", [["is_company", "=", True]], limit=5)

            assert isinstance(partner_ids, list)
            print(f"Found {len(partner_ids)} company partners")

    @skip_on_rate_limit
    def test_real_read_partners(self, real_config):
        """Test reading partner data on real server."""
        with OdooConnection(real_config) as conn:
            try:
                conn.authenticate()
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

            # Search for a partner
            partner_ids = conn.search("res.partner", [], limit=1)

            if partner_ids:
                # Read partner data
                partners = conn.read("res.partner", partner_ids, ["name", "email", "is_company"])

                assert len(partners) == 1
                assert "name" in partners[0]
                print(f"Partner: {partners[0].get('name')}")

    @skip_on_rate_limit
    def test_real_search_read_partners(self, real_config):
        """Test search_read on real server."""
        with OdooConnection(real_config) as conn:
            try:
                conn.authenticate()
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

            # Search and read in one operation
            partners = conn.search_read(
                "res.partner", [["is_company", "=", True]], ["name", "email", "phone"], limit=3
            )

            assert isinstance(partners, list)
            for partner in partners:
                assert "name" in partner
                print(f"Company: {partner.get('name')}")

    @skip_on_rate_limit
    def test_real_fields_get(self, real_config):
        """Test getting field definitions on real server."""
        with OdooConnection(real_config) as conn:
            try:
                conn.authenticate()
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

            # Get partner fields
            fields = conn.fields_get("res.partner", ["string", "type", "required"])

            assert isinstance(fields, dict)
            assert "name" in fields
            assert fields["name"]["type"] == "char"
            print(f"Found {len(fields)} fields in res.partner")

    @skip_on_rate_limit
    def test_real_search_count(self, real_config):
        """Test counting records on real server."""
        with OdooConnection(real_config) as conn:
            try:
                conn.authenticate()
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

            # Count all partners
            total_count = conn.search_count("res.partner", [])

            # Count companies
            company_count = conn.search_count("res.partner", [["is_company", "=", True]])

            assert total_count >= company_count
            print(f"Total partners: {total_count}, Companies: {company_count}")

    @skip_on_rate_limit
    def test_real_execute_method(self, real_config):
        """Test generic execute method on real server."""
        with OdooConnection(real_config) as conn:
            try:
                conn.authenticate()
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

            # Use execute to call name_search
            result = conn.execute(
                "res.partner",
                "name_search",
                "Admin",  # search term
                [],  # domain
                "ilike",  # operator
                100,  # limit
            )

            assert isinstance(result, list)
            print(f"Name search returned {len(result)} results")

    def test_real_error_handling(self, real_config):
        """Test error handling with real server."""
        with OdooConnection(real_config) as conn:
            try:
                conn.authenticate()
            except OdooConnectionError as e:
                if "429" in str(e) or "Too many requests" in str(e).lower():
                    pytest.skip("Rate limited by server")
                raise

            # Try to access non-existent model
            with pytest.raises(OdooConnectionError):
                conn.search("non.existent.model", [])

            # Try invalid method
            with pytest.raises(OdooConnectionError):
                conn.execute("res.partner", "invalid_method")


if __name__ == "__main__":
    # Run integration tests when executed directly
    pytest.main([__file__, "-v", "-k", "Integration"])
