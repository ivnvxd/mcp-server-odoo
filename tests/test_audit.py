"""Tests for audit logging."""

import json
import logging

import pytest

from mcp_server_odoo.audit import AuditEntry, AuditLogger, _mask_sensitive
from mcp_server_odoo.config import OdooConfig


def _make_config(**overrides) -> OdooConfig:
    defaults = {
        "url": "http://localhost:8069",
        "username": "admin",
        "password": "admin",
        "yolo_mode": "read",
    }
    defaults.update(overrides)
    return OdooConfig(**defaults)


class TestMaskSensitive:
    def test_masks_password(self):
        data = {"name": "test", "password": "secret123"}
        result = _mask_sensitive(data)
        assert result["name"] == "test"
        assert result["password"] == "***MASKED***"

    def test_masks_nested(self):
        data = {"user": {"name": "test", "api_key": "abc123"}}
        result = _mask_sensitive(data)
        assert result["user"]["api_key"] == "***MASKED***"

    def test_empty_dict(self):
        assert _mask_sensitive({}) == {}

    def test_no_sensitive_fields(self):
        data = {"name": "test", "email": "test@test.com"}
        result = _mask_sensitive(data)
        assert result == data


class TestAuditLogger:
    def test_disabled_is_noop(self):
        config = _make_config(audit_log_enabled=False)
        logger = AuditLogger(config)
        entry = AuditEntry(
            timestamp="2024-01-01T00:00:00Z",
            correlation_id="abc123",
            subject="test",
            auth_mode="none",
            tool_name="search_records",
            model="res.partner",
            operation="read",
            record_ids=None,
            success=True,
            duration_ms=10.0,
        )
        # Should not raise
        logger.log_tool_call(entry)

    def test_enabled_logs_entry(self, caplog):
        config = _make_config(audit_log_enabled=True)
        logger = AuditLogger(config)
        entry = AuditEntry(
            timestamp="2024-01-01T00:00:00Z",
            correlation_id="abc123",
            subject="test-user",
            auth_mode="api_key",
            tool_name="search_records",
            model="res.partner",
            operation="read",
            record_ids=[1, 2, 3],
            success=True,
            duration_ms=42.5,
        )

        with caplog.at_level(logging.INFO, logger="odoo.mcp.audit"):
            logger.log_tool_call(entry)

        assert len(caplog.records) >= 1
        log_data = json.loads(caplog.records[-1].message)
        assert log_data["tool_name"] == "search_records"
        assert log_data["subject"] == "test-user"
        assert log_data["model"] == "res.partner"
        assert log_data["success"] is True

    def test_masks_sensitive_extra(self, caplog):
        config = _make_config(audit_log_enabled=True)
        logger = AuditLogger(config)
        entry = AuditEntry(
            timestamp="2024-01-01T00:00:00Z",
            correlation_id="abc123",
            subject="test",
            auth_mode="none",
            tool_name="create_record",
            model="res.partner",
            operation="create",
            record_ids=None,
            success=True,
            duration_ms=10.0,
            extra={"password": "secret123", "name": "test"},
        )

        with caplog.at_level(logging.INFO, logger="odoo.mcp.audit"):
            logger.log_tool_call(entry)

        log_data = json.loads(caplog.records[-1].message)
        assert log_data["extra"]["password"] == "***MASKED***"
        assert log_data["extra"]["name"] == "test"


class TestTrackToolCall:
    def test_success_tracking(self, caplog):
        config = _make_config(audit_log_enabled=True)
        audit = AuditLogger(config)

        with caplog.at_level(logging.INFO, logger="odoo.mcp.audit"):
            with audit.track_tool_call(
                "search_records",
                "user1",
                "api_key",
                model="res.partner",
                operation="read",
            ):
                pass  # simulate work

        assert len(caplog.records) >= 1
        log_data = json.loads(caplog.records[-1].message)
        assert log_data["success"] is True
        assert log_data["duration_ms"] >= 0

    def test_failure_tracking(self, caplog):
        config = _make_config(audit_log_enabled=True)
        audit = AuditLogger(config)

        with caplog.at_level(logging.INFO, logger="odoo.mcp.audit"):
            with pytest.raises(ValueError, match="test error"):
                with audit.track_tool_call(
                    "create_record",
                    "user1",
                    "api_key",
                    model="res.partner",
                    operation="create",
                ):
                    raise ValueError("test error")

        log_data = json.loads(caplog.records[-1].message)
        assert log_data["success"] is False
        assert "test error" in log_data["error"]

    def test_correlation_id_generated(self, caplog):
        config = _make_config(audit_log_enabled=True)
        audit = AuditLogger(config)

        with caplog.at_level(logging.INFO, logger="odoo.mcp.audit"):
            with audit.track_tool_call("test", "user1", "none") as entry:
                assert len(entry.correlation_id) == 16
