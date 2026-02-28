"""Tests for performance optimization module."""

import asyncio
import os
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.performance import (
    Cache,
    CacheEntry,
    ConnectionPool,
    PerformanceManager,
    PerformanceMonitor,
    RequestOptimizer,
)


class TestCacheEntry:
    """Test CacheEntry functionality."""

    def test_cache_entry_creation(self):
        """Test creating a cache entry."""
        now = datetime.now()
        entry = CacheEntry(
            key="test_key",
            value={"data": "test"},
            created_at=now,
            accessed_at=now,
            ttl_seconds=300,
            hit_count=0,
            size_bytes=100,
        )

        assert not entry.is_expired()

    def test_cache_entry_expiration(self):
        """Test cache entry expiration."""
        # Create an entry that's already expired
        old_time = datetime.now() - timedelta(seconds=600)
        entry = CacheEntry(
            key="test_key",
            value="test_value",
            created_at=old_time,
            accessed_at=old_time,
            ttl_seconds=300,
        )

        assert entry.is_expired()

    def test_cache_entry_access(self):
        """Test accessing a cache entry."""
        entry = CacheEntry(
            key="test_key",
            value="test_value",
            created_at=datetime.now(),
            accessed_at=datetime.now(),
            ttl_seconds=300,
        )

        original_access_time = entry.accessed_at
        original_hit_count = entry.hit_count

        # Access the entry
        time.sleep(0.01)  # Small delay to ensure time difference
        entry.access()

        assert entry.hit_count == original_hit_count + 1
        assert entry.accessed_at > original_access_time


class TestCache:
    """Test Cache functionality."""

    def test_cache_put_and_get(self):
        """Test basic cache put and get operations."""
        cache = Cache(max_size=10, max_memory_mb=1)

        # Put a value
        cache.put("key1", {"data": "value1"}, ttl_seconds=300)

        # Get the value
        value = cache.get("key1")
        assert value == {"data": "value1"}

        # Check stats
        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 0
        assert stats["total_entries"] == 1

    def test_cache_miss(self):
        """Test cache miss."""
        cache = Cache()

        # Try to get non-existent key
        value = cache.get("non_existent")
        assert value is None

        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 1

    def test_cache_expiration(self):
        """Test cache entry expiration."""
        cache = Cache()

        # Put a value with very short TTL
        cache.put("key1", "value1", ttl_seconds=0)

        # Try to get it (should be expired)
        time.sleep(0.01)
        value = cache.get("key1")
        assert value is None

        stats = cache.get_stats()
        assert stats["expired_evictions"] == 1

    def test_cache_lru_eviction(self):
        """Test LRU eviction when cache is full."""
        cache = Cache(max_size=3)

        # Fill the cache
        cache.put("key1", "value1")
        cache.put("key2", "value2")
        cache.put("key3", "value3")

        # Access key1 and key2 to make them more recently used
        cache.get("key1")
        cache.get("key2")

        # Add a new entry (should evict key3)
        cache.put("key4", "value4")

        # key3 should be evicted
        assert cache.get("key3") is None
        assert cache.get("key1") == "value1"
        assert cache.get("key2") == "value2"
        assert cache.get("key4") == "value4"

    def test_cache_invalidate(self):
        """Test cache invalidation."""
        cache = Cache()

        cache.put("key1", "value1")
        cache.put("key2", "value2")

        # Invalidate one key
        removed = cache.invalidate("key1")
        assert removed is True
        assert cache.get("key1") is None
        assert cache.get("key2") == "value2"

        # Try to invalidate non-existent key
        removed = cache.invalidate("non_existent")
        assert removed is False

    def test_cache_invalidate_pattern(self):
        """Test pattern-based cache invalidation."""
        cache = Cache()

        # Put values with pattern
        cache.put("model:res.partner:1", "partner1")
        cache.put("model:res.partner:2", "partner2")
        cache.put("model:res.users:1", "user1")
        cache.put("other:key", "other_value")

        # Invalidate all partner entries
        count = cache.invalidate_pattern("model:res.partner:*")
        assert count == 2
        assert cache.get("model:res.partner:1") is None
        assert cache.get("model:res.partner:2") is None
        assert cache.get("model:res.users:1") == "user1"
        assert cache.get("other:key") == "other_value"

    def test_cache_clear(self):
        """Test clearing the cache."""
        cache = Cache()

        cache.put("key1", "value1")
        cache.put("key2", "value2")

        # Clear cache
        cache.clear()

        assert cache.get("key1") is None
        assert cache.get("key2") is None

        stats = cache.get_stats()
        assert stats["total_entries"] == 0
        # Note: misses are counted from the get() calls above
        assert stats["misses"] == 2


