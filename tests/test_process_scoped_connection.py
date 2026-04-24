"""Regression tests for process-scoped Odoo connection lifecycle.

These tests guard against the production bug where the Odoo connection
was torn down at the end of every MCP (streamable-http) session,
causing all subsequent sessions to fail with "Not authenticated with
Odoo" because ``_ensure_connection`` returned in ~10ms without actually
re-authenticating.

Fix contract verified here:

1. The Odoo connection is established **once** across multiple
   lifespan enter/exit cycles.
2. Tool/resource registration happens **once** per ``OdooMCPServer``
   instance, regardless of how many times the lifespan is re-entered.
3. ``_ensure_connection`` is idempotent when the connection is alive
   and authenticated (no re-auth).
4. ``_ensure_connection`` transparently re-authenticates when the
   cached connection is dead / stale.
5. The lifespan does NOT call ``_cleanup_connection`` on exit.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.server import OdooMCPServer


def _mk_config() -> OdooConfig:
    return OdooConfig(
        url="http://localhost:8069",
        api_key="k" * 40,
        database="testdb",
        log_level="WARNING",
    )


@pytest.fixture
def fake_connection_factory():
    """Build a mock OdooConnection class that tracks connect/auth calls."""

    def _factory(*, healthy: bool = True):
        state = {"connect_calls": 0, "auth_calls": 0, "disconnect_calls": 0, "healthy": healthy}

        def _cls(config, performance_manager=None):
            inst = MagicMock(name="OdooConnection")
            inst.config = config
            inst.database = config.database or "testdb"
            inst.is_authenticated = False

            def _connect():
                state["connect_calls"] += 1

            def _authenticate(database=None):
                state["auth_calls"] += 1
                inst.is_authenticated = True

            def _disconnect(suppress_logging: bool = False):
                state["disconnect_calls"] += 1
                inst.is_authenticated = False

            def _check_health():
                if state["healthy"] and inst.is_authenticated:
                    return True, "ok"
                return False, "dead"

            inst.connect.side_effect = _connect
            inst.authenticate.side_effect = _authenticate
            inst.disconnect.side_effect = _disconnect
            inst.check_health.side_effect = _check_health
            return inst

        return _cls, state

    return _factory


@pytest.fixture
def built_server(fake_connection_factory):
    """Construct an OdooMCPServer with all heavy deps mocked out."""
    conn_cls, state = fake_connection_factory(healthy=True)

    tools_register = MagicMock(name="register_tools", return_value=MagicMock())
    resources_register = MagicMock(name="register_resources", return_value=MagicMock())

    with (
        patch("mcp_server_odoo.server.OdooConnection", conn_cls),
        patch("mcp_server_odoo.server.PerformanceManager", MagicMock()),
        patch("mcp_server_odoo.server.AccessController", MagicMock()),
        patch("mcp_server_odoo.server.register_tools", tools_register),
        patch("mcp_server_odoo.server.register_resources", resources_register),
    ):
        srv = OdooMCPServer(config=_mk_config())
        yield srv, state, tools_register, resources_register


@pytest.mark.asyncio
async def test_connection_established_once_across_lifespan_cycles(built_server):
    """Re-entering the lifespan must NOT re-authenticate or reconnect."""
    srv, state, _tools, _resources = built_server

    async with srv._odoo_lifespan(srv.app):
        assert srv.connection is not None
        assert srv.connection.is_authenticated is True
    # After first session close, connection must still be live.
    assert srv.connection is not None, "Connection must survive session teardown (process-scoped)"
    assert srv.connection.is_authenticated is True

    async with srv._odoo_lifespan(srv.app):
        pass
    async with srv._odoo_lifespan(srv.app):
        pass

    assert state["connect_calls"] == 1, "connect() must be called exactly once"
    assert state["auth_calls"] == 1, "authenticate() must be called exactly once"
    assert state["disconnect_calls"] == 0, "disconnect() must NOT be called from lifespan exit"


@pytest.mark.asyncio
async def test_tools_and_resources_registered_once(built_server):
    srv, _state, tools_register, resources_register = built_server

    for _ in range(3):
        async with srv._odoo_lifespan(srv.app):
            pass

    assert tools_register.call_count == 1, "Tools must be registered exactly once"
    assert resources_register.call_count == 1, "Resources must be registered exactly once"


@pytest.mark.asyncio
async def test_ensure_connection_is_idempotent_when_alive(built_server):
    srv, state, _t, _r = built_server

    async with srv._odoo_lifespan(srv.app):
        pass
    assert state["auth_calls"] == 1

    # Direct idempotency check.
    srv._ensure_connection()
    srv._ensure_connection()
    srv._ensure_connection()
    assert state["auth_calls"] == 1


def test_ensure_connection_reauths_when_connection_dead(fake_connection_factory):
    """If the cached connection fails its liveness probe, re-authenticate."""
    conn_cls, state = fake_connection_factory(healthy=True)

    with (
        patch("mcp_server_odoo.server.OdooConnection", conn_cls),
        patch("mcp_server_odoo.server.PerformanceManager", MagicMock()),
        patch("mcp_server_odoo.server.AccessController", MagicMock()),
        patch("mcp_server_odoo.server.register_tools", MagicMock()),
        patch("mcp_server_odoo.server.register_resources", MagicMock()),
    ):
        srv = OdooMCPServer(config=_mk_config())
        srv._ensure_connection()
        assert state["connect_calls"] == 1
        assert state["auth_calls"] == 1

        # Simulate the server becoming unreachable / session dying.
        state["healthy"] = False
        first_conn = srv.connection

        srv._ensure_connection()
        # A new connection object should have been built and authenticated.
        assert state["connect_calls"] == 2
        assert state["auth_calls"] == 2
        assert srv.connection is not first_conn


def test_lifespan_does_not_cleanup_connection(built_server):
    """Explicit invariant: _cleanup_connection is NOT in the lifespan path."""
    import inspect

    src = inspect.getsource(OdooMCPServer._odoo_lifespan)
    # Must not actually *call* _cleanup_connection from the lifespan
    # (comments mentioning it for clarity are fine).
    code_lines = [line.split("#", 1)[0] for line in src.splitlines()]
    code = "\n".join(code_lines)
    assert "_cleanup_connection(" not in code, (
        "Lifespan must not call _cleanup_connection — the connection is "
        "process-scoped and must survive per-session teardown."
    )
