"""Tests for OdooConnection write operations (create, write, unlink).

These tests mock only the XML-RPC proxy (the network boundary),
letting all OdooConnection logic (argument building, caching,
performance tracking, error handling) run for real.
"""

import xmlrpc.client
from unittest.mock import Mock

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


@pytest.fixture
def config():
    """Create a minimal config for testing."""
    return OdooConfig(
        url="http://localhost:8069",
        database="testdb",
        username="admin",
        password="admin",
    )


@pytest.fixture
def conn(config):
    """Create an OdooConnection with only the XML-RPC proxy mocked."""
    conn = OdooConnection(config)
    conn._connected = True
    conn._authenticated = True
    conn._uid = 2
    conn._database = "testdb"
    conn._auth_method = "password"
    # Mock only the network boundary
    conn._object_proxy = Mock()
    return conn


class TestCreateOperation:
    """Test OdooConnection.create() with real logic."""

    def test_create_builds_correct_execute_kw_call(self, conn):
        """create() should forward values as positional arg to execute_kw."""
        conn._object_proxy.execute_kw.return_value = 123

        result = conn.create("res.partner", {"name": "Test", "email": "t@example.com"})

        assert result == 123
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[0] == "testdb"
        assert args[1] == 2  # uid
        assert args[2] == "admin"  # password credential
        assert args[3] == "res.partner"
        assert args[4] == "create"
        assert args[5] == [{"name": "Test", "email": "t@example.com"}]
        assert args[6] == {}

    def test_create_invalidates_model_cache(self, conn):
        """create() should invalidate the record cache for the model."""
        conn._object_proxy.execute_kw.return_value = 42

        # Prime the cache
        conn._performance_manager.cache_record("res.partner", {"id": 1, "name": "Old"})

        conn.create("res.partner", {"name": "New"})

        # Cache should be invalidated (model-wide)
        cached = conn._performance_manager.get_cached_record("res.partner", 1)
        assert cached is None

    def test_create_propagates_xmlrpc_fault(self, conn):
        """create() should wrap XML-RPC faults as OdooConnectionError."""
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1, "Access denied on res.partner"
        )

        with pytest.raises(OdooConnectionError, match="Operation failed"):
            conn.create("res.partner", {"name": "Fail"})

    def test_create_propagates_connection_error(self, conn):
        """create() should propagate OdooConnectionError from execute_kw."""
        conn._object_proxy.execute_kw.side_effect = OdooConnectionError("Network error")

        with pytest.raises(OdooConnectionError, match="Network error"):
            conn.create("res.partner", {"name": "Test"})


class TestWriteOperation:
    """Test OdooConnection.write() with real logic."""

    def test_write_builds_correct_execute_kw_call(self, conn):
        """write() should forward ids and values as positional args."""
        conn._object_proxy.execute_kw.return_value = True

        result = conn.write("res.partner", [10, 20], {"email": "new@example.com"})

        assert result is True
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[3] == "res.partner"
        assert args[4] == "write"
        assert args[5] == [[10, 20], {"email": "new@example.com"}]

    def test_write_invalidates_cache_per_record(self, conn):
        """write() should invalidate cache for each updated record."""
        conn._object_proxy.execute_kw.return_value = True

        conn._performance_manager.cache_record("res.partner", {"id": 1, "name": "Old1"})
        conn._performance_manager.cache_record("res.partner", {"id": 2, "name": "Old2"})
        conn._performance_manager.cache_record("res.partner", {"id": 3, "name": "Untouched"})

        conn.write("res.partner", [1, 2], {"name": "Updated"})

        assert conn._performance_manager.get_cached_record("res.partner", 1) is None
        assert conn._performance_manager.get_cached_record("res.partner", 2) is None
        # Record 3 was not in the write list — it should remain cached
        assert conn._performance_manager.get_cached_record("res.partner", 3) is not None

    def test_write_propagates_xmlrpc_fault(self, conn):
        """write() should wrap XML-RPC faults as OdooConnectionError."""
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            2, "Constraint violation: unique email"
        )

        with pytest.raises(OdooConnectionError):
            conn.write("res.partner", [1], {"email": "dup@example.com"})


class TestUnlinkOperation:
    """Test OdooConnection.unlink() with real logic."""

    def test_unlink_builds_correct_execute_kw_call(self, conn):
        """unlink() should forward ids as positional arg."""
        conn._object_proxy.execute_kw.return_value = True

        result = conn.unlink("res.partner", [5, 6, 7])

        assert result is True
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[3] == "res.partner"
        assert args[4] == "unlink"
        assert args[5] == [[5, 6, 7]]

    def test_unlink_invalidates_cache_per_record(self, conn):
        """unlink() should invalidate cache for each deleted record."""
        conn._object_proxy.execute_kw.return_value = True

        conn._performance_manager.cache_record("res.partner", {"id": 5, "name": "Del"})
        conn._performance_manager.cache_record("res.partner", {"id": 6, "name": "Del2"})

        conn.unlink("res.partner", [5, 6])

        assert conn._performance_manager.get_cached_record("res.partner", 5) is None
        assert conn._performance_manager.get_cached_record("res.partner", 6) is None

    def test_unlink_propagates_xmlrpc_fault(self, conn):
        """unlink() should wrap XML-RPC faults as OdooConnectionError."""
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1, "Cannot delete record: linked to other records"
        )

        with pytest.raises(OdooConnectionError):
            conn.unlink("res.partner", [1])


class TestWriteOperationsGuards:
    """Test authentication and connection guards for write operations."""

    def test_create_not_authenticated_raises(self, config):
        """create() should raise if not authenticated."""
        conn = OdooConnection(config)
        conn._connected = True

        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            conn.create("res.partner", {"name": "Test"})

    def test_write_not_authenticated_raises(self, config):
        """write() should raise if not authenticated."""
        conn = OdooConnection(config)
        conn._connected = True

        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            conn.write("res.partner", [1], {"name": "Test"})

    def test_unlink_not_authenticated_raises(self, config):
        """unlink() should raise if not authenticated."""
        conn = OdooConnection(config)
        conn._connected = True

        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            conn.unlink("res.partner", [1])

    def test_create_not_connected_raises(self, config):
        """create() should raise if not connected."""
        conn = OdooConnection(config)
        conn._authenticated = True

        with pytest.raises(OdooConnectionError, match="Not connected"):
            conn.create("res.partner", {"name": "Test"})