class TestConnectionPool:
    """Test ConnectionPool functionality."""

    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = Mock(spec=OdooConfig)
        config.url = os.getenv("ODOO_URL", "http://localhost:8069")
        return config

    def test_connection_pool_creation(self, mock_config):
        """Test creating a connection pool."""
        pool = ConnectionPool(mock_config, max_connections=5)

        assert pool.max_connections == 5
        assert pool.config == mock_config
        assert len(pool._connections) == 0

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_get_connection(self, mock_proxy, mock_config):
        """Test getting a connection from pool."""
        pool = ConnectionPool(mock_config)

        # Get a connection
        pool.get_connection("/xmlrpc/2/common")

        # Should create a new connection
        mock_proxy.assert_called_once()
        stats = pool.get_stats()
        assert stats["connections_created"] == 1
        assert stats["connections_reused"] == 0

        # Get same endpoint again (should reuse)
        pool.get_connection("/xmlrpc/2/common")

        stats = pool.get_stats()
        assert stats["connections_created"] == 1
        assert stats["connections_reused"] == 1

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_connection_pool_max_limit(self, mock_proxy, mock_config):
        """Test connection pool respects max connections."""
        pool = ConnectionPool(mock_config, max_connections=2)

        # Create max connections
        pool.get_connection("/endpoint1")
        pool.get_connection("/endpoint2")

        stats = pool.get_stats()
        assert stats["active_connections"] == 2

        # Creating another should remove oldest
        pool.get_connection("/endpoint3")

        stats = pool.get_stats()
        assert stats["active_connections"] == 2
        assert stats["connections_closed"] == 1

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_connection_pool_set_database(self, mock_proxy, mock_config):
        """Test set_database clears pool and sets transport database."""
        pool = ConnectionPool(mock_config, max_connections=5)

        # Create a couple of connections first
        pool.get_connection("/xmlrpc/2/common")
        pool.get_connection("/xmlrpc/2/object")
        assert pool.get_stats()["active_connections"] == 2

        # Set database
        pool.set_database("new_db")

        # Pool should be cleared
        assert pool.get_stats()["active_connections"] == 0
        assert pool.get_stats()["connections_closed"] == 2
        # Transport should have database set
        assert pool._transport.database == "new_db"

    def test_odoo_transport_sends_database_header(self, mock_config):
        """Test OdooTransport injects X-Odoo-Database header via send_headers."""
        from mcp_server_odoo.performance import OdooTransport

        transport = OdooTransport(database="mydb")
        mock_connection = Mock()

        # Call send_headers with empty headers list
        transport.send_headers(mock_connection, [])

        # Verify that putheader was called with the database header
        mock_connection.putheader.assert_called_with("X-Odoo-Database", "mydb")

    def test_connection_pool_clear(self, mock_config):
        """Test clearing connection pool."""
        pool = ConnectionPool(mock_config)

        # Add some connections
        with patch("mcp_server_odoo.performance.ServerProxy"):
            pool.get_connection("/endpoint1")
            pool.get_connection("/endpoint2")

        # Clear pool
        pool.clear()

        stats = pool.get_stats()
        assert stats["active_connections"] == 0
        assert stats["connections_closed"] == 2


class TestRequestOptimizer:
    """Test RequestOptimizer functionality."""

    def test_field_usage_tracking(self):
        """Test tracking field usage."""
        optimizer = RequestOptimizer()

        # Track field usage
        optimizer.track_field_usage("res.partner", ["name", "email"])
        optimizer.track_field_usage("res.partner", ["name", "phone"])
        optimizer.track_field_usage("res.partner", ["name", "is_company"])

        # Get optimized fields
        fields = optimizer.get_optimized_fields("res.partner", None)

        # "name" should be first (used 3 times)
        assert fields[0] == "name"
        assert len(fields) <= 20

    def test_get_optimized_fields_passthrough(self):
        """Test that explicitly requested fields are returned as-is (early-return path)."""
        optimizer = RequestOptimizer()

        # When specific fields are requested, they should pass through unchanged
        fields = optimizer.get_optimized_fields("res.partner", ["id", "display_name"])
        assert fields == ["id", "display_name"]

    def test_should_batch_request(self):
        """Test batch request logic."""
        optimizer = RequestOptimizer()

        # Large read should be batched
        assert optimizer.should_batch_request("res.partner", "read", 100) is True

        # Small read should not
        assert optimizer.should_batch_request("res.partner", "read", 10) is False

    def test_batch_queue(self):
        """Test batch queue operations."""
        optimizer = RequestOptimizer()

        # Add to batch
        optimizer.add_to_batch("res.partner", "read", {"ids": [1, 2, 3]})
        optimizer.add_to_batch("res.partner", "read", {"ids": [4, 5, 6]})

        # Get batch
        batch = optimizer.get_batch("res.partner", "read")
        assert len(batch) == 2
        assert batch[0]["ids"] == [1, 2, 3]
        assert batch[1]["ids"] == [4, 5, 6]

        # Queue should be empty now
        batch = optimizer.get_batch("res.partner", "read")
        assert len(batch) == 0


