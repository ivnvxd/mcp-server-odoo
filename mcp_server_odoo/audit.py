"""Structured audit logging for MCP tool calls.

Emits JSON-structured audit entries for every tool invocation,
capturing who did what, on which model, with what result.
"""

import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

from .config import OdooConfig

# Dedicated audit logger — separate from application logs
audit_logger = logging.getLogger("odoo.mcp.audit")

# Fields whose values should never appear in audit logs
SENSITIVE_FIELDS = {
    "password",
    "password_crypt",
    "oauth_access_token",
    "oauth_token_secret",
    "secret",
    "token",
    "api_key",
    "credit_card",
    "pin",
}


@dataclass
class AuditEntry:
    """A single auditable event."""

    timestamp: str
    correlation_id: str
    subject: str
    auth_mode: str
    tool_name: str
    model: Optional[str]
    operation: str
    record_ids: Optional[List[int]]
    success: bool
    duration_ms: float
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _mask_sensitive(data: Dict[str, Any]) -> Dict[str, Any]:
    """Mask values of sensitive fields in a dictionary."""
    if not data:
        return data
    masked = {}
    for key, value in data.items():
        if key.lower() in SENSITIVE_FIELDS:
            masked[key] = "***MASKED***"
        elif isinstance(value, dict):
            masked[key] = _mask_sensitive(value)
        else:
            masked[key] = value
    return masked


class AuditLogger:
    """Structured audit logger for tool calls."""

    def __init__(self, config: OdooConfig):
        self.enabled = config.audit_log_enabled
        if self.enabled:
            # Ensure audit logger has at least a handler
            if not audit_logger.handlers and not audit_logger.parent.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(message)s"))
                audit_logger.addHandler(handler)
            audit_logger.setLevel(logging.INFO)

    def log_tool_call(self, entry: AuditEntry) -> None:
        """Emit a structured audit log entry."""
        if not self.enabled:
            return

        record = asdict(entry)
        # Mask any sensitive data in extra
        if record.get("extra"):
            record["extra"] = _mask_sensitive(record["extra"])

        audit_logger.info(json.dumps(record, default=str))

    @contextmanager
    def track_tool_call(
        self,
        tool_name: str,
        subject: str = "unknown",
        auth_mode: str = "none",
        model: Optional[str] = None,
        operation: str = "",
        record_ids: Optional[List[int]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Generator[AuditEntry, None, None]:
        """Context manager that tracks a tool call and logs on exit.

        Usage:
            with audit.track_tool_call("search_records", ...) as entry:
                result = do_work()
                entry.record_ids = [1, 2, 3]
        """
        correlation_id = uuid.uuid4().hex[:16]
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            correlation_id=correlation_id,
            subject=subject,
            auth_mode=auth_mode,
            tool_name=tool_name,
            model=model,
            operation=operation,
            record_ids=record_ids,
            success=True,
            duration_ms=0,
            extra=extra or {},
        )

        start = time.monotonic()
        try:
            yield entry
        except Exception as e:
            entry.success = False
            entry.error = str(e)
            raise
        finally:
            entry.duration_ms = round((time.monotonic() - start) * 1000, 2)
            self.log_tool_call(entry)
