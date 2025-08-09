"""Tests for the configuration module."""

import os
import tempfile
from pathlib import Path

import pytest

from mcp_server_odoo.config import OdooConfig, get_config, load_config, reset_config, set_config


@pytest.fixture(autouse=True)
def reset_config_fixture():
    """Reset configuration before each test."""
    reset_config()
    yield
    reset_config()


class TestOdooConfig:
    """Test the OdooConfig dataclass."""

    def test_valid_config_with_api_key(self):
        """Test creating a valid configuration with API key."""
        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"), api_key="test-api-key"
        )
        assert config.url == os.getenv("ODOO_URL", "http://localhost:8069")
        assert config.api_key == "test-api-key"
        assert config.uses_api_key is True
        assert config.uses_credentials is False
        assert config.log_level == "INFO"
        assert config.default_limit == 10
        assert config.max_limit == 100

    def test_valid_config_with_credentials(self):
        """Test creating a valid configuration with username/password."""
        config = OdooConfig(
            url="https://odoo.example.com",
            username="testuser",
            password="testpass",
            database="test_db",
        )
        assert config.url == "https://odoo.example.com"
        assert config.username == "testuser"
        assert config.password == "testpass"
        assert config.database == "test_db"
        assert config.uses_api_key is False
        assert config.uses_credentials is True

    def test_missing_url_raises_error(self):
        """Test that missing URL raises ValueError."""
        with pytest.raises(ValueError, match="ODOO_URL is required"):
            OdooConfig(url="", api_key="test-key")

    def test_invalid_url_format_raises_error(self):
        """Test that invalid URL format raises ValueError."""
        with pytest.raises(ValueError, match="ODOO_URL must start with http"):
            OdooConfig(url="invalid-url", api_key="test-key")

    def test_missing_authentication_raises_error(self):
        """Test that missing authentication raises ValueError."""
        with pytest.raises(ValueError, match="Authentication required"):
            OdooConfig(url="http://localhost:8069")

    def test_incomplete_credentials_raises_error(self):
        """Test that incomplete username/password raises ValueError."""
        with pytest.raises(ValueError, match="Authentication required"):
            OdooConfig(url="http://localhost:8069", username="user")

    def test_invalid_default_limit(self):
        """Test that invalid default limit raises ValueError."""
        with pytest.raises(ValueError, match="ODOO_MCP_DEFAULT_LIMIT must be positive"):
            OdooConfig(url="http://localhost:8069", api_key="test-key", default_limit=0)

    def test_invalid_max_limit(self):
        """Test that invalid max limit raises ValueError."""
        with pytest.raises(ValueError, match="ODOO_MCP_MAX_LIMIT must be positive"):
            OdooConfig(url="http://localhost:8069", api_key="test-key", max_limit=-1)

    def test_default_exceeds_max_limit(self):
        """Test that default exceeding max limit raises ValueError."""
        with pytest.raises(ValueError, match="cannot exceed ODOO_MCP_MAX_LIMIT"):
            OdooConfig(
                url="http://localhost:8069", api_key="test-key", default_limit=100, max_limit=50
            )

    def test_invalid_log_level(self):
        """Test that invalid log level raises ValueError."""
        with pytest.raises(ValueError, match="Invalid log level"):
            OdooConfig(url="http://localhost:8069", api_key="test-key", log_level="INVALID")

    def test_log_level_case_insensitive(self):
        """Test that log level is case insensitive."""
        config = OdooConfig(url="http://localhost:8069", api_key="test-key", log_level="debug")
        # Config should validate successfully
        assert config.log_level == "debug"


