"""Tests for basic Odoo XML-RPC connection infrastructure.

These tests use a real Odoo server at localhost:8069 to test
connection management and error handling.
"""

import os
import socket
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError, create_connection


@pytest.fixture
def test_config():
    """Create test configuration."""
    return OdooConfig(
        url=os.getenv("ODOO_URL", "http://localhost:8069"),
        api_key=os.getenv("ODOO_API_KEY") or None,
        username=os.getenv("ODOO_USER", "admin"),
        password=os.getenv("ODOO_PASSWORD", "admin"),
        database=os.getenv("ODOO_DB") or "odoo",
        log_level="INFO",
        default_limit=10,
        max_limit=100,
        yolo_mode=os.getenv("ODOO_YOLO", "off"),
    )


@pytest.fixture
def invalid_config():
    """Create configuration with invalid URL."""
    return OdooConfig(
        url="http://invalid.host.nowhere:9999",
        api_key="test_api_key",
        database=os.getenv("ODOO_DB") or "odoo",
        log_level="INFO",
        default_limit=10,
        max_limit=100,
    )


class TestOdooConnectionInit:
    """Test OdooConnection initialization."""

    def test_init_valid_config(self, test_config):
        """Test initialization with valid configuration."""
        conn = OdooConnection(test_config)

        assert conn.config == test_config
        assert conn.timeout == OdooConnection.DEFAULT_TIMEOUT
        assert not conn.is_connected

        # Parse expected values from config URL
        from urllib.parse import urlparse

        parsed = urlparse(test_config.url)
        expected_host = parsed.hostname or "localhost"
        expected_port = parsed.port or (443 if parsed.scheme == "https" else 80)

        assert conn._url_components["host"] == expected_host
        assert conn._url_components["port"] == expected_port
        assert conn._url_components["scheme"] == parsed.scheme

    def test_init_custom_timeout(self, test_config):
        """Test initialization with custom timeout."""
        conn = OdooConnection(test_config, timeout=60)
        assert conn.timeout == 60

    def test_parse_url_https(self):
        """Test URL parsing for HTTPS URLs."""
        config = OdooConfig(
            url="https://odoo.example.com", api_key="test", database=os.getenv("ODOO_DB")
        )
        conn = OdooConnection(config)

        assert conn._url_components["scheme"] == "https"
        assert conn._url_components["host"] == "odoo.example.com"
        assert conn._url_components["port"] == 443

    def test_parse_url_with_path(self):
        """Test URL parsing with path."""
        config = OdooConfig(
            url="http://localhost:8069/custom/path", api_key="test", database=os.getenv("ODOO_DB")
        )
        conn = OdooConnection(config)

        assert conn._url_components["path"] == "/custom/path"
        assert conn._url_components["base_url"] == "http://localhost:8069/custom/path"

    def test_parse_url_invalid_scheme(self):
        """Test URL parsing with invalid scheme."""
        with pytest.raises(ValueError, match="ODOO_URL must start with http:// or https://"):
            config = OdooConfig(
                url="ftp://localhost:8069", api_key="test", database=os.getenv("ODOO_DB")
            )
            OdooConnection(config)

    def test_build_endpoint_url(self, test_config):
        """Test endpoint URL building."""
        conn = OdooConnection(test_config)

        # Get the endpoints from config
        endpoints = test_config.get_endpoint_paths()

        db_url = conn._build_endpoint_url(endpoints["db"])
        # Build expected URL from config
        from urllib.parse import urlparse

        parsed = urlparse(test_config.url)
        expected_url = f"{parsed.scheme}://{parsed.netloc}{endpoints['db']}"
        assert db_url == expected_url

        common_url = conn._build_endpoint_url(endpoints["common"])
        expected_common_url = f"{parsed.scheme}://{parsed.netloc}{endpoints['common']}"
        assert common_url == expected_common_url

        object_url = conn._build_endpoint_url(endpoints["object"])
        expected_object_url = f"{parsed.scheme}://{parsed.netloc}{endpoints['object']}"
        assert object_url == expected_object_url


