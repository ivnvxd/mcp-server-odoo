"""Tests for caching functionality with Odoo integration."""

import os
import time
from unittest.mock import Mock, patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.performance import PerformanceManager

# Import skip_on_rate_limit decorator
from .test_xmlrpc_operations import skip_on_rate_limit


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
            "db": "/mcp/xmlrpc/db",
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

    def test_read_caching(self, mock_config, mock_performance_manager):
        """Test read method uses cache."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        # Mock records
        mock_records = [
            {"id": 1, "name": "Partner 1"},
            {"id": 2, "name": "Partner 2"},
        ]

        with patch.object(conn, "execute_kw", return_value=mock_records) as mock_execute:
            # First read should hit server
            records1 = conn.read("res.partner", [1, 2])
            assert len(records1) == 2
            mock_execute.assert_called_once()

            # Second read of same records should use cache
            records2 = conn.read("res.partner", [1, 2])
            assert records2 == records1
            mock_execute.assert_called_once()  # Still only called once

            # Read with one cached and one new record
            mock_execute.return_value = [{"id": 3, "name": "Partner 3"}]
            records3 = conn.read("res.partner", [1, 3])
            assert len(records3) == 2
            assert records3[0]["id"] == 1  # Cached
            assert records3[1]["id"] == 3  # New
            assert mock_execute.call_count == 2  # Called again for record 3

    def test_read_cache_respects_fields(self, mock_config, mock_performance_manager):
        """Test read cache respects requested fields."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        with patch.object(conn, "execute_kw") as mock_execute:
            # Read with specific fields
            mock_execute.return_value = [{"id": 1, "name": "Partner 1"}]
            conn.read("res.partner", [1], fields=["name"])

            # Read same record with different fields should not use cache
            mock_execute.return_value = [{"id": 1, "email": "test@example.com"}]
            conn.read("res.partner", [1], fields=["email"])

            # Should have called server twice
            assert mock_execute.call_count == 2

    def test_cache_invalidation_on_write(self, mock_config, mock_performance_manager):
        """Test cache invalidation when records are modified."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"

        # Cache a record
        with patch.object(conn, "execute_kw", return_value=[{"id": 1, "name": "Original"}]):
            conn.read("res.partner", [1])

        # Invalidate the cache for this record
        mock_performance_manager.invalidate_record_cache("res.partner", 1)

        # Next read should hit server again
        with patch.object(
            conn, "execute_kw", return_value=[{"id": 1, "name": "Updated"}]
        ) as mock_execute:
            records = conn.read("res.partner", [1])
            assert records[0]["name"] == "Updated"
            mock_execute.assert_called_once()


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

    @pytest.mark.integration
    def test_real_fields_caching(self, real_config, performance_manager):
        """Test field caching with real Odoo connection."""
        conn = OdooConnection(real_config, performance_manager=performance_manager)

        try:
            conn.connect()
            conn.authenticate()

            # Measure time for first fields_get call
            start1 = time.time()
            fields1 = conn.fields_get("res.partner")
            duration1 = time.time() - start1

            # Measure time for second fields_get call (should be cached)
            start2 = time.time()
            fields2 = conn.fields_get("res.partner")
            duration2 = time.time() - start2

            # Cached call should be much faster
            assert duration2 < duration1 / 10  # At least 10x faster
            assert fields1 == fields2

            # Check cache stats
            stats = performance_manager.field_cache.get_stats()
            assert stats["hits"] == 1
            assert stats["misses"] == 1
            assert stats["hit_rate"] == 0.5

        finally:
            conn.disconnect()

    @pytest.mark.integration
    def test_real_record_caching(self, real_config, performance_manager):
        """Test record caching with real Odoo connection."""
        conn = OdooConnection(real_config, performance_manager=performance_manager)

        try:
            conn.connect()
            conn.authenticate()

            # Get some partner IDs
            partner_ids = conn.search("res.partner", [], limit=5)

            if partner_ids:
                # First read - hits server
                start1 = time.time()
                records1 = conn.read("res.partner", partner_ids[:2], ["name", "email", "phone"])
                duration1 = time.time() - start1

                # Second read - should use cache
                start2 = time.time()
                records2 = conn.read("res.partner", partner_ids[:2], ["name", "email", "phone"])
                duration2 = time.time() - start2

                # Cached read should be faster
                assert duration2 < duration1 / 5
                assert records1 == records2

                # Read mix of cached and uncached
                mixed_ids = [partner_ids[0], partner_ids[3]]  # One cached, one new
                records3 = conn.read("res.partner", mixed_ids, ["name", "email", "phone"])
                assert len(records3) == 2

                # Check cache stats
                stats = performance_manager.record_cache.get_stats()
                assert stats["hits"] >= 2  # At least 2 cache hits

        finally:
            conn.disconnect()

    @pytest.mark.integration
    @skip_on_rate_limit
    def test_cache_performance_improvement(self, real_config, performance_manager):
        """Test overall performance improvement with caching."""
        conn = OdooConnection(real_config, performance_manager=performance_manager)

        try:
            conn.connect()
            conn.authenticate()

            # Get test data
            partner_ids = conn.search("res.partner", [], limit=20)

            if len(partner_ids) >= 20:
                # Simulate repeated access pattern
                total_time_without_cache = 0
                total_time_with_cache = 0

                # First pass - cache warming
                for i in range(10):
                    record_id = partner_ids[i]
                    start = time.time()
                    conn.read("res.partner", [record_id], ["name", "email", "phone", "city"])
                    total_time_without_cache += time.time() - start

                # Second pass - using cache
                for i in range(10):
                    record_id = partner_ids[i]
                    start = time.time()
                    conn.read("res.partner", [record_id], ["name", "email", "phone", "city"])
                    total_time_with_cache += time.time() - start

                # Cache should provide significant speedup
                speedup = total_time_without_cache / total_time_with_cache
                assert speedup > 5  # At least 5x faster with cache

                # Get performance stats
                perf_stats = performance_manager.get_stats()

                # Check cache effectiveness
                cache_stats = perf_stats["caches"]["record_cache"]
                assert cache_stats["hit_rate"] >= 0.5  # Good hit rate

                # Check connection pooling
                conn_stats = perf_stats["connection_pool"]
                # Connection reuse happens after multiple operations on same endpoint
                assert conn_stats["connections_created"] >= 3  # At least 3 endpoints created

        finally:
            conn.disconnect()

    @pytest.mark.integration
    @skip_on_rate_limit
    def test_cache_memory_limits(self, real_config):
        """Test cache respects memory limits."""
        # Create manager with small memory limit
        small_manager = PerformanceManager(real_config)
        small_manager.record_cache = small_manager.record_cache.__class__(
            max_size=10,
            max_memory_mb=0.1,  # Very small limit
        )

        conn = OdooConnection(real_config, performance_manager=small_manager)

        try:
            conn.connect()
            conn.authenticate()

            # Try to cache many large records
            partner_ids = conn.search("res.partner", [], limit=50)

            if len(partner_ids) >= 20:
                for pid in partner_ids[:20]:
                    try:
                        conn.read("res.partner", [pid])
                    except Exception:
                        # Some records might not be accessible, continue
                        pass

                # Check cache didn't grow too large
                stats = small_manager.record_cache.get_stats()
                assert stats["total_entries"] <= 10
                assert stats["total_size_mb"] <= 0.5  # Allow more overhead for JSON serialization

        finally:
            conn.disconnect()

    @pytest.mark.integration
    @skip_on_rate_limit
    def test_connection_pool_reuse(self, real_config, performance_manager):
        """Test connection pooling improves performance."""
        # Create two connections sharing same performance manager
        conn1 = OdooConnection(real_config, performance_manager=performance_manager)
        conn2 = OdooConnection(real_config, performance_manager=performance_manager)

        try:
            # Connect both
            conn1.connect()
            conn1.authenticate()
            conn2.connect()
            conn2.authenticate()

            # They should be reusing connections from pool
            pool_stats = performance_manager.connection_pool.get_stats()

            # Should have reused some connections
            assert pool_stats["connections_reused"] > 0

            # Do some operations
            conn1.fields_get("res.partner")
            conn2.fields_get("res.partner")  # Use same model to avoid permission issue

            # Check pool is being used effectively
            pool_stats = performance_manager.connection_pool.get_stats()
            assert pool_stats["active_connections"] <= 6  # 3 endpoints * 2 connections max

        finally:
            conn1.disconnect()
            conn2.disconnect()