class TestLoadConfig:
    """Test the load_config function."""

    def test_load_config_from_env_vars(self, monkeypatch):
        """Test loading configuration from environment variables."""
        monkeypatch.setenv("ODOO_URL", "http://test.odoo.com")
        monkeypatch.setenv("ODOO_API_KEY", "env-api-key")
        monkeypatch.setenv("ODOO_DB", "test_db")
        monkeypatch.setenv("ODOO_MCP_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("ODOO_MCP_DEFAULT_LIMIT", "20")
        monkeypatch.setenv("ODOO_MCP_MAX_LIMIT", "200")

        config = load_config()

        assert config.url == "http://test.odoo.com"
        assert config.api_key == "env-api-key"
        assert config.database == "test_db"
        assert config.log_level == "DEBUG"
        assert config.default_limit == 20
        assert config.max_limit == 200

    def test_load_config_from_env_file(self, monkeypatch):
        """Test loading configuration from .env file."""
        # Clear environment variables
        for key in [
            "ODOO_URL",
            "ODOO_API_KEY",
            "ODOO_USER",
            "ODOO_PASSWORD",
            "ODOO_MCP_DEFAULT_LIMIT",
            "ODOO_MCP_MAX_LIMIT",
        ]:
            monkeypatch.delenv(key, raising=False)

        # Create a temporary .env file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("ODOO_URL=http://file.odoo.com\n")
            f.write("ODOO_USER=fileuser\n")
            f.write("ODOO_PASSWORD=filepass\n")
            f.write("ODOO_MCP_DEFAULT_LIMIT=30\n")
            env_file = f.name

        try:
            config = load_config(Path(env_file))

            assert config.url == "http://file.odoo.com"
            assert config.username == "fileuser"
            assert config.password == "filepass"
            assert config.default_limit == 30
        finally:
            os.unlink(env_file)

    def test_env_vars_override_env_file(self, monkeypatch):
        """Test that environment variables override .env file."""
        # Set environment variable
        monkeypatch.setenv("ODOO_URL", "http://env.odoo.com")
        monkeypatch.setenv("ODOO_API_KEY", "env-key")

        # Create a temporary .env file with different values
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("ODOO_URL=http://file.odoo.com\n")
            f.write("ODOO_API_KEY=file-key\n")
            env_file = f.name

        try:
            config = load_config(Path(env_file))

            # Environment variables should take precedence
            assert config.url == "http://env.odoo.com"
            assert config.api_key == "env-key"
        finally:
            os.unlink(env_file)

    def test_load_config_with_empty_strings(self, monkeypatch):
        """Test that empty strings are treated as None."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "  ")  # Whitespace only
        monkeypatch.setenv("ODOO_USER", "user")
        monkeypatch.setenv("ODOO_PASSWORD", "pass")
        monkeypatch.setenv("ODOO_DB", "")  # Empty string

        config = load_config()

        assert config.api_key is None  # Whitespace stripped to empty
        assert config.database is None  # Empty string becomes None
        # Should use credentials since API key is empty
        assert config.uses_credentials is True

    def test_load_config_invalid_integer(self, monkeypatch):
        """Test that invalid integer values raise ValueError."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test-key")
        monkeypatch.setenv("ODOO_MCP_DEFAULT_LIMIT", "not-a-number")

        with pytest.raises(ValueError, match="must be a valid integer"):
            load_config()


class TestConfigSingleton:
    """Test the singleton configuration management."""

    def test_get_config_loads_config(self, monkeypatch):
        """Test that get_config loads configuration on first call."""
        reset_config()  # Ensure clean state

        monkeypatch.setenv("ODOO_URL", "http://singleton.odoo.com")
        monkeypatch.setenv("ODOO_API_KEY", "singleton-key")

        config = get_config()

        assert config.url == "http://singleton.odoo.com"
        assert config.api_key == "singleton-key"

        # Second call should return same instance
        config2 = get_config()
        assert config is config2

    def test_set_config(self):
        """Test setting a custom configuration."""
        reset_config()  # Ensure clean state

        custom_config = OdooConfig(url="http://custom.odoo.com", api_key="custom-key")

        set_config(custom_config)

        config = get_config()
        assert config is custom_config
        assert config.url == "http://custom.odoo.com"

    def test_reset_config(self, monkeypatch):
        """Test resetting the configuration."""
        # Set initial config
        monkeypatch.setenv("ODOO_URL", "http://first.odoo.com")
        monkeypatch.setenv("ODOO_API_KEY", "first-key")

        config1 = get_config()
        assert config1.url == "http://first.odoo.com"

        # Reset and change environment
        reset_config()
        monkeypatch.setenv("ODOO_URL", "http://second.odoo.com")
        monkeypatch.setenv("ODOO_API_KEY", "second-key")

        config2 = get_config()
        assert config2.url == "http://second.odoo.com"
        assert config1 is not config2