class TestBuildRecordUrl:
    """Test version-aware record URL generation."""

    def test_legacy_url_for_odoo_17(self, test_config):
        """Odoo <= 17 should use /web# hash format."""
        conn = OdooConnection(test_config)
        conn._server_version = "17.0"
        url = conn.build_record_url("res.partner", 42)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/web#id=42&model=res.partner&view_type=form"

    def test_legacy_url_for_odoo_16(self, test_config):
        """Odoo 16 should use /web# hash format."""
        conn = OdooConnection(test_config)
        conn._server_version = "16.0"
        url = conn.build_record_url("res.partner", 42)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/web#id=42&model=res.partner&view_type=form"

    def test_modern_url_for_odoo_18(self, test_config):
        """Odoo 18+ should use /odoo/ path format."""
        conn = OdooConnection(test_config)
        conn._server_version = "18.0"
        url = conn.build_record_url("res.partner", 42)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/odoo/res.partner/42"

    def test_modern_url_for_odoo_19(self, test_config):
        """Odoo 19 should use /odoo/ path format."""
        conn = OdooConnection(test_config)
        conn._server_version = "19.0"
        url = conn.build_record_url("sale.order", 7)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/odoo/sale.order/7"

    def test_fallback_when_version_unknown(self, test_config):
        """Unknown version should fall back to legacy format."""
        conn = OdooConnection(test_config)
        conn._server_version = None
        url = conn.build_record_url("res.partner", 42)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/web#id=42&model=res.partner&view_type=form"

    def test_saas_version_18(self, test_config):
        """SaaS version based on Odoo 18 should use modern format."""
        conn = OdooConnection(test_config)
        conn._server_version = "saas~18.1"
        url = conn.build_record_url("res.partner", 42)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/odoo/res.partner/42"

    def test_saas_version_17(self, test_config):
        """SaaS version based on Odoo 17 should use legacy format."""
        conn = OdooConnection(test_config)
        conn._server_version = "saas~17.4"
        url = conn.build_record_url("res.partner", 42)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/web#id=42&model=res.partner&view_type=form"

    def test_fallback_when_version_malformed(self, test_config):
        """Completely malformed version string should fall back to legacy format."""
        conn = OdooConnection(test_config)
        conn._server_version = "unknown"
        url = conn.build_record_url("res.partner", 42)
        base = test_config.url.rstrip("/")
        assert url == f"{base}/web#id=42&model=res.partner&view_type=form"


class TestOdooConnectionConnect:
    """Test connection establishment."""

    @pytest.mark.yolo
    def test_connect_success(self, test_config):
        """Test successful connection to real Odoo server."""
        conn = OdooConnection(test_config)

        try:
            conn.connect()
            assert conn.is_connected
            assert conn._db_proxy is not None
            assert conn._common_proxy is not None
            assert conn._object_proxy is not None
        finally:
            conn.disconnect()

    @pytest.mark.yolo
    def test_connect_already_connected(self, test_config, caplog):
        """Test connecting when already connected."""
        conn = OdooConnection(test_config)

        try:
            conn.connect()
            assert conn.is_connected

            # Try to connect again
            conn.connect()
            assert "Already connected to Odoo" in caplog.text
        finally:
            conn.disconnect()

    def test_connect_invalid_host(self, invalid_config):
        """Test connection to invalid host."""
        conn = OdooConnection(invalid_config)

        with pytest.raises(OdooConnectionError) as exc_info:
            conn.connect()

        error_msg = str(exc_info.value)
        assert "Connection failed" in error_msg or "Connection test failed" in error_msg

    def test_connect_timeout(self, test_config):
        """Test connection timeout handling."""
        # Use very short timeout
        conn = OdooConnection(test_config, timeout=0.001)

        # Mock socket to simulate timeout
        with patch("socket.socket") as mock_socket:
            mock_socket.side_effect = socket.timeout("Timeout")

            with pytest.raises(OdooConnectionError) as exc_info:
                conn.connect()

            error_msg = str(exc_info.value)
            assert "Connection failed" in error_msg or "Connection test failed" in error_msg


class TestOdooConnectionDisconnect:
    """Test connection cleanup."""

    @pytest.mark.yolo
    def test_disconnect_when_connected(self, test_config):
        """Test normal disconnect."""
        conn = OdooConnection(test_config)

        conn.connect()
        assert conn.is_connected

        conn.disconnect()
        assert not conn.is_connected
        assert conn._db_proxy is None
        assert conn._common_proxy is None
        assert conn._object_proxy is None

    def test_disconnect_when_not_connected(self, test_config, caplog):
        """Test disconnect when not connected."""
        conn = OdooConnection(test_config)

        conn.disconnect()
        assert "Not connected to Odoo" in caplog.text


