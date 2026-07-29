"""Server wiring for per-request auth (ODOO_MCP_PER_REQUEST_AUTH).

Covers the branches the per-request mode switches inside OdooMCPServer:
proxy wiring at construction, the no-op startup/cleanup connection paths,
the health payload, the exposure log variant, and the stateless transport
configuration. No live Odoo involved.
"""

import asyncio
import logging

import pytest

from mcp_server_odoo import request_auth
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.request_auth import PerRequestAuthMiddleware, _PerRequestProxy
from mcp_server_odoo.server import OdooMCPServer


def _cfg(**kw):
    base = dict(
        url="http://odoo:8069",
        transport="streamable-http",
        database="cubert",
        per_request_auth=True,
    )
    base.update(kw)
    return OdooConfig(**base)


@pytest.fixture()
def server():
    prev_cfg = request_auth._base_config
    prev_pm = request_auth._performance_manager
    try:
        yield OdooMCPServer(_cfg())
    finally:
        request_auth._base_config = prev_cfg
        request_auth._performance_manager = prev_pm


def test_setup_wires_proxies_and_configures_module(server):
    assert isinstance(server.connection, _PerRequestProxy)
    assert isinstance(server.access_controller, _PerRequestProxy)
    # configure() ran with the server's config; handlers registered eagerly.
    assert request_auth._base_config is server.config
    assert server.performance_manager is not None


def test_ensure_connection_is_a_noop(server):
    # No server-wide connection is created at startup; the proxies stay.
    server._ensure_connection()
    assert isinstance(server.connection, _PerRequestProxy)


def test_cleanup_connection_is_a_noop(server):
    # Touching the proxy outside a request context would fail closed; the
    # early return means cleanup never touches it.
    server._cleanup_connection()


def test_health_status_reports_per_request_mode(server):
    health = server.get_health_status()
    assert health["status"] == "healthy"
    assert health["connection"]["mode"] == "per-request-auth"


def test_warn_if_exposed_notes_the_401_gate(server, caplog):
    with caplog.at_level(logging.INFO, logger="mcp_server_odoo.server"):
        server._warn_if_exposed("0.0.0.0")
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "per-request auth" in joined
    # The generic "no authentication" warning would be false here.
    assert "NO built-in" not in joined


def test_run_http_delegates_to_per_request_transport(server, monkeypatch):
    called = {}

    async def fake_run(host, port):
        called["hostport"] = (host, port)

    monkeypatch.setattr(server, "_run_http_per_request", fake_run)
    asyncio.run(server.run_http(host="127.0.0.1", port=8069))
    assert called["hostport"] == ("127.0.0.1", 8069)


def test_run_http_per_request_serves_stateless_guarded_app(server, monkeypatch):
    import uvicorn

    captured = {}

    class FakeConfig:
        def __init__(self, app, host, port, log_level):
            captured["app"] = app
            captured["hostport"] = (host, port)
            captured["log_level"] = log_level

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        async def serve(self):
            captured["served"] = True

    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    asyncio.run(server._run_http_per_request("127.0.0.1", 8069))

    # Stateless + json so the middleware's contextvar reaches the handler.
    assert server.app.settings.stateless_http is True
    assert server.app.settings.json_response is True
    assert isinstance(captured["app"], PerRequestAuthMiddleware)
    assert captured["hostport"] == ("127.0.0.1", 8069)
    assert captured["served"] is True
