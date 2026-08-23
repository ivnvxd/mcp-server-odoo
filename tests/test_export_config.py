"""Tests for export configuration in OdooConfig."""

import tempfile
from pathlib import Path

import pytest

from mcp_server_odoo.config import load_config, reset_config


@pytest.fixture(autouse=True)
def reset_config_fixture():
    """Reset configuration before each test."""
    reset_config()
    yield
    reset_config()


class TestExportConfig:
    """Test export-related configuration fields."""

    def test_export_enabled_default_true(self, monkeypatch):
        """Test export_enabled defaults to True."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        config = load_config()
        assert config.export_enabled is True
        assert config.is_export_allowed is True

    def test_export_enabled_false_via_env(self, monkeypatch):
        """Test export_enabled can be disabled via env var."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_EXPORT_ENABLED", "false")
        config = load_config()
        assert config.export_enabled is False
        assert config.is_export_allowed is False

    def test_export_max_rows_default_10000(self, monkeypatch):
        """Test export_max_rows defaults to 10000."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        config = load_config()
        assert config.export_max_rows == 10000

    def test_export_max_rows_custom_via_env(self, monkeypatch):
        """Test export_max_rows can be customized via env var."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_MAX_EXPORT_ROWS", "5000")
        config = load_config()
        assert config.export_max_rows == 5000

    def test_export_batch_size_default_500(self, monkeypatch):
        """Test export_batch_size defaults to 500."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        config = load_config()
        assert config.export_batch_size == 500

    def test_export_batch_size_custom_via_env(self, monkeypatch):
        """Test export_batch_size can be customized via env var."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_EXPORT_BATCH_SIZE", "200")
        config = load_config()
        assert config.export_batch_size == 200

    def test_export_dir_default_uses_tempdir(self, monkeypatch):
        """Test export_dir defaults to system temp directory."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        config = load_config()
        expected = Path(tempfile.gettempdir()) / "odoo-mcp-exports"
        assert config.export_dir == expected

    def test_export_dir_custom_via_env(self, monkeypatch, tmp_path):
        """Test export_dir can be customized via env var."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_EXPORT_DIR", str(tmp_path))
        config = load_config()
        assert config.export_dir == tmp_path

    def test_export_dir_auto_created(self, monkeypatch, tmp_path):
        """Test export_dir is created when accessed if it doesn't exist."""
        nested_dir = tmp_path / "deep" / "nested" / "odoo-exports"
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_EXPORT_DIR", str(nested_dir))
        config = load_config()
        # Access the property to trigger directory creation
        result_dir = config.export_dir
        assert result_dir == nested_dir
        assert nested_dir.exists()
        assert nested_dir.is_dir()

    def test_batch_size_greater_than_max_rows_raises(self, monkeypatch):
        """Test that batch_size > max_rows raises ValueError."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_EXPORT_BATCH_SIZE", "20000")
        monkeypatch.setenv("ODOO_MCP_MAX_EXPORT_ROWS", "10000")
        with pytest.raises(ValueError, match="cannot exceed"):
            load_config()

    def test_batch_size_equal_to_max_rows_ok(self, monkeypatch):
        """Test that batch_size == max_rows is allowed."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_EXPORT_BATCH_SIZE", "10000")
        monkeypatch.setenv("ODOO_MCP_MAX_EXPORT_ROWS", "10000")
        config = load_config()
        assert config.export_batch_size == config.export_max_rows

    def test_batch_size_less_than_one_raises(self, monkeypatch):
        """Test that batch_size < 1 raises ValueError."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_EXPORT_BATCH_SIZE", "0")
        with pytest.raises(ValueError, match="at least 1"):
            load_config()

    def test_max_rows_less_than_one_raises(self, monkeypatch):
        """Test that max_rows < 1 raises ValueError."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_MAX_EXPORT_ROWS", "0")
        with pytest.raises(ValueError, match="at least 1"):
            load_config()
