"""Unit tests for per-request authentication (ODOO_MCP_PER_REQUEST_AUTH).

These run without a live Odoo: they cover the security-critical logic that a
regression could silently break — header parsing, the 401 gate, contextvar
reset (no key bleed across requests), fail-closed access, and the config
contract. The live two-key isolation / interleaving / revocation behaviour is
exercised separately against a real Odoo + cubert_mcp addon.
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

from mcp_server_odoo import request_auth
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.request_auth import (
    PerRequestAuthError,
    PerRequestAuthMiddleware,
    _extract_key,
    current_api_key,
)


# --------------------------------------------------------------------------- #
# Header extraction
# --------------------------------------------------------------------------- #
def _headers(mapping=None):
    """Build an ASGI raw-headers list from a {name: value} dict."""
    mapping = mapping or {}
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in mapping.items()]


def test_extract_bearer_key():
    assert _extract_key(_headers({"Authorization": "Bearer abc123"})) == "abc123"


def test_extract_bearer_case_insensitive_scheme():
    assert _extract_key(_headers({"Authorization": "bearer abc123"})) == "abc123"


def test_extract_x_api_key_fallback():
    assert _extract_key(_headers({"X-API-Key": "k"})) == "k"


def test_extract_missing_returns_none():
    assert _extract_key(_headers()) is None


def test_extract_malformed_authorization_returns_none():
    assert _extract_key(_headers({"Authorization": "abc123"})) is None  # no scheme
    assert _extract_key(_headers({"Authorization": "Bearer "})) is None  # empty token
    assert _extract_key(_headers({"Authorization": "Basic abc123"})) is None  # wrong scheme


# --------------------------------------------------------------------------- #
# Config contract
# --------------------------------------------------------------------------- #
def _cfg(**kw):
    base = dict(
        url="http://odoo:8069",
        transport="streamable-http",
        database="cubert",
        per_request_auth=True,
    )
    base.update(kw)
    return OdooConfig(**base)


def test_per_request_uses_standard_endpoints():
    ep = _cfg().get_endpoint_paths()
    assert ep["object"] == "/xmlrpc/2/object"
    assert ep["common"] == "/xmlrpc/2/common"


def test_per_request_needs_no_startup_key():
    # No api_key / username / password required at startup.
    cfg = _cfg()
    assert cfg.api_key is None


def test_per_request_requires_database():
    with pytest.raises(ValueError, match="ODOO_DB"):
        _cfg(database=None)


def test_per_request_requires_http_transport():
    with pytest.raises(ValueError, match="streamable-http"):
        _cfg(transport="stdio")


def test_per_request_rejects_yolo():
    with pytest.raises(ValueError, match="YOLO"):
        _cfg(yolo_mode="true")


def test_standard_mode_still_requires_auth():
    # Regression: without per-request auth, a key or credentials are required.
    with pytest.raises(ValueError, match="Authentication required"):
        OdooConfig(url="http://odoo:8069")


# --------------------------------------------------------------------------- #
# Fail-closed access
# --------------------------------------------------------------------------- #
def test_get_connection_without_key_fails_closed():
    request_auth.configure(_cfg(), performance_manager=object())
    token = current_api_key.set(None)
    try:
        with pytest.raises(PerRequestAuthError):
            request_auth.get_connection()
    finally:
        current_api_key.reset(token)


# --------------------------------------------------------------------------- #
# ASGI middleware: 401 gate + contextvar lifecycle
# --------------------------------------------------------------------------- #
class _Recorder:
    """Fake downstream ASGI app that records the key visible mid-request."""

    def __init__(self):
        self.called = False
        self.seen_key = "UNSET"

    async def __call__(self, scope, receive, send):
        self.called = True
        self.seen_key = current_api_key.get()


async def _send_collect(store):
    async def send(msg):
        store.append(msg)

    return send


def _scope(path="/mcp", headers=None):
    return {"type": "http", "path": path, "headers": headers or []}


def _run(coro):
    return asyncio.run(coro)


def test_middleware_rejects_missing_key_with_401():
    downstream = _Recorder()
    mw = PerRequestAuthMiddleware(downstream)
    sent = []

    async def go():
        send = await _send_collect(sent)
        await mw(_scope(headers=_headers()), None, send)

    _run(go())
    assert downstream.called is False  # never dispatched
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


def test_middleware_sets_key_for_request_then_resets():
    downstream = _Recorder()
    mw = PerRequestAuthMiddleware(downstream)
    sent = []

    async def go():
        send = await _send_collect(sent)
        await mw(_scope(headers=_headers({"Authorization": "Bearer thekey"})), None, send)

    _run(go())
    assert downstream.called is True
    assert downstream.seen_key == "thekey"  # visible during dispatch
    # Reset in finally: no bleed to the next request on a reused task.
    assert current_api_key.get() is None


def test_middleware_passes_through_unguarded_path():
    downstream = _Recorder()
    mw = PerRequestAuthMiddleware(downstream, guarded_prefix="/mcp")
    sent = []

    async def go():
        send = await _send_collect(sent)
        await mw(_scope(path="/health", headers=_headers()), None, send)

    _run(go())
    assert downstream.called is True  # /health not gated
    assert sent == []  # no 401 emitted


# --------------------------------------------------------------------------- #
# Per-request config, connection and controller lifecycle
# --------------------------------------------------------------------------- #
@contextmanager
def _configured(cfg=None, key="req-key"):
    """Configure the module for one test; restore pristine state after."""
    prev_cfg = request_auth._base_config
    prev_pm = request_auth._performance_manager
    request_auth.configure(cfg or _cfg(), performance_manager=Mock())
    token = current_api_key.set(key) if key else None
    try:
        yield
    finally:
        request_auth.reset()
        if token is not None:
            current_api_key.reset(token)
        request_auth._base_config = prev_cfg
        request_auth._performance_manager = prev_pm


def test_request_config_carries_key_and_drops_credentials():
    base = _cfg()
    with _configured(base, key=None):
        cfg = request_auth._request_config("k-abc")
    assert cfg is not base  # a copy: the base config is never mutated
    assert cfg.api_key == "k-abc"
    assert cfg.username is None
    assert cfg.password is None
    assert cfg.per_request_auth is True
    assert base.api_key is None


def test_request_config_unconfigured_fails_closed():
    prev = request_auth._base_config
    request_auth._base_config = None
    try:
        with pytest.raises(PerRequestAuthError):
            request_auth._request_config("k")
        with pytest.raises(PerRequestAuthError):
            request_auth.get_connection()
        with pytest.raises(PerRequestAuthError):
            request_auth.get_access_controller()
    finally:
        request_auth._base_config = prev


def test_get_connection_builds_once_per_request():
    with _configured(), patch("mcp_server_odoo.request_auth.OdooConnection") as conn_cls:
        conn = conn_cls.return_value
        first = request_auth.get_connection()
        second = request_auth.get_connection()
        assert first is conn
        assert second is conn  # memoized within the request
        conn_cls.assert_called_once()
        conn.connect.assert_called_once()
        conn.authenticate.assert_called_once()


def test_get_access_controller_builds_from_request_connection():
    with (
        _configured(),
        patch("mcp_server_odoo.request_auth.OdooConnection") as conn_cls,
        patch("mcp_server_odoo.request_auth.AccessController") as ac_cls,
    ):
        conn_cls.return_value.database = "cubert"
        conn_cls.return_value.auth_method = "api_key"
        first = request_auth.get_access_controller()
        second = request_auth.get_access_controller()
        assert first is ac_cls.return_value
        assert second is first  # memoized within the request
        ac_cls.assert_called_once()
        assert ac_cls.call_args.kwargs["database"] == "cubert"
        assert ac_cls.call_args.kwargs["auth_method"] == "api_key"


def test_reset_disconnects_and_clears_request_state():
    with _configured(), patch("mcp_server_odoo.request_auth.OdooConnection") as conn_cls:
        conn = conn_cls.return_value
        request_auth.get_connection()
        request_auth.reset()
        conn.disconnect.assert_called_once_with(suppress_logging=True)
        # State cleared: the next access has no key and fails closed.
        with pytest.raises(PerRequestAuthError):
            request_auth.get_connection()


def test_reset_swallows_disconnect_errors():
    with _configured(), patch("mcp_server_odoo.request_auth.OdooConnection") as conn_cls:
        conn_cls.return_value.disconnect.side_effect = RuntimeError("boom")
        request_auth.get_connection()
        request_auth.reset()  # cleanup must never raise


def test_proxies_route_to_the_request_objects():
    with (
        _configured(),
        patch("mcp_server_odoo.request_auth.OdooConnection") as conn_cls,
        patch("mcp_server_odoo.request_auth.AccessController") as ac_cls,
    ):
        conn_cls.return_value.database = "cubert"
        conn_cls.return_value.auth_method = "api_key"
        conn_cls.return_value.is_authenticated = True
        ac_cls.return_value.enabled_models = ["project.task"]
        assert request_auth.connection_proxy().is_authenticated is True
        assert request_auth.access_controller_proxy().enabled_models == ["project.task"]


def test_middleware_ignores_non_http_scopes():
    downstream = _Recorder()
    mw = PerRequestAuthMiddleware(downstream)

    async def go():
        await mw({"type": "lifespan"}, None, None)

    _run(go())
    assert downstream.called is True  # passed through untouched
