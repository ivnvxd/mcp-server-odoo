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
from typing import Any, Dict, List, NoReturn, Optional, Tuple
from urllib.parse import quote, urlparse

from .error_sanitizer import ErrorSanitizer
from .odoo_connection import (
    _UNAVAILABLE_HTTP_STATUSES,
    ACCESS_DENIED_FAULT_CODE,
    ACCESS_ERROR_FAULT_CODE,
    WARNING_FAULT_CODE,
    OdooConnectionError,
    OdooRequestFault,
    OdooUnreachableError,
    OdooValidationFault,
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

# Odoo's application-error fault code; the others come from odoo_connection
_FAULT_APPLICATION = 1


class Json2UnavailableError(OdooConnectionError):
    """The server has no JSON-2: no ``/web/version`` route, or Odoo before 19.
    ``ODOO_RPC_TRANSPORT=auto`` then uses XML-RPC."""


class Json2RouteError(OdooConnectionError):
    """A ``/json/2`` answer without a JSON-2 error body: a page from a proxy that
    blocks the route, or from Odoo for an unknown database."""


class Json2AuthError(OdooConnectionError):
    """Odoo refused the API key (HTTP 401). ``ODOO_RPC_TRANSPORT=auto`` with
    ODOO_USER and ODOO_PASSWORD then tries them over XML-RPC."""


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
        status, response_headers, data = self._send("POST", path, payload, headers, retry_safe)
        if status == 200:
            try:
                return json.loads(data)
            except ValueError as e:
                raise OdooConnectionError(
                    f"Operation failed: {model}.{method} answered with invalid JSON"
                ) from e
        self._raise_for_status(status, response_headers, data)

    def version(self) -> Dict[str, Any]:
        """``GET /web/version``: the server version, without a login (blocking).

        Odoo 19 and later answer ``{"version_info": [...], "version": "20.0"}``.
        Odoo 18 and older have no such route, and JSON-2 neither.

        Raises:
            OdooUnreachableError: Odoo did not answer
            OdooConnectionError: no version route, so no JSON-2
        """
        status, headers, data = self._send(
            "GET", f"{self._base_path}/web/version", None, {"Accept": "application/json"}, True
        )
        if status == 200:
            try:
                info = json.loads(data)
            except ValueError:
                info = None
            if isinstance(info, dict) and isinstance(info.get("version"), str):
                return info
        if status in _UNAVAILABLE_HTTP_STATUSES:
            raise OdooUnreachableError(f"Odoo did not answer (HTTP {status})")
        if 300 <= status < 400:
            raise OdooConnectionError(_redirect_message(status, headers))
        raise Json2UnavailableError(
            f"Odoo has no /web/version route (HTTP {status}): JSON-2 needs Odoo 19 or later"
        )

    def close(self) -> None:
        """Close the connection; the next call opens a new one."""
        with self._lock:
            self._close()

    def _send(
        self,
        verb: str,
        path: str,
        payload: Optional[bytes],
        headers: Dict[str, str],
        retry_safe: bool,
    ) -> Tuple[int, http.client.HTTPMessage, bytes]:
        """One request under the lock, with one retry on a stale keepalive connection."""
        with self._lock:
            for attempt in (0, 1):
                reused = self._connection is not None and self._connection.sock is not None
                connection = self._open()
                try:
                    connection.request(verb, path, body=payload, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    if response.will_close:
                        self._close()
                    return response.status, response.headers, data
                except TimeoutError:
                    self._close()
                    if attempt or not reused or not retry_safe:
                        # Unreachable: at connect, as over XML-RPC, the server keeps
                        # running and retries with a backoff. A later call is not retried.
                        raise OdooUnreachableError(
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
        detail = ErrorSanitizer.sanitize_message(str(error) or type(error).__name__)
        # The sanitizer already words a refused connection as "Cannot connect to Odoo server"
        return (
            detail
            if detail.startswith("Cannot connect to Odoo")
            else f"Cannot connect to Odoo: {detail}"
        )

    def _raise_for_status(
        self, status: int, headers: http.client.HTTPMessage, data: bytes
    ) -> NoReturn:
        """Raise the exception XML-RPC would raise for the same Odoo error."""
        if status in _UNAVAILABLE_HTTP_STATUSES:
            raise OdooUnreachableError(f"Odoo did not answer (HTTP {status})")
        if 300 <= status < 400:
            # The target can be an internal host: the log names it, the
            # client-facing error does not
            logger.warning(_redirect_message(status, headers))
            raise OdooConnectionError(
                f"Odoo redirected the request (HTTP {status}). Set ODOO_URL to the address "
                "it redirects to, which the server log names."
            )
        error = _error_body(data)
        if error is None:
            raise Json2RouteError(f"Operation failed: HTTP {status} without a JSON error")
        name, message = error
        short_name = name.rsplit(".", 1)[-1]
        if status == 401:
            raise Json2AuthError(
                f"Authentication failed: Odoo refused the API key "
                f"({ErrorSanitizer.sanitize_message(message)}). On Odoo 20 and later the key "
                "needs the rpc scope."
            )
        if short_name == "AccessDenied":
            code, fault_string = ACCESS_DENIED_FAULT_CODE, message
        elif short_name == "AccessError":
            code, fault_string = ACCESS_ERROR_FAULT_CODE, message
        elif status in (404, 409, 422):
            code, fault_string = WARNING_FAULT_CODE, message
        elif status == 400:
            raise OdooRequestFault(
                f"Odoo refused the request: {ErrorSanitizer.sanitize_message(message)}"
            )
        else:
            # Like an XML-RPC fault 1: the exception class leads the message,
            # and the sanitizer decides whether it is a business error
            code, fault_string = _FAULT_APPLICATION, f"{short_name}: {message}"
        _raise_for_fault(xmlrpc.client.Fault(code, fault_string))


def _redirect_message(status: int, headers: http.client.HTTPMessage) -> str:
    return (
        f"Odoo redirected the request (HTTP {status}) to {headers.get('Location')}. "
        "Set ODOO_URL to the address it redirects to."
    )


def _error_body(data: bytes) -> Optional[Tuple[str, str]]:
    """``(name, message)`` of a JSON-2 error body, or None for any other body."""
    try:
        body = json.loads(data)
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return None
    return str(body.get("name") or ""), body["message"]


# Positional parameters of the methods the package sends through execute_kw,
# by name, from the Odoo 19 and 20 signatures. The flag marks a record method:
# its first positional argument is the record ids. A model method takes none,
# and JSON-2 refuses ids for it. read_group is left out on purpose: its
# signature changed in Odoo 20, and the package calls it only before Odoo 19.
_SIGNATURES: Dict[str, Tuple[bool, Tuple[str, ...]]] = {
    "search": (False, ("domain", "offset", "limit", "order")),
    "search_read": (False, ("domain", "fields", "offset", "limit", "order")),
    "search_count": (False, ("domain", "limit")),
    "fields_get": (False, ("allfields", "attributes")),
    "create": (False, ("vals_list",)),
    "default_get": (False, ("fields",)),
    "name_search": (False, ("name", "domain", "operator", "limit")),
    "formatted_read_group": (
        False,
        ("domain", "groupby", "aggregates", "having", "offset", "limit", "order"),
    ),
    "context_get": (False, ()),
    "read": (True, ("fields", "load")),
    "write": (True, ("vals",)),
    "unlink": (True, ()),
    "web_save_multi": (True, ("vals_list", "specification")),
    "message_post": (True, ()),
}


def json2_arguments(
    model: str, method: str, args: List[Any], kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """The JSON-2 body for an ``execute_kw(model, method, args, kwargs)`` call.

    JSON-2 takes named arguments only. For a method in ``_SIGNATURES`` the
    positional arguments get their parameter names. For any other method
    (``call_model_method``) only a leading id or list of ids can be named,
    as ``ids``; the keyword arguments pass through, ``context`` included.

    Raises:
        OdooValidationFault: an argument that cannot be named
    """
    body = dict(kwargs)
    rest = list(args)
    signature = _SIGNATURES.get(method)
    if signature is None:
        if rest and _is_ids(rest[0]):
            body["ids"] = _as_ids(rest.pop(0))
        if rest:
            raise OdooValidationFault(
                f"JSON-2 takes named arguments only: pass the arguments of {model}.{method} "
                "after the record ids in keyword_arguments, by parameter name"
            )
        return body

    takes_ids, names = signature
    if takes_ids:
        if not rest or not (_is_ids(rest[0]) or rest[0] in ([], ())):
            raise OdooValidationFault(f"{model}.{method} needs the record ids first")
        body["ids"] = _as_ids(rest.pop(0))
    if len(rest) > len(names):
        raise OdooValidationFault(f"{model}.{method} takes at most {len(names)} arguments")
    for name, value in zip(names[: len(rest)], rest, strict=True):
        if name in body:
            raise OdooValidationFault(f"{model}.{method}: {name} is given twice")
        body[name] = value
    return body


def _as_ids(value: Any) -> List[int]:
    return [value] if isinstance(value, int) else list(value)


def _is_ids(value: Any) -> bool:
    """An id or a non-empty list of ids (booleans excluded: True is not record 1).

    An empty list is not taken as ids: it can as well be an empty domain.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return (
        isinstance(value, (list, tuple))
        and len(value) > 0
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    )
