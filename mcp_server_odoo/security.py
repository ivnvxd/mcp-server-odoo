"""Security policy for Odoo MCP Server.

Config-driven safety layer that enforces model allowlists, field filtering,
mutation controls, and record limits. Runs BEFORE the Odoo-level AccessController.
"""

import hashlib
import hmac
import logging
import time
from typing import Any, Dict, List, Optional, Set

from .config import OdooConfig

logger = logging.getLogger(__name__)


class SecurityPolicyError(Exception):
    """Raised when a security policy check fails."""

    pass


# Fields that must never be stripped by field filtering
PROTECTED_FIELDS: Set[str] = {"id", "display_name"}

# Fields that should never be writable via MCP
READONLY_SYSTEM_FIELDS: Set[str] = {
    "id",
    "create_date",
    "create_uid",
    "write_date",
    "write_uid",
    "__last_update",
}

# Default read operations
DEFAULT_READ_OPERATIONS: Set[str] = {"search_read", "read", "fields_get", "search_count"}

# Default write operations
DEFAULT_WRITE_OPERATIONS: Set[str] = {"create", "write"}


class SecurityPolicy:
    """Config-driven safety layer for Odoo operations.

    Enforces:
    - Model allowlists
    - Per-model operation restrictions
    - Field allowlists and denylists
    - Record count limits
    """

    def __init__(self, config: OdooConfig):
        self.config = config
        self._allowed_models: Optional[Set[str]] = (
            set(config.allowed_models) if config.allowed_models else None
        )
        self._allowed_read_ops: Set[str] = (
            set(config.allowed_read_operations)
            if config.allowed_read_operations
            else DEFAULT_READ_OPERATIONS
        )
        self._allowed_write_ops: Set[str] = (
            set(config.allowed_write_operations)
            if config.allowed_write_operations
            else DEFAULT_WRITE_OPERATIONS
        )
        self._model_op_map: Optional[Dict[str, Set[str]]] = None
        if config.model_operation_map:
            self._model_op_map = {
                model: set(ops) for model, ops in config.model_operation_map.items()
            }

        logger.info(
            "SecurityPolicy initialized: allowed_models=%s, mutations=%s, deletes=%s",
            len(self._allowed_models) if self._allowed_models else "all",
            config.enable_mutations,
            config.enable_deletes,
        )

    def check_model_allowed(self, model: str) -> None:
        """Check if a model is in the allowlist.

        Raises SecurityPolicyError if the model is not allowed.
        When no allowlist is configured (allowed_models is None), all models pass.
        """
        if self.config.admin_mode:
            return
        if self._allowed_models is not None and model not in self._allowed_models:
            raise SecurityPolicyError(
                f"Model '{model}' is not in the allowed models list. "
                f"Allowed models: {', '.join(sorted(self._allowed_models))}"
            )

    def is_model_allowed(self, model: str) -> bool:
        if self.config.admin_mode:
            return True
        if self._allowed_models is None:
            return True
        return model in self._allowed_models

    def check_operation_allowed(self, model: str, operation: str) -> None:
        """Check if an operation is allowed on a model.

        Checks in order:
        1. Per-model operation map (most specific)
        2. Global read/write operation lists
        """
        if self.config.admin_mode:
            return

        # Check per-model map first (most specific)
        if self._model_op_map and model in self._model_op_map:
            if operation not in self._model_op_map[model]:
                raise SecurityPolicyError(
                    f"Operation '{operation}' is not allowed on model '{model}'. "
                    f"Allowed: {', '.join(sorted(self._model_op_map[model]))}"
                )
            return

        # Check global operation lists
        all_allowed = self._allowed_read_ops | self._allowed_write_ops
        if operation not in all_allowed:
            raise SecurityPolicyError(
                f"Operation '{operation}' is not in the allowed operations list."
            )

    def _get_field_lists(self, model: str):
        """Get field allowlist and denylist for a model.

        Looks up by both dotted (res.partner) and underscored (res_partner) keys.
        """
        model_key = model.replace(".", "_")
        allowlist = self.config.field_allowlists.get(model) or self.config.field_allowlists.get(
            model_key
        )
        denylist = self.config.field_denylists.get(model) or self.config.field_denylists.get(
            model_key
        )
        return allowlist, denylist

    def filter_read_fields(self, model: str, fields: Optional[List[str]]) -> Optional[List[str]]:
        """Filter fields for read operations based on allowlist/denylist.

        Returns filtered field list, or None if no filtering needed.
        Protected fields (id, display_name) are never removed.
        """
        if self.config.admin_mode:
            return fields

        allowlist, denylist = self._get_field_lists(model)

        if not allowlist and not denylist:
            return fields

        if fields is None:
            # If no specific fields requested and we have an allowlist, use it
            if allowlist:
                return list(set(allowlist) | PROTECTED_FIELDS)
            return None

        result = []
        for f in fields:
            if f in PROTECTED_FIELDS:
                result.append(f)
                continue
            if allowlist and f not in allowlist:
                continue
            if denylist and f in denylist:
                continue
            result.append(f)

        return result

    def filter_write_fields(self, model: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Filter field values for write operations.

        Removes:
        - System readonly fields
        - Fields not in allowlist (if configured)
        - Fields in denylist (if configured)
        """
        if self.config.admin_mode:
            return values

        allowlist, denylist = self._get_field_lists(model)

        result = {}
        stripped = []
        for field_name, value in values.items():
            if field_name in READONLY_SYSTEM_FIELDS:
                stripped.append(field_name)
                continue
            if allowlist and field_name not in allowlist:
                stripped.append(field_name)
                continue
            if denylist and field_name in denylist:
                stripped.append(field_name)
                continue
            result[field_name] = value

        if stripped:
            logger.warning(
                "Stripped disallowed fields from write to %s: %s",
                model,
                ", ".join(stripped),
            )

        return result

    def clamp_limit(self, limit: Optional[int]) -> int:
        """Clamp a query limit to the configured maximum."""
        if limit is None or limit <= 0:
            return min(self.config.default_limit, self.config.max_records_per_query)
        return min(limit, self.config.max_records_per_query)

    def clamp_batch_size(self, size: int) -> int:
        """Clamp a batch size to the configured maximum."""
        return min(max(size, 1), self.config.max_batch_size)

    def get_allowed_models_list(self) -> Optional[List[str]]:
        """Return the allowed models list, or None if unrestricted."""
        if self._allowed_models is None:
            return None
        return sorted(self._allowed_models)


class MutationPolicy:
    """Controls whether write operations are permitted.

    Enforces:
    - Global mutation enable/disable
    - Global delete enable/disable
    - Per-model write restrictions via SecurityPolicy
    """

    def __init__(self, config: OdooConfig):
        self.config = config

    def check_mutation_allowed(self, operation: str, model: str) -> None:
        """Check if a mutation (create/write) is allowed.

        Raises SecurityPolicyError if mutations are disabled.
        """
        if self.config.admin_mode:
            return
        if not self.config.enable_mutations:
            raise SecurityPolicyError(
                f"Write operation '{operation}' on '{model}' is blocked. "
                "Mutations are disabled by default. "
                "Set ODOO_ENABLE_MUTATIONS=true to allow write operations."
            )

    def check_delete_allowed(self, model: str) -> None:
        """Check if delete operations are allowed.

        Raises SecurityPolicyError if deletes are disabled.
        """
        if self.config.admin_mode:
            return
        if not self.config.enable_mutations:
            raise SecurityPolicyError(
                f"Delete on '{model}' is blocked. "
                "Mutations are disabled. Set ODOO_ENABLE_MUTATIONS=true first."
            )
        if not self.config.enable_deletes:
            raise SecurityPolicyError(
                f"Delete on '{model}' is blocked. "
                "Deletes are disabled by default. "
                "Set ODOO_ENABLE_DELETES=true to allow delete operations."
            )

    @property
    def mutations_enabled(self) -> bool:
        return self.config.enable_mutations or self.config.admin_mode

    @property
    def deletes_enabled(self) -> bool:
        return self.config.enable_deletes or self.config.admin_mode


class ConfirmationManager:
    """Manages confirmation tokens for mutation operations.

    When require_confirmation_for_mutations is enabled, write operations
    must provide a valid confirmation token to proceed.
    """

    TOKEN_TTL = 120  # seconds

    def __init__(self, config: OdooConfig, secret: Optional[str] = None):
        self.config = config
        self._secret = (secret or "mcp-odoo-confirm").encode()
        self._pending: Dict[str, float] = {}  # token -> expiry timestamp

    @property
    def requires_confirmation(self) -> bool:
        return self.config.require_confirmation_for_mutations and self.config.enable_mutations

    def generate_token(self, operation: str, model: str, summary: str) -> str:
        """Generate a confirmation token for a pending mutation."""
        payload = f"{operation}:{model}:{summary}:{time.time()}"
        token = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()[:32]
        self._pending[token] = time.time() + self.TOKEN_TTL
        self._cleanup_expired()
        return token

    def validate_token(self, token: str) -> bool:
        """Validate and consume a confirmation token."""
        self._cleanup_expired()
        if token in self._pending:
            del self._pending[token]
            return True
        return False

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [t for t, exp in self._pending.items() if exp < now]
        for t in expired:
            del self._pending[t]
