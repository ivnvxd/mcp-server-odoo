"""Tests for export_utils module."""

import csv
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.export_utils import (
    MAX_PREVIEW_LINES,
    _ExportFileWriteError,
    _hash_domain,
    _write_csv_atomic,
    execute_export,
    generate_export_filename,
)
from mcp_server_odoo.schemas import (
    ExportAccessDeniedError,
    ExportBlockedExceedsLimitError,
    ExportSuccessResult,
)


@pytest.fixture
def mock_config(tmp_path):
    """Config with temp dir for tests."""
    return OdooConfig(
        url="http://test",
        database="test",
        username="admin",
        password="x",
        api_key=None,
        _export_dir=tmp_path,
    )


@pytest.fixture
def mock_connection():
    """Mock OdooConnection with default 5 records."""
    conn = MagicMock()
    conn.execute_kw.return_value = 5
    return conn


@pytest.fixture
def mock_access():
    """Mock access controller that allows all access."""
    ctrl = MagicMock()
    ctrl.validate_model_access = MagicMock(return_value=None)
    return ctrl


class TestGenerateExportFilename:
    """Tests for generate_export_filename."""

    def test_filename_includes_model_and_uuid(self):
        """Filename matches expected pattern."""
        filename = generate_export_filename("res.partner")
        assert filename.startswith("odoo_export_res_partner_")
        assert filename.endswith(".csv")
        # Pattern: odoo_export_{model}_{timestamp}_{uuid8}.csv
        parts = filename.replace(".csv", "").split("_")
        assert len(parts) == 6
        assert parts[0] == "odoo"
        assert parts[1] == "export"
        assert parts[2] == "res"
        assert parts[3] == "partner"

    def test_model_dots_replaced(self):
        """Dots in model name are replaced with underscores."""
        filename = generate_export_filename("res.partner")
        assert "res.partner" not in filename
        assert "res_partner" in filename

    def test_model_slashes_replaced(self):
        """Slashes in model name are replaced with underscores."""
        filename = generate_export_filename("stock/layer")
        assert "stock/layer" not in filename
        assert "stock_layer" in filename

    def test_concurrent_filenames_unique(self):
        """Two calls produce different filenames due to UUID."""
        filename1 = generate_export_filename("res.partner")
        filename2 = generate_export_filename("res.partner")
        assert filename1 != filename2


class TestHashDomain:
    """Tests for _hash_domain."""

    def test_same_domain_same_hash(self):
        """Identical domains produce same hash."""
        domain = [["active", "=", True]]
        h1 = _hash_domain(domain)
        h2 = _hash_domain(domain)
        assert h1 == h2

    def test_different_domain_different_hash(self):
        """Different domains produce different hashes."""
        domain1 = [["active", "=", True]]
        domain2 = [["active", "=", False]]
        h1 = _hash_domain(domain1)
        h2 = _hash_domain(domain2)
        assert h1 != h2

    def test_hash_length_16(self):
        """Hash is exactly 16 characters."""
        domain = [["active", "=", True]]
        h = _hash_domain(domain)
        assert len(h) == 16

    def test_hash_is_hex(self):
        """Hash contains only hexadecimal characters."""
        domain = [["active", "=", True]]
        h = _hash_domain(domain)
        assert all(c in "0123456789abcdef" for c in h)

    def test_empty_domain(self):
        """Empty domain produces a hash."""
        h = _hash_domain([])
        assert len(h) == 16

    def test_none_domain(self):
        """None domain is handled gracefully."""
        h = _hash_domain(None)
        assert len(h) == 16


