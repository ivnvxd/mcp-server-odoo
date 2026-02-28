"""Unit tests for OdooConnection CRUD methods.

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
def connected_connection(config):
    """Create an OdooConnection that appears connected and authenticated."""
    conn = OdooConnection(config)
    # Set internal state to simulate successful connect + authenticate
    conn._connected = True
    conn._authenticated = True
    conn._uid = 2
    conn._database = "testdb"
    conn._auth_method = "password"

    # Mock only the XML-RPC object proxy (the network boundary)
    conn._object_proxy = Mock()
    return conn


class TestSearch:
    """Test OdooConnection.search() method."""

    def test_search_builds_correct_execute_kw_call(self, connected_connection):
        """search() should call execute_kw with 'search' method and domain as first arg."""
        conn = connected_connection
        domain = [["is_company", "=", True]]
        conn._object_proxy.execute_kw.return_value = [1, 2, 3]

        result = conn.search("res.partner", domain)

        assert result == [1, 2, 3]
        conn._object_proxy.execute_kw.assert_called_once()
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[0] == "testdb"
        assert args[1] == 2  # uid
        assert args[2] == "admin"  # password
        assert args[3] == "res.partner"
        assert args[4] == "search"
        assert args[5] == [domain]  # positional args
        assert args[6] == {}  # kwargs (no extra params)

    def test_search_passes_kwargs(self, connected_connection):
        """search() should forward limit, offset, order to execute_kw kwargs."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = [10]

        result = conn.search("res.partner", [], limit=5, offset=10, order="name asc")

        assert result == [10]
        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["limit"] == 5
        assert kwargs["offset"] == 10
        assert kwargs["order"] == "name asc"

    def test_search_not_authenticated_raises(self, config):
        """search() should raise if not authenticated."""
        conn = OdooConnection(config)
        conn._connected = True

        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            conn.search("res.partner", [])


class TestRead:
    """Test OdooConnection.read() method."""

    def test_read_without_fields(self, connected_connection):
        """read() without fields should not pass 'fields' kwarg."""
        conn = connected_connection
        records = [{"id": 1, "name": "Test"}]
        conn._object_proxy.execute_kw.return_value = records

        result = conn.read("res.partner", [1])

        assert result == records
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[4] == "read"
        assert args[5] == [[1]]  # ids as positional arg
        assert args[6] == {}  # no fields kwarg

    def test_read_with_fields(self, connected_connection):
        """read() with fields should pass them as kwarg."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = [{"id": 1, "name": "Test"}]

        result = conn.read("res.partner", [1, 2], ["name", "email"])

        assert result == [{"id": 1, "name": "Test"}]
        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["fields"] == ["name", "email"]

    def test_read_empty_fields_not_passed(self, connected_connection):
        """read() with empty fields list should not pass 'fields' kwarg."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = []

        conn.read("res.partner", [1], [])

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert "fields" not in kwargs


class TestCreate:
    """Test OdooConnection.create() method."""

    def test_create_returns_record_id(self, connected_connection):
        """create() should return the ID from execute_kw."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = 42

        result = conn.create("res.partner", {"name": "New Partner"})

        assert result == 42
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[4] == "create"
        assert args[5] == [{"name": "New Partner"}]

    def test_create_invalidates_cache(self, connected_connection):
        """create() should invalidate the record cache for the model."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = 10

        # Prime the cache
        conn._performance_manager.cache_record("res.partner", {"id": 1, "name": "Old"})

        conn.create("res.partner", {"name": "New"})

        # Cache should be invalidated (model-wide invalidation)
        cached = conn._performance_manager.get_cached_record("res.partner", 1)
        assert cached is None

    def test_create_propagates_xmlrpc_fault(self, connected_connection):
        """create() should wrap XML-RPC faults as OdooConnectionError."""
        conn = connected_connection
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1, "Access denied on res.partner"
        )

        with pytest.raises(OdooConnectionError, match="Operation failed"):
            conn.create("res.partner", {"name": "Fail"})


class TestWrite:
    """Test OdooConnection.write() method."""

    def test_write_returns_true(self, connected_connection):
        """write() should return True on success."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = True

        result = conn.write("res.partner", [1], {"name": "Updated"})

        assert result is True
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[4] == "write"
        assert args[5] == [[1], {"name": "Updated"}]

    def test_write_invalidates_cache_per_record(self, connected_connection):
        """write() should invalidate cache for each updated record."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = True

        conn._performance_manager.cache_record("res.partner", {"id": 1, "name": "Old1"})
        conn._performance_manager.cache_record("res.partner", {"id": 2, "name": "Old2"})

        conn.write("res.partner", [1, 2], {"name": "Updated"})

        assert conn._performance_manager.get_cached_record("res.partner", 1) is None
        assert conn._performance_manager.get_cached_record("res.partner", 2) is None


class TestUnlink:
    """Test OdooConnection.unlink() method."""

    def test_unlink_returns_true(self, connected_connection):
        """unlink() should return True on success."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = True

        result = conn.unlink("res.partner", [1, 2])

        assert result is True
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[4] == "unlink"
        assert args[5] == [[1, 2]]

    def test_unlink_invalidates_cache(self, connected_connection):
        """unlink() should invalidate cache for deleted records."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = True

        conn._performance_manager.cache_record("res.partner", {"id": 5})
        conn.unlink("res.partner", [5])

        assert conn._performance_manager.get_cached_record("res.partner", 5) is None