class TestPerformanceMonitor:
    """Test PerformanceMonitor functionality."""

    def test_track_operation(self):
        """Test tracking operation performance."""
        monitor = PerformanceMonitor()

        # Track an operation
        with monitor.track_operation("test_op"):
            time.sleep(0.01)  # Simulate work

        stats = monitor.get_stats()
        assert "test_op" in stats["operations"]
        assert stats["operations"]["test_op"]["count"] == 1
        assert stats["operations"]["test_op"]["avg_ms"] > 0

    def test_multiple_operations(self):
        """Test tracking multiple distinct operations."""
        monitor = PerformanceMonitor()

        for _ in range(5):
            with monitor.track_operation("op1"):
                time.sleep(0.001)

        for _ in range(3):
            with monitor.track_operation("op2"):
                time.sleep(0.005)

        stats = monitor.get_stats()
        assert stats["operations"]["op1"]["count"] == 5
        assert stats["operations"]["op2"]["count"] == 3
        # Both ops have recorded positive durations
        assert stats["operations"]["op1"]["avg_ms"] > 0
        assert stats["operations"]["op2"]["avg_ms"] > 0
        # op2 sleeps 5x longer, so its average should be higher
        assert stats["operations"]["op2"]["avg_ms"] > stats["operations"]["op1"]["avg_ms"]


class TestPerformanceManager:
    """Test PerformanceManager functionality."""

    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = Mock(spec=OdooConfig)
        config.url = os.getenv("ODOO_URL", "http://localhost:8069")
        return config

    def test_performance_manager_creation(self, mock_config):
        """Test creating performance manager."""
        manager = PerformanceManager(mock_config)

        assert manager.config == mock_config
        assert manager.field_cache is not None
        assert manager.record_cache is not None
        assert manager.permission_cache is not None
        assert manager.connection_pool is not None
        assert manager.request_optimizer is not None
        assert manager.monitor is not None

    def test_cache_key_generation(self, mock_config):
        """Test cache key generation for various parameter combinations."""
        manager = PerformanceManager(mock_config)

        # Simple key
        key = manager.cache_key("test", model="res.partner", id=1)
        assert key == "test:id:1:model:res.partner"

        # Complex key with list
        key = manager.cache_key("test", fields=["name", "email"], model="res.partner")
        assert "model:res.partner" in key
        assert "fields:" in key

        # Key with fields=None should be matchable by invalidation pattern
        key_none = manager.cache_key("record", model="res.partner", id=1, fields=None)
        assert "record:" in key_none
        assert "model:res.partner" in key_none
        assert "id:1" in key_none

    def test_field_caching(self, mock_config):
        """Test field definition caching."""
        manager = PerformanceManager(mock_config)

        fields = {
            "name": {"type": "char", "string": "Name"},
            "email": {"type": "char", "string": "Email"},
        }

        # Cache fields
        manager.cache_fields("res.partner", fields)

        # Get cached fields
        cached = manager.get_cached_fields("res.partner")
        assert cached == fields

    def test_record_caching(self, mock_config):
        """Test record caching."""
        manager = PerformanceManager(mock_config)

        record = {"id": 1, "name": "Test Partner", "email": "test@example.com"}

        # Cache record
        manager.cache_record("res.partner", record, fields=["name", "email"])

        # Get cached record
        cached = manager.get_cached_record("res.partner", 1, fields=["name", "email"])
        assert cached == record

    def test_record_cache_invalidation(self, mock_config):
        """Test record cache invalidation."""
        manager = PerformanceManager(mock_config)

        # Cache some records with same fields parameter as when retrieving
        manager.cache_record("res.partner", {"id": 1, "name": "Partner 1"}, fields=None)
        manager.cache_record("res.partner", {"id": 2, "name": "Partner 2"}, fields=None)
        manager.cache_record("res.users", {"id": 1, "name": "User 1"}, fields=None)

        # Verify they're cached
        assert manager.get_cached_record("res.partner", 1, fields=None) is not None
        assert manager.get_cached_record("res.partner", 2, fields=None) is not None

        # Invalidate specific record
        manager.invalidate_record_cache("res.partner", 1)
        assert manager.get_cached_record("res.partner", 1, fields=None) is None
        assert manager.get_cached_record("res.partner", 2, fields=None) is not None

        # Invalidate all partner records
        manager.invalidate_record_cache("res.partner")
        assert manager.get_cached_record("res.partner", 2, fields=None) is None
        assert manager.get_cached_record("res.users", 1, fields=None) is not None

    def test_permission_caching(self, mock_config):
        """Test permission caching."""
        manager = PerformanceManager(mock_config)

        # Cache permission
        manager.cache_permission("res.partner", "read", user_id=2, allowed=True)

        # Get cached permission
        cached = manager.get_cached_permission("res.partner", "read", user_id=2)
        assert cached is True

    def test_get_comprehensive_stats(self, mock_config):
        """Test getting comprehensive performance stats."""
        manager = PerformanceManager(mock_config)

        # Do some operations
        manager.cache_fields("res.partner", {"name": {"type": "char"}})
        manager.cache_record("res.partner", {"id": 1, "name": "Test"})
        manager.cache_permission("res.partner", "read", 2, True)

        with manager.monitor.track_operation("test_op"):
            time.sleep(0.001)

        # Get stats
        stats = manager.get_stats()

        assert "caches" in stats
        assert "field_cache" in stats["caches"]
        assert "record_cache" in stats["caches"]
        assert "permission_cache" in stats["caches"]
        assert "connection_pool" in stats
        assert "performance" in stats

    def test_clear_all_caches(self, mock_config):
        """Test clearing all caches."""
        manager = PerformanceManager(mock_config)

        # Add data to caches
        manager.cache_fields("res.partner", {"name": {"type": "char"}})
        manager.cache_record("res.partner", {"id": 1, "name": "Test"})
        manager.cache_permission("res.partner", "read", 2, True)

        # Clear all
        manager.clear_all_caches()

        # Verify all caches are empty
        assert manager.get_cached_fields("res.partner") is None
        assert manager.get_cached_record("res.partner", 1) is None
        assert manager.get_cached_permission("res.partner", "read", 2) is None

    def test_optimize_search_fields(self, mock_config):
        """Test optimize_search_fields returns optimized fields and tracks usage."""
        manager = PerformanceManager(mock_config)

        # Pre-populate field usage so optimizer has data
        manager.request_optimizer.track_field_usage("res.partner", ["name", "email"])
        manager.request_optimizer.track_field_usage("res.partner", ["name", "phone"])

        # Call optimize_search_fields with no explicit fields
        result = manager.optimize_search_fields("res.partner")

        # Should return fields ranked by usage: name (2), email (1), phone (1)
        assert "name" in result
        assert "email" in result
        assert "phone" in result

        # Verify side effect: usage counts increased from the internal track call
        usage = manager.request_optimizer._field_usage["res.partner"]
        # name was tracked 2 + 1 (from optimize call) = 3
        assert usage["name"] == 3
        # email was tracked 1 + 1 = 2
        assert usage["email"] == 2


