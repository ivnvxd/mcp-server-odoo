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

    def test_read_empty_fields_passed_through(self, connected_connection):
        """read() passes fields=[] through verbatim; only None omits the kwarg.

        Mapping [] to smart defaults happens in the tool layer — the
        connection must not silently turn [] into "all fields".
        """
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = []

        conn.read("res.partner", [1], [])
        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs.get("fields") == []

        conn.read("res.partner", [1], None)
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

    def test_allowed_companies_injection(self, connected_connection):
        """execute_kw should inject allowed_company_ids into context when configured."""
        conn = connected_connection
        conn.config.allowed_companies = [1, 3]
        conn._object_proxy.execute_kw.return_value = []

        conn.search("res.partner", [])

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["context"]["allowed_company_ids"] == [1, 3]

    def test_allowed_companies_overrides_caller_context(self, connected_connection):
        """Company scoping is a guardrail: caller-provided values must not widen it."""
        conn = connected_connection
        conn.config.allowed_companies = [1]
        conn._object_proxy.execute_kw.return_value = []

        conn.execute_kw(
            "res.partner", "search", [[]], {"context": {"allowed_company_ids": [1, 2, 3]}}
        )

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert kwargs["context"]["allowed_company_ids"] == [1]

    def test_allowed_companies_context_is_copied(self, connected_connection):
        """Mutating the injected context must not corrupt the shared config."""
        conn = connected_connection
        conn.config.allowed_companies = [1, 3]
        conn._object_proxy.execute_kw.return_value = []

        conn.search("res.partner", [])

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        kwargs["context"]["allowed_company_ids"].append(99)
        assert conn.config.allowed_companies == [1, 3]

    def test_allowed_companies_not_injected_when_unset(self, connected_connection):
        """Without configuration, context must not carry allowed_company_ids."""
        conn = connected_connection
        conn.config.allowed_companies = None
        conn._object_proxy.execute_kw.return_value = []

        conn.search("res.partner", [])

        kwargs = conn._object_proxy.execute_kw.call_args[0][6]
        assert "allowed_company_ids" not in kwargs.get("context", {})

    def test_marshal_none_fault_returns_none(self, connected_connection):
        """Odoo's "cannot marshal None" fault is translated to a None return.

        Methods like ``toggle_active`` and ``action_archive`` mutate state then
        return ``None``. Odoo's XML-RPC marshaller is configured with
        ``allow_none=False`` and raises a Fault on the response — even though
        the call already succeeded server-side. ``execute_kw`` recognizes this
        and returns ``None`` so callers see normal Python semantics.
        """
        conn = connected_connection
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1,
            "Traceback (most recent call last):\n"
            "  File ..., in dump_nil\n"
            "    raise TypeError('cannot marshal None unless allow_none is enabled')\n"
            "TypeError: cannot marshal None unless allow_none is enabled",
        )

        result = conn.execute_kw("res.partner", "toggle_active", [[1]], {})
        assert result is None

    def test_other_xmlrpc_fault_still_raises(self, connected_connection):
        """A regular Odoo Fault is still wrapped as OdooConnectionError."""
        conn = connected_connection
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1,
            "ValueError: Invalid field 'bad_field' on model 'res.partner'",
        )

        with pytest.raises(OdooConnectionError, match="Operation failed"):
            conn.execute_kw("res.partner", "do_thing", [[1]], {})

    def test_marshal_none_substring_alone_does_not_swallow(self, connected_connection):
        """Only the full ``cannot marshal None unless allow_none`` signature is treated
        as a void return. A fault whose message merely contains the looser substring
        "cannot marshal None" must still raise — not be silently turned into None.
        """
        conn = connected_connection
        conn._object_proxy.execute_kw.side_effect = xmlrpc.client.Fault(
            1,
            "ValidationError: User said 'cannot marshal None' in a message — distinct fault",
        )

        with pytest.raises(OdooConnectionError, match="Operation failed"):
            conn.execute_kw("res.partner", "do_thing", [[1]], {})


class TestTimeoutRetryGating:
    """execute_kw must mark the transport's timeout-retry flag per call:
    only read-only methods may be re-sent after a keepalive socket timeout
    (issue #68) — a re-sent write could be double-executed."""

    def test_read_method_marks_transport_retry_safe(self, connected_connection):
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = []

        conn.execute_kw("res.partner", "search_read", [[]], {})

        assert conn._object_proxy("transport").timeout_retry_safe is True

    def test_write_method_marks_transport_retry_unsafe(self, connected_connection):
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = 1

        conn.execute_kw("res.partner", "create", [{"name": "X"}], {})

        assert conn._object_proxy("transport").timeout_retry_safe is False

    def test_arbitrary_method_marks_transport_retry_unsafe(self, connected_connection):
        """call_model_method targets (workflow methods) are writes by default."""
        conn = connected_connection
        conn._object_proxy.execute_kw.return_value = True

        conn.execute_kw("sale.order", "action_confirm", [[1]], {})

        assert conn._object_proxy("transport").timeout_retry_safe is False
