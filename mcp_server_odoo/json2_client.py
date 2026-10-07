"""Client for Odoo's JSON-2 API (``POST /json/2/<model>/<method>``, Odoo 19 and later).

JSON-2 takes a bearer API key, the database in ``X-Odoo-Database``, and a JSON
object of named arguments (``ids``, ``context`` and the method's parameters).
An error comes back as ``{name, message, arguments, context, debug}`` with an
HTTP status. The client maps it onto the XML-RPC fault codes and reuses
``_raise_for_fault``, so callers see the same exceptions and messages on both
transports. ``debug`` holds the server traceback and never reaches an
exception: only ``message`` does, through the sanitizer.
"""

import errno
import http.client
import json
import logging
import socket
import ssl
import threading
import xmlrpc.client
from typing import Any, Dict, NoReturn, Optional, Tuple
from urllib.parse import quote, urlparse

from .error_sanitizer import ErrorSanitizer
from .odoo_connection import (
    _UNAVAILABLE_HTTP_STATUSES,
    OdooConnectionError,
    OdooUnreachableError,
    _raise_for_fault,
)

logger = logging.getLogger(__name__)

# Errors of a reused keepalive socket that the server closed while idle. The
# request never reached Odoo, so one retry on a new connection is safe for any
# method (the stdlib XML-RPC transport does the same).
_STALE_CONNECTION_ERRORS = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)
# macOS can report a send on a socket the peer closed as EPROTOTYPE
_STALE_CONNECTION_ERRNOS = frozenset(
    {errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.EPROTOTYPE}
)

# XML-RPC fault codes of Odoo's /xmlrpc/2 endpoint (see _raise_for_fault)
_FAULT_ACCESS_DENIED = 3
_FAULT_ACCESS_ERROR = 4
_FAULT_WARNING = 2
_FAULT_APPLICATION = 1


class Json2Client:
    """One persistent HTTP(S) connection to ``/json/2``, used under a lock."""

    def __init__(self, url: str, api_key: str, database: Optional[str], timeout: float):
        parsed = urlparse(url)
        self._https = parsed.scheme == "https"
        self._host = parsed.hostname or ""
        self._port = parsed.port or (443 if self._https else 80)
        self._base_path = parsed.path.rstrip("/")
        self._api_key = api_key
        self.database = database
        self.timeout = timeout
        self._ssl_context = ssl.create_default_context() if self._https else None
        self._connection: Optional[http.client.HTTPConnection] = None
        self._lock = threading.Lock()

    def call(
        self, model: str, method: str, body: Dict[str, Any], *, retry_safe: bool = False
    ) -> Any:
        """Call ``model.method`` with the named arguments in ``body`` (blocking).

        ``retry_safe`` allows one retry after a timeout on a reused
        connection; only read-only methods may set it, because a timed-out
        write can still be committing on the server.

        Raises:
            OdooValidationFault: a business error (access, validation, missing record)
            OdooUnreachableError: Odoo did not answer
            OdooConnectionError: any other failure
        """
        path = f"{self._base_path}/json/2/{quote(model, safe='')}/{quote(method, safe='')}"
        payload = json.dumps(body).encode()
        headers = {
            "Authorization": f"bearer {self._api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        if self.database:
            headers["X-Odoo-Database"] = self.database
        status, response_headers, data = self._send(path, payload, headers, retry_safe)
        if status == 200:
            try:
                return json.loads(data)
            except ValueError as e:
                raise OdooConnectionError(
                    f"Operation failed: {model}.{method} answered with invalid JSON"
                ) from e
        self._raise_for_status(status, response_headers, data)

    def close(self) -> None:
        """Close the connection; the next call opens a new one."""
        with self._lock:
            self._close()

    def _send(
        self, path: str, payload: bytes, headers: Dict[str, str], retry_safe: bool
    ) -> Tuple[int, http.client.HTTPMessage, bytes]:
        """POST under the lock, with one retry on a stale keepalive connection."""
        with self._lock:
            for attempt in (0, 1):
                reused = self._connection is not None and self._connection.sock is not None
                connection = self._open()
                try:
                    connection.request("POST", path, body=payload, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    if response.will_close:
                        self._close()
                    return response.status, response.headers, data
                except TimeoutError:
                    self._close()
                    if attempt or not reused or not retry_safe:
                        raise OdooConnectionError(
                            f"Operation timeout after {self.timeout} seconds"
                        ) from None
                except (OSError, http.client.HTTPException) as e:
                    # http.client leaves a failed connection unusable
                    self._close()
                    stale = isinstance(e, _STALE_CONNECTION_ERRORS) or (
                        isinstance(e, OSError) and e.errno in _STALE_CONNECTION_ERRNOS
                    )
                    if attempt or not reused or not stale:
                        raise OdooUnreachableError(self._network_message(e)) from None
        raise AssertionError("unreachable")  # pragma: no cover

    def _open(self) -> http.client.HTTPConnection:
        if self._connection is None:
            if self._https:
                self._connection = http.client.HTTPSConnection(
                    self._host, self._port, timeout=self.timeout, context=self._ssl_context
                )
            else:
                self._connection = http.client.HTTPConnection(
                    self._host, self._port, timeout=self.timeout
                )
        return self._connection

    def _close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @staticmethod
    def _network_message(error: BaseException) -> str:
        if isinstance(error, socket.gaierror):
            return "Cannot connect to Odoo: the host name does not resolve"
        return f"Cannot connect to Odoo: {ErrorSanitizer.sanitize_message(str(error) or type(error).__name__)}"

    def _raise_for_status(
        self, status: int, headers: http.client.HTTPMessage, data: bytes
    ) -> NoReturn:
        """Raise the exception XML-RPC would raise for the same Odoo error."""
        if status in _UNAVAILABLE_HTTP_STATUSES:
            raise OdooUnreachableError(f"Odoo did not answer (HTTP {status})")
        if 300 <= status < 400:
            raise OdooConnectionError(
                f"Odoo redirected the request (HTTP {status}) to {headers.get('Location')}. "
                "Set ODOO_URL to the address it redirects to."
            )
        error = _error_body(data)
        if error is None:
            raise OdooConnectionError(f"Operation failed: HTTP {status} without a JSON error")
        name, message = error
        short_name = name.rsplit(".", 1)[-1]
        if status == 401:
            raise OdooConnectionError(
                f"Authentication failed: Odoo refused the API key ({ErrorSanitizer.sanitize_message(message)})"
            )
        if short_name == "AccessDenied":
            code, fault_string = _FAULT_ACCESS_DENIED, message
        elif short_name == "AccessError":
            code, fault_string = _FAULT_ACCESS_ERROR, message
        elif status in (404, 409, 422):
            code, fault_string = _FAULT_WARNING, message
        elif status == 400:
            raise OdooConnectionError(
                f"Operation failed: Odoo refused the request ({ErrorSanitizer.sanitize_message(message)})"
            )
        else:
            # Like an XML-RPC fault 1: the exception class leads the message,
            # and the sanitizer decides whether it is a business error
            code, fault_string = _FAULT_APPLICATION, f"{short_name}: {message}"
        _raise_for_fault(xmlrpc.client.Fault(code, fault_string))


def _error_body(data: bytes) -> Optional[Tuple[str, str]]:
    """``(name, message)`` of a JSON-2 error body, or None for any other body."""
    try:
        body = json.loads(data)
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return None
    return str(body.get("name") or ""), body["message"]
