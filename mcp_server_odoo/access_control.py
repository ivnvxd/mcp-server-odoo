"""Access control integration with Odoo MCP module.

This module provides integration with the Odoo MCP module's access control
system via REST API endpoints.
"""

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .config import OdooConfig
from .error_handling import ValidationError

logger = logging.getLogger(__name__)


class AccessControlError(Exception):
    """Exception for access control failures."""

    pass


class AccessControlUnavailableError(AccessControlError):
    """Permission could not be EVALUATED (infrastructure failure).

    Distinct from a denial: a network outage or malformed response from
    the /mcp/ REST endpoints means "could not verify", not "not allowed".
    Callers should surface these as connection errors (retryable), never
    as access denials. Still fails closed — operations do not proceed.
    """

    pass


def access_denied_message(error: Exception) -> str:
    """Prefix an access failure with "Access denied: " unless it already says so.

    The MCP module's own refusals are self-labelling ("Access denied: your
    user is not authorized for MCP..."), so the unconditional prefix the
    handlers used produced "Access denied: Access denied: ...". Prefix only
    when the message does not already open with it.
    """
    message = str(error).strip()
    if message.lower().startswith("access denied"):
        return message
    return f"Access denied: {message}"


def _http_error_message(error: urllib.error.HTTPError) -> Optional[str]:
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


@dataclass
class ModelPermissions:
    """Permissions for a specific model."""

    model: str
    enabled: bool
    can_read: bool = False
    can_write: bool = False
    can_create: bool = False
    can_unlink: bool = False

    def can_perform(self, operation: str) -> bool:
        """Check if a specific operation is allowed."""
        operation_map = {
            "read": self.can_read,
            "write": self.can_write,
            "create": self.can_create,
            "unlink": self.can_unlink,
            "delete": self.can_unlink,  # Alias
        }
        return operation_map.get(operation, False)


@dataclass
class CacheEntry:
    """Cache entry for permission data."""

    data: Any
    timestamp: datetime

    def is_expired(self, ttl_seconds: int) -> bool:
        """Check if cache entry is expired."""
        return datetime.now() - self.timestamp > timedelta(seconds=ttl_seconds)