class TestPerformanceIntegration:
    """Integration tests for performance features."""

    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = Mock(spec=OdooConfig)
        config.url = os.getenv("ODOO_URL", "http://localhost:8069")
        return config

    @pytest.mark.asyncio
    async def test_concurrent_cache_access(self, mock_config):
        """Test cache with concurrent access."""
        manager = PerformanceManager(mock_config)

        async def cache_operation(i):
            """Perform cache operations."""
            # Write
            manager.cache_record("res.partner", {"id": i, "name": f"Partner {i}"}, fields=None)

            # Read
            for _ in range(10):
                record = manager.get_cached_record("res.partner", i, fields=None)
                assert record is not None
                await asyncio.sleep(0.001)

        # Run concurrent operations (start from 1 to avoid ID 0)
        tasks = [cache_operation(i) for i in range(1, 11)]
        await asyncio.gather(*tasks)

        # Check stats
        stats = manager.record_cache.get_stats()
        assert stats["hits"] >= 90  # At least 90 hits from 10 tasks * 10 reads
        assert stats["total_entries"] == 10

    def test_performance_under_load(self, mock_config):
        """Test performance manager under load."""
        manager = PerformanceManager(mock_config)

        start_time = time.time()

        # Simulate heavy usage
        for i in range(1000):
            # Cache operations
            manager.cache_record("res.partner", {"id": i, "name": f"Partner {i}"})

            # Some cache hits
            if i > 100:
                manager.get_cached_record("res.partner", i - 100)

            # Track operations
            with manager.monitor.track_operation(f"op_{i % 10}"):
                time.sleep(0.0001)

        duration = time.time() - start_time

        # Should complete reasonably fast
        assert duration < 10.0  # 10 seconds for 1000 operations (generous for CI)

        # Check cache is working
        stats = manager.record_cache.get_stats()
        assert stats["hit_rate"] > 0.8  # Good hit rate