class TestWriteCsvAtomic:
    """Tests for _write_csv_atomic."""

    def test_writes_csv_with_header_and_data(self, tmp_path):
        """CSV file has header row and data rows."""
        target = tmp_path / "export.csv"
        headers = ["id", "name"]
        records = iter([["1", "Alice"], ["2", "Bob"]])

        row_count, preview = _write_csv_atomic(target, headers, records)

        assert row_count == 2
        assert target.exists()
        with open(target, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
        assert len(rows) == 3  # header + 2 data
        assert rows[0] == ["id", "name"]
        assert rows[1] == ["1", "Alice"]
        assert rows[2] == ["2", "Bob"]

    def test_empty_result_writes_header_only(self, tmp_path):
        """Empty record iterator writes header only."""
        target = tmp_path / "export.csv"
        headers = ["id", "name"]
        records = iter([])

        row_count, preview = _write_csv_atomic(target, headers, records)

        assert row_count == 0
        assert target.exists()
        with open(target, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
        assert len(rows) == 1  # header only
        assert rows[0] == ["id", "name"]

    def test_atomic_write_creates_temp_file(self, tmp_path):
        """Temp file is renamed to target on success."""
        target = tmp_path / "export.csv"
        headers = ["id"]
        records = iter([["1"]])

        _write_csv_atomic(target, headers, records)

        # No temp file left behind
        temp_file = target.with_suffix(target.suffix + ".tmp")
        assert not temp_file.exists()
        assert target.exists()

    def test_atomic_write_failure_cleans_up(self, tmp_path):
        """Failed write removes temp file."""
        target = tmp_path / "export.csv"
        headers = ["id"]
        records = iter([["1"]])

        with patch("mcp_server_odoo.export_utils.open", side_effect=OSError("disk error")):
            with pytest.raises(_ExportFileWriteError):
                _write_csv_atomic(target, headers, records)

        temp_file = target.with_suffix(target.suffix + ".tmp")
        assert not temp_file.exists()
        assert not target.exists()

    def test_returns_preview_max_10_lines(self, tmp_path):
        """Preview is capped at 10 lines."""
        target = tmp_path / "export.csv"
        headers = ["id"]
        # More than 10 rows
        records = iter([[str(i)] for i in range(20)])

        row_count, preview = _write_csv_atomic(target, headers, records)

        assert row_count == 20
        assert len(preview) == 10  # 1 header + 9 data rows

    def test_csv_uses_utf8_bom(self, tmp_path):
        """File starts with UTF-8 BOM bytes."""
        target = tmp_path / "export.csv"
        headers = ["id"]
        records = iter([["1"]])

        _write_csv_atomic(target, headers, records)

        with open(target, "rb") as f:
            bom = f.read(3)
        assert bom == b"\xef\xbb\xbf"  # UTF-8 BOM

    def test_csv_rfc4180_quoting(self, tmp_path):
        """Comma and quote characters are properly quoted."""
        target = tmp_path / "export.csv"
        headers = ["id", "comment"]
        records = iter([["1", 'Hello, "World"']])

        _write_csv_atomic(target, headers, records)

        with open(target, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
        # The field with comma should be quoted
        assert rows[1] == ["1", 'Hello, "World"']

    def test_path_too_long_raises(self, tmp_path):
        """Long path raises _ExportFileWriteError."""
        # Create a path that exceeds MAX_PATH_LENGTH
        long_dir = tmp_path / ("a" * 300)
        target = long_dir / "export.csv"
        headers = ["id"]
        records = iter([["1"]])

        with pytest.raises(_ExportFileWriteError, match="too long"):
            _write_csv_atomic(target, headers, records)


class TestExecuteExport:
    """Tests for execute_export."""

    def test_export_writes_csv_with_header_and_data(
        self, mock_config, mock_connection, mock_access
    ):
        """Happy path: file has header and rows."""
        # search_count returns 5, search_read returns 5 records
        mock_connection.execute_kw.side_effect = [
            5,  # search_count
            [  # search_read batch 1
                {"id": 1, "display_name": "Record 1"},
                {"id": 2, "display_name": "Record 2"},
                {"id": 3, "display_name": "Record 3"},
                {"id": 4, "display_name": "Record 4"},
                {"id": 5, "display_name": "Record 5"},
            ],
        ]

        result = execute_export(
            model="res.partner",
            domain=[["active", "=", True]],
            fields=["id", "display_name"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportSuccessResult)
        assert result.row_count == 5
        assert result.success is True
        assert result.file_path.endswith(".csv")
        assert result.file_size_bytes > 0
        assert result.truncated is False
        assert len(result.preview) <= MAX_PREVIEW_LINES

    def test_export_respects_max_rows(self, mock_config, mock_connection, mock_access):
        """Config max_rows=100, search_count=50 succeeds."""
        mock_config.export_max_rows = 100
        mock_connection.execute_kw.side_effect = [
            50,  # search_count
            [{"id": i, "display_name": f"Record {i}"} for i in range(50)],
        ]

        result = execute_export(
            model="res.partner",
            domain=[],
            fields=None,  # should use DEFAULT_FIELDS
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportSuccessResult)
        assert result.row_count == 50

    def test_export_blocked_exceeds_limit_raises(self, mock_config, mock_connection, mock_access):
        """search_count > max_rows returns ExportBlockedExceedsLimitError."""
        mock_config.export_max_rows = 100
        mock_connection.execute_kw.side_effect = [
            250000,  # search_count exceeds limit
        ]

        result = execute_export(
            model="res.partner",
            domain=[["active", "=", True]],
            fields=["id"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportBlockedExceedsLimitError)
        assert result.matched_count == 250000
        assert result.max_rows_limit == 100
        assert "250000" in result.message
        assert "aggregate_records" in result.suggestion

    def test_export_at_exact_limit_succeeds(self, mock_config, mock_connection, mock_access):
        """search_count == max_rows succeeds."""
        mock_config.export_max_rows = 100
        mock_connection.execute_kw.side_effect = [
            100,  # search_count at exact limit
            [{"id": i, "display_name": f"Record {i}"} for i in range(100)],
        ]

        result = execute_export(
            model="res.partner",
            domain=[],
            fields=["id", "display_name"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportSuccessResult)
        assert result.row_count == 100

    def test_export_empty_result_writes_header_only(
        self, mock_config, mock_connection, mock_access
    ):
        """search_count=0, file has only header."""
        mock_connection.execute_kw.side_effect = [
            0,  # search_count
        ]

        result = execute_export(
            model="res.partner",
            domain=[["id", "=", 999999]],
            fields=["id", "display_name"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportSuccessResult)
        assert result.row_count == 0
        # Verify file exists with header
        file_path = Path(result.file_path)
        assert file_path.exists()
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
        assert len(rows) == 1  # header only
        assert rows[0] == ["id", "display_name"]

    def test_export_default_fields_applied_when_none(
        self, mock_config, mock_connection, mock_access
    ):
        """fields=None uses DEFAULT_FIELDS."""
        mock_connection.execute_kw.side_effect = [
            2,
            [{"id": 1, "display_name": "Rec 1"}, {"id": 2, "display_name": "Rec 2"}],
        ]

        result = execute_export(
            model="res.partner",
            domain=[],
            fields=None,
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportSuccessResult)
        # verify search_read was called with DEFAULT_FIELDS
        calls = mock_connection.execute_kw.call_args_list
        search_read_call = calls[1]  # second call is search_read
        # kwargs dict is 4th positional arg: calls[1][0][3]
        assert search_read_call[0][3]["fields"] == ["id", "display_name"]

    def test_export_custom_fields_respected(self, mock_config, mock_connection, mock_access):
        """fields=["id", "name"] uses those exact columns."""
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1, "name": "Alice"}],
        ]

        result = execute_export(
            model="res.partner",
            domain=[],
            fields=["id", "name"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportSuccessResult)
        calls = mock_connection.execute_kw.call_args_list
        search_read_call = calls[1]
        # kwargs dict is 4th positional arg: calls[1][0][3]
        assert search_read_call[0][3]["fields"] == ["id", "name"]

    def test_export_access_denied_raises(self, mock_config, mock_connection, mock_access):
        """access_controller raises, get ExportAccessDeniedError."""
        mock_access.validate_model_access = MagicMock(side_effect=Exception("Access denied"))

        result = execute_export(
            model="restricted.model",
            domain=[],
            fields=["id"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportAccessDeniedError)
        assert "not accessible" in result.message

    def test_export_search_count_called_first(self, mock_config, mock_connection, mock_access):
        """search_count is called before search_read."""
        mock_connection.execute_kw.side_effect = [
            2,
            [{"id": 1}, {"id": 2}],
        ]

        execute_export(
            model="res.partner",
            domain=[],
            fields=["id"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        calls = mock_connection.execute_kw.call_args_list
        assert calls[0][0][1] == "search_count"
        assert calls[1][0][1] == "search_read"

    def test_export_batches_correctly_with_offset(self, mock_config, mock_connection, mock_access):
        """1500 records, batch=500, 3 batches with offsets 0, 500, 1000."""
        mock_config.export_batch_size = 500
        mock_connection.execute_kw.side_effect = [
            1500,  # search_count
            # batch 1
            [{"id": i} for i in range(1, 501)],
            # batch 2
            [{"id": i} for i in range(501, 1001)],
            # batch 3
            [{"id": i} for i in range(1001, 1501)],
        ]

        result = execute_export(
            model="res.partner",
            domain=[],
            fields=["id"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportSuccessResult)
        assert result.row_count == 1500
        # Verify 3 search_read calls with correct offsets
        calls = mock_connection.execute_kw.call_args_list
        assert len(calls) == 4  # 1 search_count + 3 search_read
        # kwargs dict is 4th positional arg: calls[n][0][3]
        assert calls[1][0][3]["offset"] == 0
        assert calls[1][0][3]["limit"] == 500
        assert calls[2][0][3]["offset"] == 500
        assert calls[2][0][3]["limit"] == 500
        assert calls[3][0][3]["offset"] == 1000
        assert calls[3][0][3]["limit"] == 500

    def test_export_calls_audit_log(self, mock_config, mock_connection, mock_access, caplog):
        """Audit log is called after successful export."""
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1, "display_name": "Test"}],
        ]

        with caplog.at_level(logging.INFO, logger="mcp_server_odoo.export"):
            execute_export(
                model="res.partner",
                domain=[],
                fields=["id", "display_name"],
                config=mock_config,
                odoo_connection=mock_connection,
                access_controller=mock_access,
            )

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "export |" in record.message
        assert "model=res.partner" in record.message
        assert "rows=1" in record.message

    def test_export_audit_log_does_not_contain_full_domain(
        self, mock_config, mock_connection, mock_access, caplog
    ):
        """Domain is not in log output, hash IS."""
        domain = [["email", "=", "secret@example.com"]]
        mock_connection.execute_kw.side_effect = [
            1,  # search_count
            [{"id": 1, "display_name": "Test"}],  # search_read
        ]

        with caplog.at_level(logging.INFO, logger="mcp_server_odoo.export"):
            execute_export(
                model="res.partner",
                domain=domain,
                fields=["id"],
                config=mock_config,
                odoo_connection=mock_connection,
                access_controller=mock_access,
            )

        log_text = caplog.text
        # Domain values should NOT appear
        assert "secret@example.com" not in log_text
        # But domain_hash should appear
        assert "domain_hash=" in log_text

    def test_export_uses_config_export_dir(self, mock_config, mock_connection, mock_access):
        """File is created in config.export_dir."""
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1}],
        ]

        result = execute_export(
            model="res.partner",
            domain=[],
            fields=["id"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        file_path = Path(result.file_path)
        assert file_path.parent == mock_config.export_dir

    def test_export_search_count_failure_returns_access_denied(
        self, mock_config, mock_connection, mock_access
    ):
        """search_count raises, returns ExportAccessDeniedError."""
        mock_connection.execute_kw.side_effect = Exception("XML-RPC error")

        result = execute_export(
            model="res.partner",
            domain=[],
            fields=["id"],
            config=mock_config,
            odoo_connection=mock_connection,
            access_controller=mock_access,
        )

        assert isinstance(result, ExportAccessDeniedError)
        assert "not accessible or does not exist" in result.message
