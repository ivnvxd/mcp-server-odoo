"""Tests for export schemas."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_server_odoo.schemas import (
    ExportAccessDeniedError,
    ExportBlockedExceedsLimitError,
    ExportConfig,
    ExportDisabledError,
    ExportFileError,
    ExportResult,
    ExportSuccessResult,
)


class TestExportConfig:
    """Test ExportConfig schema."""

    def test_export_config_defaults(self):
        """Test ExportConfig with default values."""
        config = ExportConfig(export_dir=Path("/tmp/exports"))
        assert config.export_dir == Path("/tmp/exports")
        assert config.max_rows == 10000
        assert config.batch_size == 500
        assert config.format == "csv"

    def test_export_config_custom_values(self):
        """Test ExportConfig with custom values."""
        config = ExportConfig(
            export_dir=Path("/custom/exports"),
            max_rows=5000,
            batch_size=200,
        )
        assert config.max_rows == 5000
        assert config.batch_size == 200

    def test_export_config_max_rows_validation(self):
        """Test max_rows must be > 0."""
        with pytest.raises(ValidationError):
            ExportConfig(export_dir=Path("/tmp"), max_rows=0)

    def test_export_config_batch_size_validation(self):
        """Test batch_size must be > 0."""
        with pytest.raises(ValidationError):
            ExportConfig(export_dir=Path("/tmp"), batch_size=0)


class TestExportSuccessResult:
    """Test ExportSuccessResult schema."""

    def test_export_success_result_serializes_to_json(self):
        """Test ExportSuccessResult serializes to JSON correctly."""
        result = ExportSuccessResult(
            file_path="/tmp/exports/odoo_export_res_partner_20260614T153022.csv",
            file_size_bytes=1248392,
            row_count=8421,
            truncated=False,
            max_rows_limit=10000,
            preview=["id,name,email", "1,Acme Corp,info@acme.com"],
            duration_ms=3420,
            exported_at="2026-06-14T15:30:25.123Z",
        )
        json_str = result.model_dump_json()
        data = json.loads(json_str)
        assert data["success"] is True
        assert data["file_path"] == "/tmp/exports/odoo_export_res_partner_20260614T153022.csv"
        assert data["row_count"] == 8421
        assert data["truncated"] is False

    def test_export_success_result_preview_max_10_lines(self):
        """Test preview is limited to 10 lines."""
        preview_with_10 = ["line" + str(i) for i in range(10)]
        result = ExportSuccessResult(
            file_path="/tmp/test.csv",
            file_size_bytes=100,
            row_count=100,
            truncated=False,
            max_rows_limit=10000,
            preview=preview_with_10,
            duration_ms=100,
            exported_at="2026-06-14T15:30:25.123Z",
        )
        assert len(result.preview) == 10

    def test_export_success_result_preview_rejects_more_than_10(self):
        """Test preview with more than 10 lines is rejected."""
        preview_with_11 = ["line" + str(i) for i in range(11)]
        with pytest.raises(ValidationError):
            ExportSuccessResult(
                file_path="/tmp/test.csv",
                file_size_bytes=100,
                row_count=100,
                truncated=False,
                max_rows_limit=10000,
                preview=preview_with_11,
                duration_ms=100,
                exported_at="2026-06-14T15:30:25.123Z",
            )


class TestExportBlockedError:
    """Test ExportBlockedExceedsLimitError schema."""

    def test_export_blocked_error_schema(self):
        """Test ExportBlockedExceedsLimitError has correct fields."""
        error = ExportBlockedExceedsLimitError(
            message="Domain matches 250,000 records, exceeds max_rows=10,000.",
            matched_count=250000,
            max_rows_limit=10000,
            suggestion="Add filters to your domain or use aggregate_records().",
        )
        data = error.model_dump()
        assert data["success"] is False
        assert data["error"] == "export_blocked_exceeds_limit"
        assert data["matched_count"] == 250000
        assert data["max_rows_limit"] == 10000

    def test_export_blocked_error_serializes_to_json(self):
        """Test ExportBlockedExceedsLimitError serializes to JSON."""
        error = ExportBlockedExceedsLimitError(
            message="Too many records",
            matched_count=50000,
            max_rows_limit=10000,
            suggestion="Refine your domain",
        )
        json_str = error.model_dump_json()
        data = json.loads(json_str)
        assert data["error"] == "export_blocked_exceeds_limit"


class TestExportAccessDeniedError:
    """Test ExportAccessDeniedError schema."""

    def test_export_access_denied_error_schema(self):
        """Test ExportAccessDeniedError has correct fields."""
        error = ExportAccessDeniedError(
            message="Model 'res_users_password' is not accessible.",
        )
        data = error.model_dump()
        assert data["success"] is False
        assert data["error"] == "access_denied"
        assert "not accessible" in data["message"]

    def test_export_access_denied_error_serializes_to_json(self):
        """Test ExportAccessDeniedError serializes to JSON."""
        error = ExportAccessDeniedError(message="Access denied")
        json_str = error.model_dump_json()
        data = json.loads(json_str)
        assert data["error"] == "access_denied"


class TestExportDisabledError:
    """Test ExportDisabledError schema."""

    def test_export_disabled_error_schema(self):
        """Test ExportDisabledError has correct fields."""
        error = ExportDisabledError(message="Export tool is disabled")
        data = error.model_dump()
        assert data["success"] is False
        assert data["error"] == "export_disabled"

    def test_export_disabled_error_serializes_to_json(self):
        """Test ExportDisabledError serializes to JSON."""
        error = ExportDisabledError(message="Export is disabled")
        json_str = error.model_dump_json()
        data = json.loads(json_str)
        assert data["error"] == "export_disabled"


class TestExportFileError:
    """Test ExportFileError schema."""

    def test_export_file_error_schema(self):
        """Test ExportFileError has correct fields."""
        error = ExportFileError(message="Disk full")
        data = error.model_dump()
        assert data["success"] is False
        assert data["error"] == "file_write_error"
        assert data["message"] == "Disk full"

    def test_export_file_error_serializes_to_json(self):
        """Test ExportFileError serializes to JSON."""
        error = ExportFileError(message="Permission denied")
        json_str = error.model_dump_json()
        data = json.loads(json_str)
        assert data["error"] == "file_write_error"


class TestExportResultUnion:
    """Test ExportResult union type."""

    def test_export_result_union_serializes_each_variant(self):
        """Test each ExportResult variant serializes correctly."""
        variants = [
            ExportSuccessResult(
                file_path="/tmp/test.csv",
                file_size_bytes=100,
                row_count=10,
                truncated=False,
                max_rows_limit=10000,
                preview=["id,name"],
                duration_ms=100,
                exported_at="2026-06-14T15:30:25.123Z",
            ),
            ExportBlockedExceedsLimitError(
                message="Too many",
                matched_count=50000,
                max_rows_limit=10000,
                suggestion="Filter more",
            ),
            ExportAccessDeniedError(message="Denied"),
            ExportDisabledError(message="Disabled"),
            ExportFileError(message="Error"),
        ]
        for variant in variants:
            json_str = variant.model_dump_json()
            data = json.loads(json_str)
            assert "success" in data
            assert "error" in data or data.get("success") is True

    def test_export_result_union_type_check(self):
        """Test that all variants are instances of ExportResult."""
        success = ExportSuccessResult(
            file_path="/tmp/test.csv",
            file_size_bytes=100,
            row_count=10,
            truncated=False,
            max_rows_limit=10000,
            preview=["id,name"],
            duration_ms=100,
            exported_at="2026-06-14T15:30:25.123Z",
        )
        blocked = ExportBlockedExceedsLimitError(
            message="Too many",
            matched_count=50000,
            max_rows_limit=10000,
            suggestion="Filter more",
        )
        denied = ExportAccessDeniedError(message="Denied")
        disabled = ExportDisabledError(message="Disabled")
        file_error = ExportFileError(message="Error")

        assert isinstance(success, ExportResult)
        assert isinstance(blocked, ExportResult)
        assert isinstance(denied, ExportResult)
        assert isinstance(disabled, ExportResult)
        assert isinstance(file_error, ExportResult)
