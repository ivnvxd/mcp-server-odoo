"""Errors of the Odoo connection and the mapping of Odoo faults onto them.

Shared by the XML-RPC connection, the JSON-2 client and the access controller,
so that none of them imports another for its errors.
"""

import http.client
import json
import urllib.error
import xmlrpc.client
from typing import NoReturn, Optional

from .error_sanitizer import ErrorSanitizer


class OdooConnectionError(Exception):
    """Base exception for Odoo connection errors."""

    pass


class OdooUnreachableError(OdooConnectionError):
    """Odoo did not answer: a network failure, a timeout or a gateway error.

    Unlike a refused login or a configuration problem, this can pass on its
    own, so the server keeps running and connects on a later request.
    """

    pass


# Gateway answers that mean "Odoo behind me is not answering"
UNAVAILABLE_HTTP_STATUSES = frozenset({502, 503, 504})


def is_unreachable(exc: BaseException) -> bool:
    """True when ``exc`` means Odoo did not answer, not that it refused."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in UNAVAILABLE_HTTP_STATUSES
    if isinstance(exc, xmlrpc.client.ProtocolError):
        return exc.errcode in UNAVAILABLE_HTTP_STATUSES
    # OSError covers refused connections, timeouts, DNS failures and URLError
    return isinstance(exc, (OSError, http.client.HTTPException))


class OdooRequestFault(OdooConnectionError):  # noqa: N818 — "Fault" mirrors xmlrpc.client.Fault
    """Odoo answered the request with an error that is not a business error.

    For example a ValueError for a bad domain operator, or a KeyError for an
    unknown field. Odoo did answer, so handlers show the message without a
    connection-error prefix, as they do for ``OdooValidationFault``. It stays
    a sibling of that class so the startup checks that tolerate business
    errors still stop on this one.
    """


class OdooValidationFault(OdooConnectionError):  # noqa: N818 — "Fault" mirrors xmlrpc.client.Fault
    """An XML-RPC fault carrying a user-facing business error.

    Raised when the fault string identifies a validation-class Odoo
    exception (UserError, ValidationError, MissingError, a leading
    AccessError, ...) rather than a transport problem. Subclasses
    OdooConnectionError so every existing ``except OdooConnectionError``
    ladder keeps working unchanged; handlers list it first to surface the
    message without a connection-error prefix.

    ``fault_code`` is the XML-RPC fault code, when there was one. The read
    path uses it to tell an AccessError (``ACCESS_ERROR_FAULT_CODE``) apart.
    """

    def __init__(self, message: str, fault_code: Optional[int] = None):
        super().__init__(message)
        self.fault_code = fault_code


# Odoo's ``/xmlrpc/2/*`` endpoint classifies exceptions for us in the fault
# CODE, and sends the author-written message bare — no class prefix, no
# traceback (see odoo/addons/rpc/controllers/xmlrpc.py:
# xmlrpc_handle_exception_int). Routing on the code is therefore the only
# reliable classification for YOLO mode; the string heuristics below cannot
# see a class name that is never sent.
#   2 = RPC_FAULT_CODE_WARNING          -> UserError / ValidationError
#   4 = RPC_FAULT_CODE_ACCESS_ERROR     -> AccessError (record rules / ACLs)
# 3 (ACCESS_DENIED) is deliberately absent: a rejected login is auth setup,
# not a business rule, and must keep reading as a connection problem.
# Standard mode goes through the MCP module's own proxy. Its 20.0 line (and
# later backports) sends the same codes as core for Odoo user errors, so the
# code route works there too. Older module versions re-wrap every exception
# as faultCode 500 ("Internal Server Error in MCPObjectController: ..."); that
# envelope carries no exception class, so business errors keep reading as
# connection failures against them.
_ODOO_BUSINESS_FAULT_CODES = frozenset({2, 4})
ACCESS_ERROR_FAULT_CODE = 4
ACCESS_DENIED_FAULT_CODE = 3
WARNING_FAULT_CODE = 2

# HTTP-style codes the MCP module's proxy uses for its own refusals, each with
# a message meant for the user: 400 (a call for another database than the
# request's), 403 (not in the MCP User group, model not enabled), 429 (rate
# limit). Shown as-is instead of as a transport failure.
MCP_MODULE_REFUSAL_FAULT_CODES = frozenset({400, 403, 429})


def raise_for_fault(fault: xmlrpc.client.Fault) -> NoReturn:
    """Wrap an application-level XML-RPC fault, classifying validation-class
    business errors so they don't read as connection problems.

    Classification is code-first (``_ODOO_BUSINESS_FAULT_CODES``) because
    that is what Odoo actually sends; the message-shape heuristics remain as
    the fallback for proxies that do not preserve Odoo's codes. Everything
    unclassified raises ``OdooRequestFault``: Odoo answered, so it is not a
    connection error either.
    """
    if fault.faultCode in _ODOO_BUSINESS_FAULT_CODES | MCP_MODULE_REFUSAL_FAULT_CODES:
        # Transport says business: keep the message's prose and line
        # structure instead of running the traceback-shaped reduction.
        raise OdooValidationFault(
            ErrorSanitizer.sanitize_business_fault(fault.faultString), fault.faultCode
        ) from fault

    sanitized_message = ErrorSanitizer.sanitize_xmlrpc_fault(fault.faultString)
    # Older MCP modules wrap every exception as "Internal Server Error in <message>"
    sanitized_message = sanitized_message.removeprefix("Internal Server Error in ")
    if ErrorSanitizer.is_business_fault(fault.faultString):
        raise OdooValidationFault(sanitized_message, fault.faultCode) from fault
    if fault.faultCode == ACCESS_DENIED_FAULT_CODE:
        # A rejected login is auth setup, still a connection problem
        raise OdooConnectionError(f"Operation failed: {sanitized_message}") from fault
    raise OdooRequestFault(f"Odoo error: {sanitized_message}") from fault


def http_error_message(error: urllib.error.HTTPError) -> Optional[str]:
    """Extract the MCP module's own error message from an HTTPError body.

    The module answers failures with
    ``{"success": false, "error": {"message": ..., "code": ...}}``. Reading
    that message keeps its diagnosis ("Model 'x' is not enabled for MCP
    access.") instead of replacing it with a generic one.

    Returns None when the body is missing, unreadable, not that shape, or
    carries a blank message — the caller then falls back to its own wording.
    The body can only be consumed once, so this is called at most once per
    error.
    """
    try:
        payload = json.loads(error.read().decode("utf-8"))
        message = payload.get("error", {}).get("message")
    except Exception:
        return None
    return message.strip() if isinstance(message, str) and message.strip() else None