class TestFieldsGet:
    """Test OdooConnection.fields_get() method."""

    def test_fields_get_caches_result(self, connected_connection):
        """fields_get() without attributes should cache and reuse results."""
        conn = connected_connection
        fields_data = {"name": {"type": "char"}, "email": {"type": "char"}}
        conn._object_proxy.execute_kw.return_value = fields_data

        # First call hits the proxy
        result1 = conn.fields_get("res.partner")
        assert result1 == fields_data
        assert conn._object_proxy.execute_kw.call_count == 1

        # Second call should use cache
        result2 = conn.fields_get("res.partner")
        assert result2 == fields_data
        assert conn._object_proxy.execute_kw.call_count == 1  # no extra call

    def test_fields_get_with_attributes_bypasses_cache(self, connected_connection):
        """fields_get() with attributes should always hit the server."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = {"name": {"string": "Name"}}

        conn.fields_get("res.partner", attributes=["string"])
        conn.fields_get("res.partner", attributes=["string"])

        assert conn._object_proxy.execute_kw.call_count == 2


class TestSearchCount:
    """Test OdooConnection.search_count() method."""

    def test_search_count_returns_integer(self, connected_connection):
        """search_count() should return the count from execute_kw."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = 42

        result = conn.search_count("res.partner", [["active", "=", True]])

        assert result == 42
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[4] == "search_count"
        assert args[5] == [[["active", "=", True]]]


class TestSearchRead:
    """Test OdooConnection.search_read() method."""

    def test_search_read_builds_correct_execute_kw_call(self, connected_connection):
        """search_read() should call execute_kw with 'search_read' method and domain."""
        conn = connected_connection
        domain = [["is_company", "=", True]]
        expected = [{"id": 1, "name": "Test"}]
        conn._object_proxy.execute_kw.return_value = expected

        result = conn.search_read("res.partner", domain)

        assert result == expected
        conn._object_proxy.execute_kw.assert_called_once()
        args = conn._object_proxy.execute_kw.call_args[0]
        assert args[0] == "testdb"
        assert args[1] == 2  # uid
        assert args[2] == "admin"  # password
        assert args[3] == "res.partner"
        assert args[4] == "search_read"
        assert args[5] == [domain]
        assert args[6] == {}

    def test_search_read_with_fields(self, connected_connection):
        """search_read() with fields should pass them as kwarg."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = [{"id": 1, "name": "Test"}]

        conn.search_read("res.partner", [], fields=["name", "email"])

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["fields"] == ["name", "email"]

    def test_search_read_without_fields(self, connected_connection):
        """search_read() without fields should not pass 'fields' kwarg."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = []

        conn.search_read("res.partner", [])

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert "fields" not in kwargs

    def test_search_read_with_limit_and_offset(self, connected_connection):
        """search_read() should forward limit and offset to execute_kw kwargs."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = []

        conn.search_read("res.partner", [], limit=10, offset=20)

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 20

    def test_search_read_propagates_xmlrpc_fault(self, connected_connection):
        """search_read() should wrap XML-RPC faults as OdooConnectionError."""
        conn = connected_connection
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1, "Access denied on res.partner"
        )

        with pytest.raises(OdooConnectionError, match="Operation failed"):
            conn.search_read("res.partner", [])


class TestExecuteKwErrorHandling:
    """Test execute_kw error handling."""

    def test_xmlrpc_fault_sanitized(self, connected_connection):
        """XML-RPC faults should have error messages sanitized."""
        conn = connected_connection
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1,
            "Traceback (most recent call last):\n"
            "  File /opt/odoo/addons/base/models/res_partner.py:123\n"
            "ValueError: Invalid field 'bad_field' on model 'res.partner'",
        )

        with pytest.raises(OdooConnectionError) as exc_info:
            conn.search("res.partner", [])

        error_msg = str(exc_info.value)
        # Should not contain internal file paths
        assert "/opt/odoo" not in error_msg
        assert "Traceback" not in error_msg

    def test_not_connected_raises(self, config):
        """execute_kw should raise if not connected."""
        conn = OdooConnection(config)
        conn._authenticated = True

        with pytest.raises(OdooConnectionError, match="Not connected"):
            conn.execute_kw("res.partner", "search", [[]], {})

    def test_locale_injection(self, connected_connection):
        """execute_kw should inject locale into context."""
        conn = connected_connection
        conn.config.locale = "fr_FR"
        conn._object_proxy.execute_kw.return_value = []

        conn.search("res.partner", [])

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["context"]["lang"] == "fr_FR"

    def test_locale_does_not_override_explicit_lang(self, connected_connection):
        """execute_kw should not override caller-provided lang."""
        conn = connected_connection
        conn.config.locale = "fr_FR"
        conn._object_proxy.execute_kw.return_value = []

        conn.execute_kw("res.partner", "search", [[]], {"context": {"lang": "de_DE"}})

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["context"]["lang"] == "de_DE"
