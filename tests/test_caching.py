"""Tests for caching functionality with Odoo integration."""

import os
from unittest.mock import Mock, patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.performance import PerformanceManager


class TestOdooConnectionCaching:
    """Test caching functionality in OdooConnection."""

    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = Mock(spec=OdooConfig)
        config.url = os.getenv("ODOO_URL", "http://localhost:8069")
        config.db = "test"
        config.username = "test"
        config.password = "test"
        config.api_key = None
        config.uses_api_key = False
        config.uses_credentials = True
        config.is_yolo_enabled = False
        config.yolo_mode = "off"
        config.get_endpoint_paths.return_value = {
            "db": "/xmlrpc/db",
            "common": "/mcp/xmlrpc/common",
            "object": "/mcp/xmlrpc/object",
        }
        return config

    @pytest.fixture
    def mock_performance_manager(self, mock_config):
        """Create mock performance manager."""
        return PerformanceManager(mock_config)

    def test_fields_get_caching(self, mock_config, mock_performance_manager):
        """Test fields_get method uses cache."""
        # Create connection with performance manager
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        # Mock the connection methods
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        # Mock execute_kw
        mock_fields = {
            "name": {"type": "char", "string": "Name"},
            "email": {"type": "char", "string": "Email"},
        }

        with patch.object(conn, "execute_kw", return_value=mock_fields) as mock_execute:
            # First call should hit server
            fields1 = conn.fields_get("res.partner")
            assert fields1 == mock_fields
            mock_execute.assert_called_once()

            # Second call should use cache
            fields2 = conn.fields_get("res.partner")
            assert fields2 == mock_fields
            mock_execute.assert_called_once()  # Still only called once

        # Check cache stats
        stats = mock_performance_manager.field_cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_fields_get_with_attributes_no_cache(self, mock_config, mock_performance_manager):
        """Test fields_get with specific attributes doesn't use cache."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        mock_fields = {"name": {"type": "char"}}

        with patch.object(conn, "execute_kw", return_value=mock_fields) as mock_execute:
            # Call with attributes should not cache
            conn.fields_get("res.partner", attributes=["type"])
            conn.fields_get("res.partner", attributes=["type"])

            # Should call server twice
            assert mock_execute.call_count == 2

    def test_fields_get_allfields_passed_positionally_no_cache(
        self, mock_config, mock_performance_manager
    ):
        """allfields goes to the server as fields_get's first positional arg;
        name-filtered responses are never cached nor served from cache."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        mock_fields = {"name": {"type": "char"}}

        with patch.object(conn, "execute_kw", return_value=mock_fields) as mock_execute:
            result = conn.fields_get("res.partner", allfields=["name", "email"])
            assert result == mock_fields
            mock_execute.assert_called_once_with(
                "res.partner", "fields_get", [["name", "email"]], {}
            )

            # The filtered call must not have poisoned the full-schema cache
            conn.fields_get("res.partner")
            assert mock_execute.call_count == 2
            # ... the unfiltered result IS cached
            conn.fields_get("res.partner")
            assert mock_execute.call_count == 2
            # ... and a later filtered call bypasses the populated cache
            conn.fields_get("res.partner", allfields=["name"])
            assert mock_execute.call_count == 3
            assert mock_execute.call_args.args == ("res.partner", "fields_get", [["name"]], {})

    def test_read_always_fetches_fresh(self, mock_config, mock_performance_manager):
        """Test read method always queries Odoo (no record caching)."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        mock_records = [
            {"id": 1, "name": "Partner 1"},
            {"id": 2, "name": "Partner 2"},
        ]

        with patch.object(conn, "execute_kw", return_value=mock_records) as mock_execute:
            # First read
            records1 = conn.read("res.partner", [1, 2])
            assert len(records1) == 2
            assert mock_execute.call_count == 1

            # Second read should also hit the server (no caching)
            records2 = conn.read("res.partner", [1, 2])
            assert len(records2) == 2
            assert mock_execute.call_count == 2

    def test_read_returns_current_data(self, mock_config, mock_performance_manager):
        """Test read always returns current data even when values change."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        with patch.object(conn, "execute_kw") as mock_execute:
            # First read returns active=False
            mock_execute.return_value = [{"id": 1, "name": "Product", "active": False}]
            records1 = conn.read("res.partner", [1])
            assert records1[0]["active"] is False

            # Record updated in Odoo, second read returns active=True
            mock_execute.return_value = [{"id": 1, "name": "Product", "active": True}]
            records2 = conn.read("res.partner", [1])
            assert records2[0]["active"] is True
            assert mock_execute.call_count == 2


class TestCachingIntegration:
    """Integration tests for caching with real Odoo connection."""

    @pytest.fixture
    def real_config(self):
        """Load real configuration."""
        return load_config()

    @pytest.fixture
    def performance_manager(self, real_config):
        """Create performance manager with real config."""
        return PerformanceManager(real_config)

    @pytest.mark.mcp
    def test_real_fields_caching(self, real_config, performance_manager):
        """Test field caching with real Odoo connection."""
        conn = OdooConnection(real_config, performance_manager=performance_manager)

        try:
            conn.connect()
            conn.authenticate()

            # First call populates cache
            fields1 = conn.fields_get("res.partner")

            # Second call should hit cache
            fields2 = conn.fields_get("res.partner")

            assert fields1 == fields2

            # Verify cache stats (deterministic check instead of timing)
            stats = performance_manager.field_cache.get_stats()
            assert stats["hits"] >= 1
            assert stats["misses"] == 1

        finally:
            conn.disconnect()

    @pytest.mark.mcp
    def test_real_read_bypasses_cache(self, real_config, performance_manager):
        """read() always returns fresh server data: a write between two reads
        must be visible in the second read (no record caching)."""
        conn = OdooConnection(real_config, performance_manager=performance_manager)
        company_id = None

        try:
            conn.connect()
            conn.authenticate()

            # res.company has full CRUD enabled on the test instance
            company_id = conn.create("res.company", {"name": "Cache Bypass Test Co"})

            records1 = conn.read("res.company", [company_id], ["name"])
            assert records1[0]["name"] == "Cache Bypass Test Co"

            conn.write("res.company", [company_id], {"name": "Cache Bypass Test Co v2"})

            records2 = conn.read("res.company", [company_id], ["name"])
            assert records2[0]["name"] == "Cache Bypass Test Co v2", (
                "second read returned stale data — read() must not serve cached records"
            )

        finally:
            if company_id:
                try:
                    conn.unlink("res.company", [company_id])
                except Exception:
                    pass
            conn.disconnect()