class TestOdooConnectionHealth:
    """Test health checking."""

    @pytest.mark.yolo
    def test_check_health_connected(self, test_config):
        """Test health check when connected."""
        conn = OdooConnection(test_config)

        try:
            conn.connect()
            is_healthy, message = conn.check_health()

            assert is_healthy
            assert "Connected to Odoo" in message
        finally:
            conn.disconnect()

    def test_check_health_not_connected(self, test_config):
        """Test health check when not connected."""
        conn = OdooConnection(test_config)

        is_healthy, message = conn.check_health()

        assert not is_healthy
        assert message == "Not connected"

    @pytest.mark.yolo
    def test_check_health_error(self, test_config):
        """Test health check with connection error."""
        conn = OdooConnection(test_config)
        conn.connect()

        # Mock common proxy to simulate error
        conn._common_proxy = MagicMock()
        conn._common_proxy.version.side_effect = Exception("Server error")

        is_healthy, message = conn.check_health()

        assert not is_healthy
        assert "Health check failed" in message

        conn.disconnect()

    def test_check_health_timeout(self, test_config):
        """Test health check returns failure tuple on socket timeout."""
        conn = OdooConnection(test_config)
        conn._connected = True
        conn._common_proxy = MagicMock()
        conn._common_proxy.version.side_effect = socket.timeout("timed out")

        is_healthy, message = conn.check_health()

        assert is_healthy is False
        assert "Health check timeout" in message


class TestOdooConnectionProxies:
    """Test proxy access."""

    @pytest.mark.yolo
    def test_proxy_access_when_connected(self, test_config):
        """Test accessing proxies when connected."""
        conn = OdooConnection(test_config)

        try:
            conn.connect()

            # Should not raise
            db_proxy = conn.db_proxy
            common_proxy = conn.common_proxy
            object_proxy = conn.object_proxy

            assert db_proxy is not None
            assert common_proxy is not None
            assert object_proxy is not None
        finally:
            conn.disconnect()

    def test_proxy_access_when_not_connected(self, test_config):
        """Test accessing proxies when not connected."""
        conn = OdooConnection(test_config)

        with pytest.raises(OdooConnectionError, match="Not connected"):
            _ = conn.db_proxy

        with pytest.raises(OdooConnectionError, match="Not connected"):
            _ = conn.common_proxy

        with pytest.raises(OdooConnectionError, match="Not connected"):
            _ = conn.object_proxy


class TestOdooConnectionContext:
    """Test context manager functionality."""

    @pytest.mark.yolo
    def test_context_manager_success(self, test_config):
        """Test using connection as context manager."""
        with OdooConnection(test_config) as conn:
            assert conn.is_connected

            # Test that we can use the connection
            is_healthy, _ = conn.check_health()
            assert is_healthy

        # Should be disconnected after context
        assert not conn.is_connected

    @pytest.mark.yolo
    def test_context_manager_with_error(self, test_config):
        """Test context manager with error in context."""
        conn = OdooConnection(test_config)

        try:
            with conn:
                assert conn.is_connected
                raise ValueError("Test error")
        except ValueError:
            pass

        # Should still be disconnected
        assert not conn.is_connected

    @pytest.mark.yolo
    def test_create_connection_helper(self, test_config):
        """Test create_connection helper function."""
        with create_connection(test_config) as conn:
            assert conn.is_connected
            assert isinstance(conn, OdooConnection)

        assert not conn.is_connected


class TestOdooConnectionIntegration:
    """Integration tests with real Odoo server."""

    @pytest.mark.yolo
    def test_real_server_version(self, test_config):
        """Test getting version from real server."""
        with create_connection(test_config) as conn:
            version = conn.common_proxy.version()

            assert isinstance(version, dict)
            assert "server_version" in version
            assert "protocol_version" in version

    @pytest.mark.yolo
    def test_real_server_db_list(self, test_config):
        """Test listing databases from real server."""
        with create_connection(test_config) as conn:
            try:
                db_list = conn.db_proxy.list()
            except Exception as e:
                if "Access Denied" in str(e):
                    pytest.skip("Database listing is disabled on this server")
                raise
            assert isinstance(db_list, list)
            assert len(db_list) > 0