class TestYoloMode:
    """Test YOLO mode configuration."""

    def test_yolo_mode_default_off(self):
        """Test YOLO mode is disabled by default."""
        config = OdooConfig(url="http://localhost:8069", api_key="test")
        assert config.yolo_mode == "off"
        assert config.is_yolo_enabled is False
        assert config.is_write_allowed is False

    def test_yolo_mode_read_only(self):
        """Test read-only YOLO mode."""
        config = OdooConfig(
            url="http://localhost:8069", username="admin", password="admin", yolo_mode="read"
        )
        assert config.yolo_mode == "read"
        assert config.is_yolo_enabled is True
        assert config.is_write_allowed is False

    def test_yolo_mode_full_access(self):
        """Test full access YOLO mode."""
        config = OdooConfig(
            url="http://localhost:8069", username="admin", password="admin", yolo_mode="true"
        )
        assert config.yolo_mode == "true"
        assert config.is_yolo_enabled is True
        assert config.is_write_allowed is True

    def test_invalid_yolo_mode(self):
        """Test invalid YOLO mode raises error."""
        with pytest.raises(ValueError, match="Invalid YOLO mode"):
            OdooConfig(
                url="http://localhost:8069", username="admin", password="admin", yolo_mode="invalid"
            )

    def test_endpoint_paths_standard_mode(self):
        """Test endpoint paths in standard mode."""
        config = OdooConfig(url="http://localhost:8069", api_key="test", yolo_mode="off")
        paths = config.get_endpoint_paths()
        assert paths["common"] == "/mcp/xmlrpc/common"
        assert paths["object"] == "/mcp/xmlrpc/object"
        assert paths["db"] == "/mcp/xmlrpc/db"

    def test_endpoint_paths_yolo_modes(self):
        """Test endpoint paths in YOLO modes."""
        # Read-only mode
        config = OdooConfig(
            url="http://localhost:8069", username="test", password="test", yolo_mode="read"
        )
        paths = config.get_endpoint_paths()
        assert paths["common"] == "/xmlrpc/2/common"
        assert paths["object"] == "/xmlrpc/2/object"
        assert paths["db"] == "/xmlrpc/db"

        # Full access mode
        config = OdooConfig(
            url="http://localhost:8069", username="test", password="test", yolo_mode="true"
        )
        paths = config.get_endpoint_paths()
        assert paths["common"] == "/xmlrpc/2/common"
        assert paths["object"] == "/xmlrpc/2/object"
        assert paths["db"] == "/xmlrpc/db"

    def test_yolo_mode_auth_requirements(self):
        """Test YOLO mode authentication requirements."""
        # YOLO mode with username/password - should work
        config = OdooConfig(
            url="http://localhost:8069", username="admin", password="admin", yolo_mode="read"
        )
        assert config.is_yolo_enabled is True

        # YOLO mode with username/API key - should work
        config = OdooConfig(
            url="http://localhost:8069", username="admin", api_key="test-key", yolo_mode="true"
        )
        assert config.is_yolo_enabled is True

        # YOLO mode without proper auth - should fail
        with pytest.raises(ValueError, match="YOLO mode requires"):
            OdooConfig(
                url="http://localhost:8069",
                api_key="test-key",  # Missing username
                yolo_mode="read",
            )

    def test_yolo_mode_from_env(self, monkeypatch):
        """Test loading YOLO mode from environment variables."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_USER", "admin")
        monkeypatch.setenv("ODOO_PASSWORD", "admin")

        # Test "read" mode
        monkeypatch.setenv("ODOO_YOLO", "read")
        config = load_config()
        assert config.yolo_mode == "read"
        assert config.is_yolo_enabled is True
        assert config.is_write_allowed is False

        # Test "true" mode
        monkeypatch.setenv("ODOO_YOLO", "true")
        config = load_config()
        assert config.yolo_mode == "true"
        assert config.is_yolo_enabled is True
        assert config.is_write_allowed is True

        # Test "off" mode (default)
        monkeypatch.delenv("ODOO_YOLO")
        config = load_config()
        assert config.yolo_mode == "off"
        assert config.is_yolo_enabled is False
        assert config.is_write_allowed is False

    def test_yolo_mode_env_aliases(self, monkeypatch):
        """Test YOLO mode environment variable aliases."""
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_USER", "admin")
        monkeypatch.setenv("ODOO_PASSWORD", "admin")

        # Test various aliases for "off"
        for value in ["", "false", "0", "off", "no"]:
            monkeypatch.setenv("ODOO_YOLO", value)
            config = load_config()
            assert config.yolo_mode == "off"

        # Test various aliases for "read"
        for value in ["read", "readonly", "read-only"]:
            monkeypatch.setenv("ODOO_YOLO", value)
            config = load_config()
            assert config.yolo_mode == "read"

        # Test various aliases for "true"
        for value in ["true", "1", "yes", "full"]:
            monkeypatch.setenv("ODOO_YOLO", value)
            config = load_config()
            assert config.yolo_mode == "true"
