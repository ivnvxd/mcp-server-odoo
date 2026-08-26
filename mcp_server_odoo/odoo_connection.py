"""Odoo XML-RPC connection management.

This module provides the OdooConnection class for managing connections
to Odoo via XML-RPC using MCP-specific endpoints.
"""

import json
import logging
import socket
import threading
import urllib.error
import urllib.request
import xmlrpc.client
from contextlib import contextmanager, suppress
from typing import Any, Dict, List, NoReturn, Optional, Tuple, Union
from urllib.parse import urlparse

from .config import OdooConfig
from .error_sanitizer import ErrorSanitizer
from .field_security import is_sensitive_field_name
from .performance import PerformanceManager

logger = logging.getLogger(__name__)

# XML-RPC marshals ints as 32-bit — a larger record id would raise
# OverflowError mid-request; callers bound-check ids against this up front
# and reject them with a clean validation error instead.
XMLRPC_MAX_INT = 2**31 - 1

# Keys whose values must never appear in logs. The exact-name set is kept for
# the handful of names the heuristic deliberately does not flag; anything
# credential-SHAPED (smtp_pass, webhook_secret, openai_api_key, ...) is caught
# by the same detector the read paths use, so a key is never logged in
# cleartext while reads withhold that very field.
_SENSITIVE_KEYS = {"password", "api_key", "token", "secret", "access_token", "new_password"}


def _is_sensitive_log_key(key: Any) -> bool:
    """Whether a write-payload key's value must be redacted from logs."""
    if not isinstance(key, str):
        return False
    return key.lower() in _SENSITIVE_KEYS or is_sensitive_field_name(key)


# Methods that are read-only on the server and therefore safe to re-send
# after a keepalive socket timeout (issue #68 recovery). Anything else —
# create/write/unlink and arbitrary call_model_method targets — could be
# double-executed by a re-send and must let the timeout propagate.
_TIMEOUT_RETRY_SAFE_METHODS = frozenset(
    {
        "read",
        "search",
        "search_read",
        "search_count",
        "fields_get",
        "read_group",
        "formatted_read_group",
        "default_get",
        "name_get",
        "name_search",
    }
)


