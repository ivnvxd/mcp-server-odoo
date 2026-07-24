"""Per-request authentication for the streamable-http transport.

In per-request mode the server holds no credentials of its own. Each HTTP
request carries its own Odoo API key in the ``Authorization: Bearer <key>``
header (``X-API-Key`` is also accepted). An ASGI middleware pulls that key into
a context variable and every Odoo access for that request runs as the key's
user, over a short-lived connection that is discarded when the request ends.

Design notes:

* **Stateless per request.** No connection pool or auth cache spans requests.
  Within a single request the connection/controller are built once and reused
  (stored in context variables), then closed in ``reset()``.
* **Fail-closed.** There is no shared, server-wide connection to fall back to.
  Any Odoo access outside a request context (no key in the contextvar) raises,
  so a missed call site fails loudly instead of leaking a shared identity.
* **Header, not tool Context.** The middleware is the one place that can both
  populate the contextvar for tools *and* resources and return a real HTTP 401
  before the request is dispatched to the MCP server.

This module is a no-op unless ``ODOO_MCP_PER_REQUEST_AUTH`` is enabled.
"""

import contextvars
import copy
import logging
from typing import Callable, Optional

from .access_control import AccessController
from .config import OdooConfig
from .odoo_connection import OdooConnection, OdooConnectionError

logger = logging.getLogger(__name__)

# The raw API key for the request currently being handled. Default None means
# "no request context" — any attempt to build a connection then fails closed.
current_api_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_api_key", default=None
)

# Per-request memoization so the connection/controller are built once per
# request rather than once per attribute access. Never spans requests.
_current_connection: contextvars.ContextVar[Optional[OdooConnection]] = contextvars.ContextVar(
    "_current_connection", default=None
)
_current_access_controller: contextvars.ContextVar[Optional[AccessController]] = (
    contextvars.ContextVar("_current_access_controller", default=None)
)

# Configured once at server startup.
_base_config: Optional[OdooConfig] = None
_performance_manager = None


class PerRequestAuthError(OdooConnectionError):
    """Raised when an Odoo access is attempted without a request API key."""


def configure(base_config: OdooConfig, performance_manager) -> None:
    """Wire the base config and shared performance manager used per request."""
    global _base_config, _performance_manager
    _base_config = base_config
    _performance_manager = performance_manager


def _require_key() -> str:
    key = current_api_key.get()
    if not key:
        raise PerRequestAuthError(
            "No API key in the request context. Per-request auth requires an "
            "'Authorization: Bearer <key>' header on every request."
        )
    return key


def _request_config(key: str) -> OdooConfig:
    """A shallow copy of the base config carrying this request's key."""
    cfg = copy.copy(_base_config)
    cfg.api_key = key
    cfg.username = None
    cfg.password = None
    cfg.per_request_auth = True
    return cfg


def get_connection() -> OdooConnection:
    """Return the connection for the current request, building it on first use.

    Fails closed if there is no API key in the request context.
    """
    if _base_config is None:
        raise PerRequestAuthError("Per-request auth is not configured")
    conn = _current_connection.get()
    if conn is not None:
        return conn
    key = _require_key()
    conn = OdooConnection(_request_config(key), performance_manager=_performance_manager)
    conn.connect()
    conn.authenticate()
    _current_connection.set(conn)
    return conn


def get_access_controller() -> AccessController:
    """Return the access controller for the current request (built on first use)."""
    if _base_config is None:
        raise PerRequestAuthError("Per-request auth is not configured")
    ac = _current_access_controller.get()
    if ac is not None:
        return ac
    conn = get_connection()
    ac = AccessController(
        _request_config(_require_key()),
        database=conn.database,
        auth_method=conn.auth_method,
    )
    _current_access_controller.set(ac)
    return ac


def reset() -> None:
    """Tear down and clear all per-request state. Called in the middleware's
    ``finally`` so a reused worker task never inherits a previous request's
    key or connection."""
    conn = _current_connection.get()
    if conn is not None:
        try:
            conn.disconnect(suppress_logging=True)
        except Exception:  # noqa: BLE001 - cleanup must never raise
            pass
    _current_connection.set(None)
    _current_access_controller.set(None)
    current_api_key.set(None)


class _PerRequestProxy:
    """Transparent stand-in for the connection / access controller.

    Registered handlers keep calling ``self.connection.<x>`` /
    ``self.access_controller.<x>``; every access is routed to the object built
    for the current request. Holds no state of its own.
    """

    __slots__ = ("_resolver",)

    def __init__(self, resolver: Callable[[], object]):
        object.__setattr__(self, "_resolver", resolver)

    def __getattr__(self, name):
        return getattr(self._resolver(), name)


def connection_proxy() -> "_PerRequestProxy":
    return _PerRequestProxy(get_connection)


def access_controller_proxy() -> "_PerRequestProxy":
    return _PerRequestProxy(get_access_controller)


def _extract_key(headers: list) -> Optional[str]:
    """Pull the API key from an ASGI raw headers list.

    Accepts ``Authorization: Bearer <key>`` (preferred) or ``X-API-Key: <key>``.
    """
    auth = None
    x_api_key = None
    for name, value in headers:
        lname = name.decode("latin-1").lower()
        if lname == "authorization":
            auth = value.decode("latin-1")
        elif lname == "x-api-key":
            x_api_key = value.decode("latin-1")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip()
        return None  # malformed Authorization header
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    return None


class PerRequestAuthMiddleware:
    """ASGI middleware that gates the MCP endpoint on a per-request API key.

    Missing/malformed credentials get a real HTTP 401 before the request ever
    reaches the MCP server. The contextvar is always cleared in ``finally`` so
    no key bleeds across requests on a reused task.
    """

    def __init__(self, app, guarded_prefix: str = "/mcp"):
        self.app = app
        self.guarded_prefix = guarded_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(self.guarded_prefix):
            # Unguarded (e.g. GET /health): pass through untouched.
            await self.app(scope, receive, send)
            return

        key = _extract_key(scope.get("headers", []))
        if not key:
            await self._unauthorized(send)
            return

        token = current_api_key.set(key)
        try:
            await self.app(scope, receive, send)
        finally:
            current_api_key.reset(token)
            reset()

    @staticmethod
    async def _unauthorized(send):
        body = b'{"error":"unauthorized","message":"Missing or malformed Authorization: Bearer <api-key> header"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="odoo-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