class AccessController:
    """Controls access to Odoo models via MCP module REST API."""

    # Cache TTL in seconds
    CACHE_TTL = 300  # 5 minutes

    # MCP REST API endpoints
    MODELS_ENDPOINT = "/mcp/models"
    MODEL_ACCESS_ENDPOINT = "/mcp/models/{model}/access"

    def __init__(
        self,
        config: OdooConfig,
        database: Optional[str] = None,
        cache_ttl: int = CACHE_TTL,
        auth_method: Optional[str] = None,
    ):
        """Initialize access controller.

        Args:
            config: OdooConfig with connection details and API key
            database: Resolved database name (needed for session auth when config.database is None)
            cache_ttl: Cache time-to-live in seconds
            auth_method: How the connection actually authenticated ('api_key'
                or 'password'). When the configured API key was rejected and
                the connection fell back to password auth, permission checks
                must use session auth too — not keep sending the dead key.
        """
        self.config = config
        self.database = database or config.database
        self.cache_ttl = cache_ttl
        self.auth_method = auth_method
        self._cache: Dict[str, CacheEntry] = {}
        self._session_id: Optional[str] = None
        # Checks run concurrently in asyncio.to_thread workers: the cache
        # lock keeps entry get/set/clear consistent, the session lock makes
        # session (re-)authentication single-flight instead of thrashing
        self._cache_lock = threading.Lock()
        self._session_lock = threading.Lock()

        # Parse base URL
        self.base_url = config.url.rstrip("/")

        # In YOLO mode, skip API key validation and MCP checks
        if config.is_yolo_enabled:
            mode_desc = "READ-ONLY" if config.yolo_mode == "read" else "FULL ACCESS"
            logger.warning(
                f"🚨 YOLO mode ({mode_desc}): Access control bypassed! "
                f"All models accessible, MCP security disabled."
            )
            return  # Skip API validation

        if config.api_key:
            logger.info(f"Initialized AccessController for {self.base_url} (API key auth)")
        elif config.uses_credentials:
            logger.info(f"Initialized AccessController for {self.base_url} (session auth)")
        else:
            logger.warning(
                "No authentication configured for MCP access control. "
                "Set ODOO_API_KEY or provide ODOO_USER/ODOO_PASSWORD."
            )

    def _authenticate_session(self) -> None:
        """Authenticate via Odoo web session to get a session cookie.

        Used as fallback when no API key is configured.

        Raises:
            AccessControlError: If session authentication fails
        """
        url = f"{self.base_url}/web/session/authenticate"
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "call",
                "params": {
                    "login": self.config.username,
                    "password": self.config.password,
                    "db": self.database,
                },
                "id": 1,
            }
        ).encode()

        req = urllib.request.Request(url, data=body)
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                # Extract session_id from Set-Cookie header
                cookie_header = response.headers.get("Set-Cookie", "")
                match = re.search(r"session_id=([^;]+)", cookie_header)
                if not match:
                    raise AccessControlError(
                        "Session authentication failed: no session cookie returned"
                    )

                self._session_id = match.group(1)

                # Verify the response indicates success (no JSON-RPC error)
                data = json.loads(response.read().decode("utf-8"))
                if "error" in data:
                    self._session_id = None
                    raise AccessControlError("Session authentication failed: invalid credentials")

                logger.info("Session authentication successful")

        except urllib.error.HTTPError as e:
            raise AccessControlError(f"Session authentication failed: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise AccessControlError(f"Session authentication failed: {e.reason}") from e

    def _ensure_session(self) -> Optional[str]:
        """Ensure a valid session exists and RETURN its id.

        The id must be read under the same lock that creates it: a concurrent
        401 handler nulls ``self._session_id`` while holding the lock, so a
        caller that re-read the attribute afterwards could interleave and send
        the literal header ``Cookie: session_id=None``.
        """
        with self._session_lock:
            if not self._session_id:
                self._authenticate_session()
            return self._session_id

    def _make_request(self, endpoint: str, timeout: int = 30) -> Dict[str, Any]:
        """Make authenticated request to MCP REST API.

        Uses API key if available, otherwise falls back to session cookie auth.
        On session 401, retries once after re-authenticating.

        Args:
            endpoint: API endpoint path
            timeout: Request timeout in seconds

        Returns:
            Parsed JSON response

        Raises:
            AccessControlError: If request fails
        """
        return self._do_request(endpoint, timeout, allow_session_retry=True)

    def _do_request(self, endpoint: str, timeout: int, allow_session_retry: bool) -> Dict[str, Any]:
        """Internal request method with optional session retry.

        Args:
            endpoint: API endpoint path
            timeout: Request timeout in seconds
            allow_session_retry: Whether to retry with a fresh session on 401

        Returns:
            Parsed JSON response

        Raises:
            AccessControlError: If request fails
        """
        url = f"{self.base_url}{endpoint}"
        uses_session = False

        req = urllib.request.Request(url)
        # Use the API key only when the connection actually authenticated
        # with it (or auth_method is unknown). After a password fallback the
        # configured key is known-rejected — sending it would 401 every check.
        use_api_key = self.config.api_key and self.auth_method != "password"
        if use_api_key:
            req.add_header("X-API-Key", self.config.api_key)
        elif self.config.uses_credentials:
            session_id = self._ensure_session()
            req.add_header("Cookie", f"session_id={session_id}")
            uses_session = True
        req.add_header("Accept", "application/json")
        if self.database:
            req.add_header("X-Odoo-Database", self.database)

        try:
            logger.debug(f"Making request to {url}")

            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))

                # Check for API response success
                if not data.get("success", False):
                    error_msg = data.get("error", {}).get("message", "Unknown error")
                    raise AccessControlError(f"API error: {error_msg}")

                return data

        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Session expired — retry once with a fresh session
                if uses_session and allow_session_retry:
                    logger.info("Session expired, re-authenticating...")
                    with self._session_lock:
                        self._session_id = None
                    return self._do_request(endpoint, timeout, allow_session_retry=False)

                if use_api_key:
                    raise AccessControlError(
                        "API key rejected by MCP module. "
                        "Verify ODOO_API_KEY is valid and the MCP module is installed."
                    ) from e
                raise AccessControlError(
                    "MCP REST API authentication failed. "
                    "Configure ODOO_API_KEY or use YOLO mode (ODOO_YOLO=read)."
                ) from e
            elif e.code == 403:
                # The module explains exactly what was refused ("Model 'x' is
                # not enabled for MCP access."); a generic message here sends
                # users hunting a credential problem that does not exist.
                raise AccessControlError(
                    _http_error_message(e) or "Access denied to MCP endpoints"
                ) from e
            elif e.code == 404:
                raise AccessControlUnavailableError(f"Endpoint not found: {endpoint}") from e
            else:
                raise AccessControlUnavailableError(f"HTTP error {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise AccessControlUnavailableError(f"Connection error: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise AccessControlUnavailableError(f"Invalid JSON response: {e}") from e
        except AccessControlError:
            raise
        except Exception as e:
            raise AccessControlUnavailableError(f"Request failed: {e}") from e

    def _get_from_cache(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        with self._cache_lock:
            if key in self._cache:
                entry = self._cache[key]
                if not entry.is_expired(self.cache_ttl):
                    logger.debug(f"Cache hit for {key}")
                    return entry.data
                else:
                    logger.debug(f"Cache expired for {key}")
                    del self._cache[key]
        return None

    def _set_cache(self, key: str, data: Any) -> None:
        """Set value in cache."""
        with self._cache_lock:
            self._cache[key] = CacheEntry(data=data, timestamp=datetime.now())
        logger.debug(f"Cached {key}")

    def clear_cache(self) -> None:
        """Clear all cached data."""
        with self._cache_lock:
            self._cache.clear()
        logger.info("Cleared access control cache")

    def get_enabled_models(self) -> List[Dict[str, str]]:
        """Get list of all MCP-enabled models.

        Returns:
            List of dicts with 'model' and 'name' keys (newer MCP modules add 'operations')

        Raises:
            AccessControlError: If request fails
        """
        # In YOLO mode, return empty list (all models are allowed)
        if self.config.is_yolo_enabled:
            logger.debug("YOLO mode: All models are accessible")
            return []  # Empty list indicates all models allowed

        cache_key = "enabled_models"

        # Check cache
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        # Make request
        response = self._make_request(self.MODELS_ENDPOINT)
        models = response.get("data", {}).get("models", [])

        # Cache result
        self._set_cache(cache_key, models)

        logger.info(f"Retrieved {len(models)} enabled models")
        return models

    def is_model_enabled(self, model: str) -> bool:
        """Check if a model is MCP-enabled.

        Args:
            model: The Odoo model name (e.g., 'res.partner')

        Returns:
            True if model is enabled, False otherwise
        """
        # In YOLO mode, all models are enabled
        if self.config.is_yolo_enabled:
            logger.debug(f"YOLO mode: Model '{model}' is accessible")
            return True

        try:
            enabled_models = self.get_enabled_models()
            return any(m["model"] == model for m in enabled_models)
        except AccessControlError as e:
            logger.error(f"Failed to check if model {model} is enabled: {e}")
            return False

    def get_model_permissions(self, model: str) -> ModelPermissions:
        """Get permissions for a specific model.

        Args:
            model: The Odoo model name

        Returns:
            ModelPermissions object with permission details

        Raises:
            AccessControlError: If request fails
        """
        # In YOLO mode, return permissions based on mode level
        if self.config.is_yolo_enabled:
            if self.config.yolo_mode == "read":
                # Read-only mode: only read operations allowed
                return ModelPermissions(
                    model=model,
                    enabled=True,
                    can_read=True,
                    can_write=False,
                    can_create=False,
                    can_unlink=False,
                )
            else:  # yolo_mode == "true"
                # Full access mode: all operations allowed
                return ModelPermissions(
                    model=model,
                    enabled=True,
                    can_read=True,
                    can_write=True,
                    can_create=True,
                    can_unlink=True,
                )

        cache_key = f"permissions_{model}"

        # Check cache
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        # Make request
        endpoint = self.MODEL_ACCESS_ENDPOINT.format(model=model)
        response = self._make_request(endpoint)
        data = response.get("data", {})

        # Parse permissions
        permissions = ModelPermissions(
            model=data.get("model", model),
            enabled=data.get("enabled", False),
            can_read=data.get("operations", {}).get("read", False),
            can_write=data.get("operations", {}).get("write", False),
            can_create=data.get("operations", {}).get("create", False),
            can_unlink=data.get("operations", {}).get("unlink", False),
        )

        # Cache result
        self._set_cache(cache_key, permissions)

        logger.debug(f"Retrieved permissions for {model}: {permissions}")
        return permissions

    def check_operation_allowed(self, model: str, operation: str) -> Tuple[bool, Optional[str]]:
        """Check if an operation is allowed on a model.

        Args:
            model: The Odoo model name
            operation: The operation to check (read, write, create, unlink)

        Returns:
            Tuple of (allowed, error_message)
        """
        # In YOLO mode, check based on mode level
        if self.config.is_yolo_enabled:
            read_operations = {
                "read",
                "search",
                "search_read",
                "fields_get",
                "count",
                "search_count",
            }

            if operation in read_operations:
                return True, None
            elif self.config.yolo_mode == "true":
                return True, None
            else:
                # Write operations blocked in read-only mode
                return False, (
                    f"Write operation '{operation}' not allowed in read-only YOLO mode. "
                    f"Only read operations are permitted for safety."
                )

        try:
            # Standard mode: Get model permissions from MCP
            permissions = self.get_model_permissions(model)

            # Check if model is enabled
            if not permissions.enabled:
                return False, f"Model '{model}' is not enabled for MCP access"

            # Check specific operation
            if not permissions.can_perform(operation):
                return False, f"Operation '{operation}' not allowed on model '{model}'"

            return True, None

        except AccessControlUnavailableError:
            # Infrastructure failure — propagate so callers report a
            # connection problem (retryable), not a permission denial
            raise
        except AccessControlError as e:
            logger.error(f"Access control check failed: {e}")
            return False, str(e)

    def validate_model_access(self, model: str, operation: str) -> None:
        """Validate model access, raising exception if denied.

        Args:
            model: The Odoo model name
            operation: The operation to perform

        Raises:
            AccessControlError: If access is denied
        """
        allowed, error_msg = self.check_operation_allowed(model, operation)
        if not allowed:
            raise AccessControlError(error_msg or f"Access denied to {model}.{operation}")

    def filter_enabled_models(self, models: List[str]) -> List[str]:
        """Filter list of models to only include enabled ones.

        Args:
            models: List of model names to filter

        Returns:
            List of enabled model names
        """
        # In YOLO mode, all models are enabled
        if self.config.is_yolo_enabled:
            logger.debug(f"YOLO mode: All {len(models)} models are accessible")
            return models  # Return all models unfiltered

        try:
            enabled_models = self.get_enabled_models()
            enabled_set = {m["model"] for m in enabled_models}
            return [m for m in models if m in enabled_set]
        except AccessControlError as e:
            logger.error(f"Failed to filter models: {e}")
            return []

    def get_all_permissions(self) -> Dict[str, ModelPermissions]:
        """Get permissions for all enabled models.

        Returns:
            Dict mapping model names to their permissions
        """
        permissions = {}

        try:
            enabled_models = self.get_enabled_models()

            for model_info in enabled_models:
                model = model_info["model"]
                try:
                    permissions[model] = self.get_model_permissions(model)
                except AccessControlError as e:
                    logger.warning(f"Failed to get permissions for {model}: {e}")

        except AccessControlError as e:
            logger.error(f"Failed to get all permissions: {e}")

        return permissions


def check_domain_balance(domain: List[Any], path: str = "domain") -> None:
    """Reject a domain whose prefix operators have no operands of their own.

    Odoo joins a flat sequence of expressions with implicit ANDs, which is why
    the ir.attachment scope is appended rather than prefixed with an explicit
    "&". That only holds while the caller's own domain is balanced: the
    trailing "|" in ``["|", ("id", ">", 0)]`` would otherwise take the appended
    scope as its second operand, ORing the allowlist away instead of ANDing it
    in. Such a payload is invalid on its own — Odoo rejects it — and becomes
    well-formed only once the scope is concatenated, so nothing downstream
    catches it.
    """
    expected = 1
    for index, token in enumerate(domain):
        if expected == 0:
            # Odoo prepends an implicit "&" for a flat sequence of terms.
            expected = 1
        if isinstance(token, (list, tuple)):
            expected -= 1
        elif token == "!":
            # Unary: consumes one expression and yields one.
            continue
        elif token in ("&", "|"):
            expected += 1
        else:
            raise ValidationError(
                f"Invalid domain term {token!r} at {path}[{index}]: expected a "
                f"condition like ['field', '=', value] or one of '&', '|', '!'"
            )
    if domain and expected:
        raise ValidationError(
            f"Unbalanced {path}: its '&'/'|'/'!' operators need {expected} more condition(s)"
        )


def attachment_scope_domain(
    config: OdooConfig, access_controller: "AccessController"
) -> Optional[List[Any]]:
    """Domain restricting ir.attachment rows to MCP-accessible res_models.

    An attachment row exposes more than a payload: `res_model`, `url` and
    `index_content` (the extracted document TEXT). Gating only the binary
    readers would leave the allowlist sidestep open for metadata, so searches
    and reads of ir.attachment are scoped here instead of post-filtering rows
    — a domain keeps `search_count` and the pagination math consistent with
    what is actually returned.

    Lives here rather than beside its callers: the tool and resource handlers
    both need it, and an allowlist-derived domain belongs with the allowlist.

    Scoped to models the caller may READ, not merely ones that are enabled:
    the two are separate endpoints, and an enabled-but-unreadable model whose
    attachments were admitted here would sidestep `validate_model_access`.

    Fails CLOSED, like every other gate on this path: an unreadable allowlist
    propagates as AccessControlError so the caller sees a retryable "could not
    verify access" instead of an unscoped result set. Swallowing the error
    would silently disable the scope on every surface at once, which is the
    one outcome a security control must not have. Returns None only when
    scoping genuinely does not apply — YOLO mode allows every model.

    Raises:
        AccessControlError: If the enabled-model listing or a per-model
            read permission cannot be retrieved.
    """
    if config.is_yolo_enabled:
        return None
    enabled = access_controller.get_enabled_models()
    names = []
    for entry in enabled:
        model = entry.get("model")
        if not model:
            continue
        # Enablement and READ permission are different endpoints (/mcp/models
        # vs /mcp/models/{model}/access), so an enabled model may still be
        # unreadable — and admitting it here would expose exactly the
        # attachment metadata the gate exists to withhold.
        operations = entry.get("operations") or {}
        if operations:
            # Newer MCP modules ship the flag in the listing itself — free.
            if operations.get("read"):
                names.append(model)
            continue
        # Older modules return only {model, name}. Neither default is safe:
        # True admits attachments the caller cannot read, False hides ones it
        # can — so resolve it. The per-model cache is shared with list_models,
        # so this is usually already warm; an unresolvable permission raises
        # AccessControlError and fails closed like the listing above.
        if access_controller.get_model_permissions(model).can_read:
            names.append(model)
    if not names:
        # Standard mode with nothing enabled: only standalone attachments can
        # qualify. Contradictory state (the ir.attachment gate already passed),
        # but the fail-closed reading is the safe one.
        return [("res_model", "=", False)]
    # Standalone attachments (no res_model) stay governed by the
    # ir.attachment gate alone, exactly as the payload readers treat them.
    return ["|", ("res_model", "=", False), ("res_model", "in", names)]