def _redact_values(value: Any) -> Any:
    """Redact sensitive values in dicts/lists for safe logging."""
    if isinstance(value, dict):
        return {
            k: "***" if _is_sensitive_log_key(k) else _redact_values(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_values(v) for v in value]
    return value


def _describe_args(args: Any) -> Any:
    """Describe positional RPC args for logging: redacted, with long
    strings summarized (binary payloads would bloat the log)."""
    redacted = _redact_values(args)

    def summarize(value: Any) -> Any:
        if isinstance(value, str) and len(value) > 200:
            return f"<str len={len(value)}>"
        if isinstance(value, dict):
            return {k: summarize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [summarize(v) for v in value]
        return value

    return summarize(redacted)


class OdooConnectionError(Exception):
    """Base exception for Odoo connection errors."""

    pass


class OdooValidationFault(OdooConnectionError):  # noqa: N818 — "Fault" mirrors xmlrpc.client.Fault
    """An XML-RPC fault carrying a user-facing business error.

    Raised when the fault string identifies a validation-class Odoo
    exception (UserError, ValidationError, MissingError, a leading
    AccessError, ...) rather than a transport problem. Subclasses
    OdooConnectionError so every existing ``except OdooConnectionError``
    ladder keeps working unchanged; handlers list it first to surface the
    message without a connection-error prefix.
    """

    pass


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
# Standard mode goes through the MCP module's own proxy, which re-wraps every
# exception as faultCode 500 with an "Internal Server Error in
# MCPObjectController: <message>" envelope. That envelope carries no exception
# class, so NEITHER the code route nor the string heuristics can classify it:
# business errors keep reading as connection failures in standard mode until
# the module preserves Odoo's own fault codes. Fixing that is a module-side
# change; this classifier is correct for the YOLO transport it can see.
_ODOO_BUSINESS_FAULT_CODES = frozenset({2, 4})


def _raise_for_fault(fault: xmlrpc.client.Fault) -> NoReturn:
    """Wrap an application-level XML-RPC fault, classifying validation-class
    business errors so they don't read as connection problems.

    Classification is code-first (``_ODOO_BUSINESS_FAULT_CODES``) because
    that is what Odoo actually sends; the message-shape heuristics remain as
    the fallback for proxies that do not preserve Odoo's codes. Everything
    unclassified keeps the historical connection-flavored "Operation failed"
    wrapping.
    """
    if fault.faultCode in _ODOO_BUSINESS_FAULT_CODES:
        # Transport says business: keep the message's prose and line
        # structure instead of running the traceback-shaped reduction.
        raise OdooValidationFault(
            ErrorSanitizer.sanitize_business_fault(fault.faultString)
        ) from fault

    sanitized_message = ErrorSanitizer.sanitize_xmlrpc_fault(fault.faultString)
    if ErrorSanitizer.is_business_fault(fault.faultString):
        raise OdooValidationFault(sanitized_message) from fault
    raise OdooConnectionError(f"Operation failed: {sanitized_message}") from fault


class OdooConnection:
    """Manages XML-RPC connections to Odoo with dynamic endpoint selection.

    This class provides connection management, health checking, and
    proper resource cleanup for Odoo XML-RPC connections. Supports both
    standard MCP endpoints and YOLO mode with standard Odoo endpoints.
    """

    # Connection timeout in seconds
    DEFAULT_TIMEOUT = 30

    def __init__(
        self,
        config: OdooConfig,
        timeout: int = DEFAULT_TIMEOUT,
        performance_manager: Optional[PerformanceManager] = None,
    ):
        """Initialize connection with configuration.

        Args:
            config: OdooConfig object with connection parameters
            timeout: Connection timeout in seconds
            performance_manager: Optional performance manager for optimizations
        """
        self.config = config
        self.timeout = timeout
        self._url_components = self._parse_url(config.url)

        # Get appropriate endpoints based on mode
        endpoints = config.get_endpoint_paths()
        self.DB_ENDPOINT = endpoints["db"]
        self.COMMON_ENDPOINT = endpoints["common"]
        self.OBJECT_ENDPOINT = endpoints["object"]

        # Log YOLO mode if enabled
        if config.is_yolo_enabled:
            if config.yolo_mode == "read":
                logger.warning(
                    "📖 YOLO MODE: READ-ONLY - Connecting to standard Odoo endpoints. "
                    "Write operations will be blocked. Safe for demos and testing."
                )
            elif config.yolo_mode == "true":
                logger.warning(
                    "🚨 YOLO MODE: FULL ACCESS - Connecting to standard Odoo endpoints. "
                    "ALL operations enabled! No MCP security controls. "
                    "FOR DEVELOPMENT/TESTING ONLY - NEVER USE IN PRODUCTION!"
                )

        # Performance manager for optimizations
        self._performance_manager = performance_manager or PerformanceManager(
            config, timeout=timeout
        )

        # XML-RPC proxies (created on connect)
        self._db_proxy: Optional[xmlrpc.client.ServerProxy] = None
        self._common_proxy: Optional[xmlrpc.client.ServerProxy] = None
        self._object_proxy: Optional[xmlrpc.client.ServerProxy] = None

        # Per-proxy locks: ServerProxy/Transport are not thread-safe, and tool
        # handlers offload calls to worker threads via asyncio.to_thread. Locks
        # are scoped to the raw proxy invocation only (never whole methods —
        # execute_kw recurses on locale fallback).
        self._db_proxy_lock = threading.Lock()
        self._common_proxy_lock = threading.Lock()
        self._object_proxy_lock = threading.Lock()

        # Connection state
        self._connected = False
        self._uid: Optional[int] = None
        self._database: Optional[str] = None
        self._authenticated = False
        self._auth_method: Optional[str] = None  # 'api_key' or 'password'
        self._server_version: Optional[str] = None

        mode_info = f" (YOLO mode: {config.yolo_mode})" if config.is_yolo_enabled else ""
        logger.info(f"Initialized OdooConnection for {self._url_components['host']}{mode_info}")

    def _parse_url(self, url: str) -> Dict[str, Any]:
        """Parse and validate Odoo URL.

        Args:
            url: The Odoo server URL

        Returns:
            Dictionary with URL components

        Raises:
            OdooConnectionError: If URL is invalid
        """
        try:
            parsed = urlparse(url)

            if parsed.scheme not in ("http", "https"):
                raise OdooConnectionError(
                    f"Invalid URL scheme: {parsed.scheme}. Must be http or https"
                )

            if not parsed.hostname:
                raise OdooConnectionError("Invalid URL: missing hostname")

            port = parsed.port
            if not port:
                port = 443 if parsed.scheme == "https" else 80

            return {
                "scheme": parsed.scheme,
                "host": parsed.hostname,
                "port": port,
                "path": parsed.path.rstrip("/") or "",
                "base_url": url.rstrip("/"),
            }

        except Exception as e:
            raise OdooConnectionError(f"Failed to parse URL: {e}") from e

    def _build_endpoint_url(self, endpoint: str) -> str:
        """Build full URL for an MCP endpoint.

        Args:
            endpoint: The MCP endpoint path

        Returns:
            Full URL for the endpoint
        """
        return f"{self._url_components['base_url']}{endpoint}"

    def connect(self) -> None:
        """Establish connection to Odoo server.

        Creates XML-RPC proxies for MCP endpoints but doesn't
        authenticate yet. Proxies are created once and reused for the
        server's lifetime.

        In standard mode, resolves the target database first using the
        server-wide ``/xmlrpc/db`` endpoint, then sets the
        ``X-Odoo-Database`` header on the transport so that subsequent
        requests to MCP addon routes (``/mcp/xmlrpc/*``) are routed to
        the correct database — required when multiple DBs exist.

        Raises:
            OdooConnectionError: If connection fails
        """
        if self._connected:
            logger.warning("Already connected to Odoo")
            return

        try:
            # 1. Create DB proxy first (server-wide /xmlrpc/db — works without DB context)
            self._db_proxy = self._performance_manager.get_optimized_connection(self.DB_ENDPOINT)

            # 2. In standard mode, resolve database and set header before creating
            #    MCP proxies so multi-DB routing works.
            if not self.config.is_yolo_enabled:
                self._resolve_and_set_database()

            # 3. Create common/object proxies (now with DB header in standard mode)
            self._common_proxy = self._performance_manager.get_optimized_connection(
                self.COMMON_ENDPOINT
            )
            self._object_proxy = self._performance_manager.get_optimized_connection(
                self.OBJECT_ENDPOINT
            )

            # 4. Test connection by calling server_version
            self._test_connection()

            self._connected = True
            logger.info("Successfully connected to Odoo server")

        except socket.timeout:
            raise OdooConnectionError(f"Connection timeout after {self.timeout} seconds") from None
        except socket.error as e:
            raise OdooConnectionError(
                f"Failed to connect to {self._url_components['host']}:"
                f"{self._url_components['port']}: {e}"
            ) from e
        except Exception as e:
            if isinstance(e, OdooConnectionError):
                raise
            raise OdooConnectionError(f"Connection failed: {e}") from e

    def _resolve_and_set_database(self) -> None:
        """Resolve the target database and set it on the transport header.

        Uses the server-wide ``/xmlrpc/db`` proxy (already created) to
        list databases and pick one, then tells the connection pool to
        inject ``X-Odoo-Database`` on all subsequent requests.
        """
        # If database is explicitly configured, use it directly
        if self.config.database:
            db_name = self.config.database
            logger.info(f"Using configured database for header: {db_name}")
        else:
            # Need to auto-detect — temporarily mark as connected so
            # list_databases() / auto_select_database() can use db_proxy
            self._connected = True
            try:
                db_name = self.auto_select_database()
            except Exception:
                self._connected = False
                raise
            self._connected = False

        self._performance_manager.set_database(db_name)
        # Re-create db_proxy with the new header
        self._db_proxy = self._performance_manager.get_optimized_connection(self.DB_ENDPOINT)
        logger.info(f"Set X-Odoo-Database header to '{db_name}'")

    def _test_connection(self) -> None:
        """Test connection by calling server_version.

        Raises:
            OdooConnectionError: If test fails
        """
        try:
            # Try to get server version via common endpoint
            with self._common_proxy_lock:
                version = self._common_proxy.version()
            self._server_version = version.get("server_version", "") if version else None
            logger.debug(f"Server version: {version}")
        except Exception as e:
            raise OdooConnectionError(f"Connection test failed: {e}") from e

    def disconnect(self, suppress_logging: bool = False) -> None:
        """Close connection and cleanup resources."""
        if not self._connected:
            if not suppress_logging:
                try:
                    logger.warning("Not connected to Odoo")
                except (ValueError, RuntimeError):
                    # Ignore logging errors during cleanup
                    pass
            return

        # Close each proxy's transport — otherwise cached keepalive
        # sockets linger until GC (matters for connect/disconnect cycles)
        for proxy, lock in (
            (self._db_proxy, self._db_proxy_lock),
            (self._common_proxy, self._common_proxy_lock),
            (self._object_proxy, self._object_proxy_lock),
        ):
            if proxy is not None:
                with lock, suppress(Exception):
                    proxy("close")()

        self._db_proxy = None
        self._common_proxy = None
        self._object_proxy = None

        # Clear connection state
        self._connected = False
        self._uid = None
        self._database = None
        self._authenticated = False
        self._auth_method = None

        if not suppress_logging:
            try:
                logger.info("Disconnected from Odoo server")
            except (ValueError, RuntimeError):
                # Ignore logging errors during cleanup
                pass

    def check_health(self) -> Tuple[bool, str]:
        """Check connection health.

        Returns:
            Tuple of (is_healthy, status_message)
        """
        if not self._connected:
            return False, "Not connected"

        try:
            # Try to get server version as health check
            with self._common_proxy_lock:
                version = self._common_proxy.version()
            return True, f"Connected to Odoo {version.get('server_version', 'unknown')}"
        except socket.timeout:
            return False, f"Health check timeout after {self.timeout} seconds"
        except Exception as e:
            return False, f"Health check failed: {e}"

    def test_connection(self) -> bool:
        """Test if connection to Odoo is working.

        Returns:
            True if connection is working, False otherwise
        """
        # If not connected, try to connect first
        if not self._connected:
            try:
                self.connect()
            except Exception as e:
                logger.error(f"Failed to connect: {e}")
                return False

        # Check health
        is_healthy, _ = self.check_health()
        return is_healthy

    def close(self) -> None:
        """Close the connection (alias for disconnect)."""
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self._connected

    @property
    def db_proxy(self) -> xmlrpc.client.ServerProxy:
        """XML-RPC proxy for db endpoint; raises OdooConnectionError if not connected."""
        if not self._connected or not self._db_proxy:
            raise OdooConnectionError("Not connected to Odoo")
        return self._db_proxy

    @property
    def common_proxy(self) -> xmlrpc.client.ServerProxy:
        """XML-RPC proxy for common endpoint; raises OdooConnectionError if not connected."""
        if not self._connected or not self._common_proxy:
            raise OdooConnectionError("Not connected to Odoo")
        return self._common_proxy

    @property
    def object_proxy(self) -> xmlrpc.client.ServerProxy:
        """XML-RPC proxy for object endpoint; raises OdooConnectionError if not connected."""
        if not self._connected or not self._object_proxy:
            raise OdooConnectionError("Not connected to Odoo")
        return self._object_proxy

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()
        return False

    def __del__(self):
        """Cleanup on deletion."""
        try:
            # Only disconnect if we're actually connected
            if hasattr(self, "_connected") and self._connected:
                # Suppress logging during cleanup to avoid I/O errors
                self.disconnect(suppress_logging=True)
        except (ValueError, AttributeError, RuntimeError):
            # ValueError: I/O operation on closed file
            # AttributeError: object might be partially initialized
            # RuntimeError: various cleanup-related errors
            pass

    def list_databases(self) -> List[str]:
        """List all available databases on the Odoo server.

        Returns:
            List of database names

        Raises:
            OdooConnectionError: If listing fails or not connected
        """
        if not self._connected:
            raise OdooConnectionError("Not connected to Odoo")

        # Warn about potential restrictions in YOLO mode
        if self.config.is_yolo_enabled:
            logger.debug(
                "YOLO mode: Database listing may be restricted on standard Odoo. "
                "Consider specifying ODOO_DB explicitly if listing fails."
            )

        try:
            # Call list_db method on database proxy
            with self._db_proxy_lock:
                databases = self.db_proxy.list()
            logger.info(f"Found {len(databases)} databases")
            logger.debug(f"Database names: {databases}")
            return databases  # ty: ignore[invalid-return-type]  # XML-RPC proxy is untyped
        except xmlrpc.client.Fault as e:
            if self.config.is_yolo_enabled and "Access Denied" in str(e):
                # Common error when database listing is restricted
                logger.warning(
                    "Database listing is restricted on this server. "
                    "Please specify ODOO_DB in your configuration."
                )
                if self.config.database:
                    # Return configured database as fallback
                    return [self.config.database]
            logger.error(f"Failed to list databases: {e}")
            raise OdooConnectionError(f"Failed to list databases: {e}") from e
        except Exception as e:
            logger.error(f"Failed to list databases: {e}")
            raise OdooConnectionError(f"Failed to list databases: {e}") from e

    def database_exists(self, db_name: str) -> bool:
        """Check if a specific database exists.

        Args:
            db_name: Name of the database to check

        Returns:
            True if database exists, False otherwise

        Raises:
            OdooConnectionError: If check fails
        """
        try:
            databases = self.list_databases()
            return db_name in databases
        except Exception as e:
            logger.error(f"Failed to check database existence: {e}")
            raise OdooConnectionError(f"Failed to check database existence: {e}") from e

    def auto_select_database(self) -> str:
        """Automatically select an appropriate database.

        Selection logic:
        1. If config.database is set, validate and use it
        2. If only one database exists, use it
        3. If multiple databases exist and one is named 'odoo', use it
        4. Otherwise raise an error

        Returns:
            Selected database name

        Raises:
            OdooConnectionError: If no suitable database can be selected
        """
        # If database is explicitly configured, use it without validation
        # Database listing may be restricted for security reasons
        if self.config.database:
            db_name = self.config.database
            logger.info(f"Using configured database: {db_name}")
            return db_name

        # List available databases
        try:
            databases = self.list_databases()
        except Exception as e:
            # If database listing is restricted, we cannot auto-select
            logger.warning(f"Cannot list databases (may be restricted): {e}")
            raise OdooConnectionError(
                "Database auto-selection failed. Database listing may be restricted. "
                "Please specify ODOO_DB in your configuration."
            ) from e

        # Handle different scenarios
        if not databases:
            raise OdooConnectionError("No databases found on Odoo server")

        if len(databases) == 1:
            db_name = databases[0]
            logger.info(f"Auto-selected only available database: {db_name}")
            return db_name

        # Multiple databases - check for 'odoo'
        if "odoo" in databases:
            logger.info("Auto-selected 'odoo' database from multiple options")
            return "odoo"

        # Cannot auto-select
        raise OdooConnectionError(
            f"Cannot auto-select database. Found {len(databases)} databases: "
            f"{', '.join(databases)}. Please specify ODOO_DB in configuration."
        )

    def validate_database_access(self, db_name: str) -> bool:
        """Validate that we can access the specified database.

        This method attempts to authenticate with the database to verify access.

        Args:
            db_name: Name of the database to validate

        Returns:
            True if database is accessible, False otherwise

        Raises:
            OdooConnectionError: If validation fails
        """
        if not self._connected:
            raise OdooConnectionError("Not connected to Odoo")

        try:
            # For API key auth, we'll need to implement a different check
            # For now, we just verify the database exists
            if self.config.uses_api_key:
                # API key validation would be done during actual authentication
                return self.database_exists(db_name)

            # For username/password auth, try to authenticate
            if self.config.uses_credentials:
                # Try to authenticate with the database
                # This will fail if we don't have access
                with self._common_proxy_lock:
                    uid = self.common_proxy.authenticate(
                        db_name, self.config.username, self.config.password, {}
                    )
                if uid:
                    logger.info(f"Successfully validated access to database '{db_name}'")
                    return True
                else:
                    logger.warning(f"Authentication failed for database '{db_name}'")
                    return False

            # Should not reach here due to config validation
            raise OdooConnectionError("No authentication method configured")

        except xmlrpc.client.Fault as e:
            logger.error(f"XML-RPC fault validating database access: {e}")
            if "Access Denied" in str(e):
                return False
            raise OdooConnectionError(f"Failed to validate database access: {e}") from e
        except Exception as e:
            logger.error(f"Error validating database access: {e}")
            raise OdooConnectionError(f"Failed to validate database access: {e}") from e

    def _authenticate_api_key_standard(self, database: str) -> bool:
        """Authenticate using API key with standard Odoo XML-RPC (YOLO mode).

        Args:
            database: Database name to authenticate against

        Returns:
            True if authentication successful, False otherwise
        """
        if not self.config.username:
            logger.warning("YOLO mode requires username with API key for standard authentication")
            return False

        try:
            # Use standard XML-RPC auth with API key as password
            with self._common_proxy_lock:
                uid = self.common_proxy.authenticate(
                    database, self.config.username, self.config.api_key, {}
                )

            if uid:
                self._uid = uid  # ty: ignore[invalid-assignment]  # XML-RPC proxy is untyped
                self._database = database
                self._auth_method = "api_key"
                self._authenticated = True
                logger.info(
                    f"YOLO mode: Authenticated using API key as password for user '{self.config.username}' (UID: {uid})"
                )
                return True
            else:
                logger.warning(
                    f"YOLO mode: Authentication failed for user '{self.config.username}'"
                )
                return False

        except xmlrpc.client.Fault as e:
            # Handle specific Odoo authentication errors
            fault_string = str(e.faultString).lower()
            if "access denied" in fault_string or "wrong login" in fault_string:
                logger.warning(f"YOLO mode: Invalid credentials for user '{self.config.username}'")
            else:
                logger.warning(f"YOLO mode: Authentication error: {e.faultString}")
            return False
        except Exception as e:
            logger.error(f"YOLO mode: Unexpected authentication error: {e}")
            return False

    def _authenticate_api_key_mcp(self, database: str) -> bool:
        """Authenticate using API key with MCP REST endpoint (standard mode).

        Args:
            database: Database name to authenticate against

        Returns:
            True if authentication successful, False otherwise

        Raises:
            OdooConnectionError: If API request fails critically
        """
        try:
            # Standard MCP API key validation
            url = f"{self._url_components['base_url']}/mcp/auth/validate"

            # Create request with API key header
            req = urllib.request.Request(url)
            req.add_header("X-API-Key", self.config.api_key)
            if database:
                req.add_header("X-Odoo-Database", database)

            # Make the request
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))

                if data.get("success") and data.get("data", {}).get("valid"):
                    self._uid = data["data"].get("user_id")
                    self._database = database
                    self._auth_method = "api_key"
                    self._authenticated = True
                    logger.info(f"Successfully authenticated with MCP API key (UID: {self._uid})")
                    return True
                else:
                    logger.warning("MCP API key validation failed")
                    return False

        except urllib.error.HTTPError as e:
            if e.code == 401:
                logger.warning("Invalid MCP API key")
                return False
            elif e.code == 404:
                logger.warning("MCP auth endpoint not found (MCP module may not be installed)")
                return False
            elif e.code == 429:
                logger.warning("Rate limit exceeded during MCP API key validation")
                return False
            else:
                logger.error(f"HTTP error during MCP API key validation: {e}")
                raise OdooConnectionError(f"Failed to validate API key: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            logger.error(f"Network error during MCP API key validation: {e}")
            raise OdooConnectionError(f"Network error during authentication: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error during MCP API key validation: {e}")
            raise OdooConnectionError(f"Failed to validate API key: {e}") from e

    def _authenticate_api_key(self, database: str) -> bool:
        """Authenticate using API key.

        Routes to appropriate authentication method based on mode.

        Args:
            database: Database name to authenticate against

        Returns:
            True if authentication successful, False otherwise

        Raises:
            OdooConnectionError: If API request fails critically
        """
        if not self.config.api_key:
            return False

        # In YOLO mode, use standard XML-RPC authentication
        if self.config.is_yolo_enabled:
            return self._authenticate_api_key_standard(database)
        else:
            # In standard mode, use MCP REST endpoint
            return self._authenticate_api_key_mcp(database)

    def _authenticate_password(self, database: str) -> bool:
        """Authenticate using username and password.

        Args:
            database: Database name to authenticate against

        Returns:
            True if authentication successful, False otherwise

        Raises:
            OdooConnectionError: If authentication fails
        """
        if not self.config.username or not self.config.password:
            return False

        try:
            # Use common proxy to authenticate
            with self._common_proxy_lock:
                uid = self.common_proxy.authenticate(
                    database, self.config.username, self.config.password, {}
                )

            if uid:
                self._uid = uid  # ty: ignore[invalid-assignment]  # XML-RPC proxy is untyped
                self._database = database
                self._auth_method = "password"
                self._authenticated = True
                logger.info(f"Successfully authenticated with username/password for user ID {uid}")
                return True
            else:
                logger.warning("Username/password authentication failed")
                return False

        except xmlrpc.client.Fault as e:
            logger.warning(f"Authentication fault: {e}")
            return False
        except Exception as e:
            logger.error(f"Error during password authentication: {e}")
            raise OdooConnectionError(f"Failed to authenticate: {e}") from e

    def authenticate(self, database: Optional[str] = None) -> None:
        """Authenticate with Odoo using available credentials.

        Authentication strategy depends on mode:
        - Standard mode: Try MCP API key, then fall back to username/password
        - YOLO mode: Try API key as password, then username/password

        Args:
            database: Database name. If not provided, uses auto-selection.

        Raises:
            OdooConnectionError: If authentication fails
        """
        if not self._connected:
            raise OdooConnectionError("Not connected to Odoo")

        if database:
            db_name = database
        else:
            db_name = self.auto_select_database()

        if self.config.is_yolo_enabled:
            mode_desc = "read-only" if self.config.yolo_mode == "read" else "full access"
            logger.info(f"Authenticating in YOLO {mode_desc} mode for database '{db_name}'")
        else:
            logger.info(f"Authenticating in standard MCP mode for database '{db_name}'")

        auth_errors = []

        if self.config.uses_api_key:
            auth_method = "API key (YOLO mode)" if self.config.is_yolo_enabled else "MCP API key"
            logger.info(f"Attempting {auth_method} authentication")

            try:
                if self._authenticate_api_key(db_name):
                    logger.info(f"Successfully authenticated using {auth_method}")
                    return
                else:
                    error_msg = f"{auth_method} authentication failed"
                    auth_errors.append(error_msg)

                    if self.config.uses_credentials:
                        logger.warning(
                            f"{error_msg} — the configured ODOO_API_KEY was rejected. "
                            "Falling back to username/password authentication. "
                            "Verify or rotate the API key; permission checks will "
                            "use session authentication for this run."
                        )
            except OdooConnectionError as e:
                # Critical error (network, etc.) - don't try fallback
                logger.error(f"Critical error during {auth_method} authentication: {e}")
                raise

        if self.config.uses_credentials:
            logger.info("Attempting username/password authentication")

            try:
                if self._authenticate_password(db_name):
                    logger.info("Successfully authenticated using username/password")
                    return
                else:
                    auth_errors.append("Username/password authentication failed")
            except OdooConnectionError as e:
                # Critical error - propagate it
                logger.error(f"Critical error during password authentication: {e}")
                raise

        if auth_errors:
            error_details = "; ".join(auth_errors)
            mode_hint = ""

            if self.config.is_yolo_enabled:
                mode_hint = " (YOLO mode - ensure Odoo credentials are correct)"
            else:
                mode_hint = " (Standard mode - ensure MCP module is installed and API key is valid)"

            raise OdooConnectionError(f"Authentication failed: {error_details}{mode_hint}")
        else:
            raise OdooConnectionError(
                "No authentication method configured. "
                "Provide either API key or username/password credentials."
            )

    @property
    def is_authenticated(self) -> bool:
        """Check if currently authenticated."""
        return self._authenticated

    @property
    def uid(self) -> Optional[int]:
        """Get authenticated user ID."""
        return self._uid

    @property
    def database(self) -> Optional[str]:
        """Get authenticated database name."""
        return self._database

    @property
    def auth_method(self) -> Optional[str]:
        """Get authentication method used ('api_key' or 'password')."""
        return self._auth_method

    @property
    def performance_manager(self) -> PerformanceManager:
        """Get the performance manager instance."""
        return self._performance_manager

    def execute(self, model: str, method: str, *args) -> Any:
        """Execute an operation on an Odoo model.

        This is a simplified interface that calls execute_kw with empty kwargs.

        Args:
            model: The Odoo model name (e.g., 'res.partner')
            method: The method to call (e.g., 'search', 'read')
            *args: Arguments to pass to the method

        Returns:
            The result from Odoo

        Raises:
            OdooConnectionError: If not authenticated or execution fails
        """
        return self.execute_kw(model, method, list(args), {})

    def execute_kw(self, model: str, method: str, args: List[Any], kwargs: Dict[str, Any]) -> Any:
        """Execute an operation on an Odoo model with keyword arguments.

        This is the main method for interacting with Odoo models via XML-RPC.

        Args:
            model: The Odoo model name (e.g., 'res.partner')
            method: The method to call (e.g., 'search_read')
            args: List of positional arguments for the method
            kwargs: Dictionary of keyword arguments for the method

        Returns:
            The result from Odoo

        Raises:
            OdooConnectionError: If not authenticated or execution fails
        """
        if not self._authenticated:
            raise OdooConnectionError("Not authenticated. Call authenticate() first.")

        if not self._connected:
            raise OdooConnectionError("Not connected to Odoo")

        # Get the appropriate password/token based on auth method
        password_or_token = (
            self.config.api_key if self._auth_method == "api_key" else self.config.password
        )

        # Inject locale into context as default (caller-provided lang takes precedence)
        if self.config.locale:
            if "context" not in kwargs:
                kwargs["context"] = {}
            kwargs["context"].setdefault("lang", self.config.locale)

        try:
            # Log the operation (values redacted — write payloads can carry
            # passwords/PII that must not land in log files)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Executing {method} on {model} with "
                    f"args={_describe_args(args)}, kwargs={_redact_values(kwargs)}"
                )

            # Execute via object proxy. The transport's timeout-retry flag is
            # set under the same lock acquisition as the request, so
            # concurrent callers can't race it.
            with self._object_proxy_lock:
                self.object_proxy("transport").timeout_retry_safe = (
                    method in _TIMEOUT_RETRY_SAFE_METHODS
                )
                result = self.object_proxy.execute_kw(
                    self._database, self._uid, password_or_token, model, method, args, kwargs
                )

            logger.debug("Operation completed successfully")
            return result

        except xmlrpc.client.Fault as e:
            # Handle an invalid lang — drop it and retry. Only blame (and
            # permanently disable) the CONFIGURED locale when it is actually
            # the offending value: a caller-supplied context lang used to null
            # self.config.locale on the shared config, silently turning
            # ODOO_MCP_LOCALE off for every later request in the process.
            context = kwargs.get("context") or {}
            bad_lang = context.get("lang")
            if "Invalid language code" in e.faultString and bad_lang:
                if bad_lang == self.config.locale:
                    logger.warning(
                        f"Locale '{bad_lang}' is not installed in Odoo. "
                        "Falling back to default language."
                    )
                    self.config.locale = None
                else:
                    logger.warning(
                        f"Language '{bad_lang}' requested for this call is not installed "
                        "in Odoo; retrying without it (server locale unchanged)."
                    )
                context.pop("lang", None)
                return self.execute_kw(model, method, args, kwargs)

            # Odoo's XML-RPC marshaller (allow_none=False) faults on void
            # returns, but the method already ran. Match the full dump_nil
            # signature so unrelated faults mentioning "cannot marshal None"
            # aren't silently swallowed.
            if "cannot marshal None unless allow_none" in e.faultString:
                return None

            logger.error(f"XML-RPC fault during {method} on {model}: {e}")
            # Sanitize and classify: business errors raise OdooValidationFault,
            # everything else stays connection-flavored
            _raise_for_fault(e)
        except socket.timeout:
            logger.error(f"Timeout during {method} on {model}")
            raise OdooConnectionError(f"Operation timeout after {self.timeout} seconds") from None
        except Exception as e:
            logger.error(f"Error during {method} on {model}: {e}")
            # Sanitize generic errors as well
            sanitized_message = ErrorSanitizer.sanitize_message(str(e))
            raise OdooConnectionError(f"Operation failed: {sanitized_message}") from e

    def search(self, model: str, domain: List[Union[str, List[Any]]], **kwargs) -> List[int]:
        """Search for records matching a domain.

        Args:
            model: The Odoo model name
            domain: Odoo domain filter (e.g., [['is_company', '=', True]])
            **kwargs: Additional parameters (limit, offset, order, context —
                e.g. ``context={"active_test": False}`` to include archived
                records)

        Returns:
            List of record IDs matching the domain
        """
        if "context" in kwargs:
            # Copy: execute_kw mutates the context dict (locale injection),
            # same as read()/search_read(). A caller reusing one dict across
            # calls must not accumulate our injected keys.
            kwargs = {**kwargs, "context": dict(kwargs["context"])}
        return self.execute_kw(model, "search", [domain], kwargs)

    def read(
        self,
        model: str,
        ids: List[int],
        fields: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Read records by IDs.

        Args:
            model: The Odoo model name
            ids: List of record IDs to read
            fields: List of field names to read (None for all fields)
            context: Optional Odoo context override for this call
                (e.g. ``{"bin_size": True}`` to get binary size placeholders
                instead of full base64 payloads)

        Returns:
            List of dictionaries containing record data
        """
        kwargs: Dict[str, Any] = {}
        if fields is not None:
            kwargs["fields"] = fields
        if context:
            # Copy: execute_kw mutates the context dict (locale injection)
            kwargs["context"] = dict(context)

        with self._performance_manager.monitor.track_operation(f"read_{model}"):
            records = self.execute_kw(model, "read", [ids], kwargs)

        return records

    def search_read(
        self,
        model: str,
        domain: List[Union[str, List[Any]]],
        fields: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Search for records and read their data in one operation.

        Args:
            model: The Odoo model name
            domain: Odoo domain filter
            fields: List of field names to read (None for all fields)
            context: Optional Odoo context override for this call
                (e.g. ``{"active_test": False}`` to include archived records)
            **kwargs: Additional parameters (limit, offset, order)

        Returns:
            List of dictionaries containing record data
        """
        if fields is not None:
            kwargs["fields"] = fields
        if context:
            # Copy: execute_kw mutates the context dict (locale injection)
            kwargs["context"] = dict(context)
        return self.execute_kw(model, "search_read", [domain], kwargs)

    def fields_get(
        self,
        model: str,
        attributes: Optional[List[str]] = None,
        allfields: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Get field definitions for a model.

        Args:
            model: The Odoo model name
            attributes: List of field attributes to return
            allfields: Field names to restrict the result to (Odoo's
                ``fields_get(allfields=...)`` server-side filter; unknown
                names are silently omitted). None/empty returns every field.

        Returns:
            Dictionary mapping field names to their definitions
        """
        # Check cache first — only unfiltered, all-attribute calls use it
        cached_fields = self._performance_manager.get_cached_fields(model)
        if cached_fields and not attributes and not allfields:
            logger.debug(f"Field definitions for {model} retrieved from cache")
            return cached_fields

        # Get fields from server (allfields is fields_get's first positional)
        args: List[Any] = [allfields] if allfields else []
        kwargs = {}
        if attributes:
            kwargs["attributes"] = attributes

        with self._performance_manager.monitor.track_operation(f"fields_get_{model}"):
            fields = self.execute_kw(model, "fields_get", args, kwargs)

        # Cache only complete responses (all fields, all attributes)
        if not attributes and not allfields:
            self._performance_manager.cache_fields(model, fields)

        return fields

    def search_count(self, model: str, domain: List[Union[str, List[Any]]]) -> int:
        """Count records matching a domain.

        Args:
            model: The Odoo model name
            domain: Odoo domain filter

        Returns:
            Number of records matching the domain
        """
        return self.execute_kw(model, "search_count", [domain], {})

    def create(self, model: str, values: Dict[str, Any]) -> int:
        """Create a new record.

        Args:
            model: The Odoo model name
            values: Dictionary of field values for the new record

        Returns:
            ID of the created record

        Raises:
            OdooConnectionError: If creation fails
        """
        try:
            with self._performance_manager.monitor.track_operation(f"create_{model}"):
                record_id = self.execute_kw(model, "create", [values], {})
                logger.info(f"Created {model} record with ID {record_id}")
                return record_id
        except Exception as e:
            logger.error(f"Failed to create {model} record: {e}")
            raise

    def write(self, model: str, ids: List[int], values: Dict[str, Any]) -> bool:
        """Update existing records.

        Args:
            model: The Odoo model name
            ids: List of record IDs to update
            values: Dictionary of field values to update

        Returns:
            True if update was successful

        Raises:
            OdooConnectionError: If update fails
        """
        try:
            with self._performance_manager.monitor.track_operation(f"write_{model}"):
                result = self.execute_kw(model, "write", [ids, values], {})
                logger.info(f"Updated {len(ids)} {model} record(s)")
                return result
        except Exception as e:
            logger.error(f"Failed to update {model} records: {e}")
            raise

    def unlink(self, model: str, ids: List[int]) -> bool:
        """Delete records.

        Args:
            model: The Odoo model name
            ids: List of record IDs to delete

        Returns:
            True if deletion was successful

        Raises:
            OdooConnectionError: If deletion fails
        """
        try:
            with self._performance_manager.monitor.track_operation(f"unlink_{model}"):
                result = self.execute_kw(model, "unlink", [ids], {})
                logger.info(f"Deleted {len(ids)} {model} record(s)")
                return result
        except Exception as e:
            logger.error(f"Failed to delete {model} records: {e}")
            raise

    @property
    def server_version(self) -> Optional[str]:
        """Cached Odoo server version string (e.g. '17.0', '18.0')."""
        return self._server_version

    def get_server_version(self) -> Optional[Dict[str, Any]]:
        """Get Odoo server version information.

        Returns:
            Dictionary with version information or None if not connected
        """
        if not self._connected:
            return None

        try:
            with self._common_proxy_lock:
                return self.common_proxy.version()  # ty: ignore[invalid-return-type]  # XML-RPC proxy is untyped
        except Exception as e:
            logger.error(f"Failed to get server version: {e}")
            return None

    def get_major_version(self) -> Optional[int]:
        """Return the Odoo major version number, or ``None`` if unknown.

        Handles standard versions (e.g. ``'18.0'`` → ``18``) and SaaS
        versions (e.g. ``'saas~18.1'`` → ``18``). Reads the cached
        ``_server_version`` populated during ``connect()``.

        Public so that tool handlers can branch on Odoo version (e.g. to
        choose between ``formatted_read_group`` and ``read_group``).
        """
        if not self._server_version:
            return None
        try:
            version = self._server_version
            # SaaS versions: 'saas~18.1' → strip prefix to get '18.1'
            if "~" in version:
                version = version.split("~", 1)[1]
            return int(version.split(".")[0])
        except (ValueError, IndexError):
            return None

    def build_record_url(self, model: str, record_id: int) -> str:
        """Build a direct URL to a record in the Odoo web interface.

        Uses the modern /odoo/ path for Odoo 18+, falls back to legacy
        /web# hash format for older versions.
        """
        base_url = self._url_components["base_url"]
        major = self.get_major_version()
        if major is not None and major >= 18:
            return f"{base_url}/odoo/{model}/{record_id}"
        return f"{base_url}/web#id={record_id}&model={model}&view_type=form"


@contextmanager
def create_connection(config: OdooConfig, timeout: int = OdooConnection.DEFAULT_TIMEOUT):
    """Create a connection context manager.

    Args:
        config: OdooConfig object
        timeout: Connection timeout in seconds

    Yields:
        Connected OdooConnection instance

    Example:
        with create_connection(config) as conn:
            # Use connection
            version = conn.common_proxy.version()
    """
    conn = OdooConnection(config, timeout)
    try:
        conn.connect()
        yield conn
    finally:
        conn.disconnect()
