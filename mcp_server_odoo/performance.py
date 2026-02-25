"""Performance optimization and caching for Odoo MCP Server.

This module provides performance optimizations including:
- Connection pooling and reuse
- Intelligent response caching
- Request batching and optimization
- Performance monitoring and metrics
"""

import json
import threading
import time
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from xmlrpc.client import SafeTransport, ServerProxy, Transport

from .config import OdooConfig
from .logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class CacheEntry:
    """Represents a cached item with metadata."""

    key: str
    value: Any
    created_at: datetime
    accessed_at: datetime
    ttl_seconds: int
    hit_count: int = 0
    size_bytes: int = 0

    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        age = datetime.now() - self.created_at
        return age.total_seconds() > self.ttl_seconds

    def access(self):
        """Update access metadata."""
        self.accessed_at = datetime.now()
        self.hit_count += 1


@dataclass
class CacheStats:
    """Cache performance statistics."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expired_evictions: int = 0
    size_evictions: int = 0
    total_entries: int = 0
    total_size_bytes: int = 0

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def record_hit(self):
        """Record a cache hit."""
        self.hits += 1

    def record_miss(self):
        """Record a cache miss."""
        self.misses += 1

    def record_eviction(self, reason: str = "manual"):
        """Record a cache eviction."""
        self.evictions += 1
        if reason == "expired":
            self.expired_evictions += 1
        elif reason == "size":
            self.size_evictions += 1


class Cache:
    """Thread-safe LRU cache with TTL support."""

    def __init__(self, max_size: int = 1000, max_memory_mb: int = 100):
        """Initialize cache.

        Args:
            max_size: Maximum number of entries
            max_memory_mb: Maximum memory usage in MB
        """
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._max_size = max_size
        self._max_memory_bytes = max_memory_mb * 1024 * 1024
        self._stats = CacheStats()

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._stats.record_miss()
                return None

            if entry.is_expired():
                self._remove(key, reason="expired")
                self._stats.record_miss()
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.access()
            self._stats.record_hit()
            return entry.value

    def put(self, key: str, value: Any, ttl_seconds: int = 300):
        """Put value in cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Time to live in seconds
        """
        with self._lock:
            # Calculate size (rough estimate)
            size_bytes = len(json.dumps(value, default=str).encode())

            # Check memory limit
            if self._stats.total_size_bytes + size_bytes > self._max_memory_bytes:
                self._evict_lru(reason="size")

            # Check size limit
            while len(self._cache) >= self._max_size:
                self._evict_lru(reason="size")

            # Add or update entry
            now = datetime.now()
            if key in self._cache:
                old_size = self._cache[key].size_bytes
                self._stats.total_size_bytes -= old_size

            entry = CacheEntry(
                key=key,
                value=value,
                created_at=now,
                accessed_at=now,
                ttl_seconds=ttl_seconds,
                size_bytes=size_bytes,
            )

            self._cache[key] = entry
            self._cache.move_to_end(key)
            self._stats.total_entries = len(self._cache)
            self._stats.total_size_bytes += size_bytes

    def invalidate(self, key: str) -> bool:
        """Invalidate a cache entry.

        Args:
            key: Cache key

        Returns:
            True if entry was removed, False if not found
        """
        with self._lock:
            return self._remove(key, reason="manual")

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all entries matching pattern.

        Args:
            pattern: Pattern to match (e.g., "model:res.partner:*")

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            count = 0
            keys_to_remove = []

            # Enhanced pattern matching with * wildcard
            if "*" in pattern:
                # Handle patterns with wildcards
                parts = pattern.split("*")
                keys_to_remove = []
                for k in self._cache.keys():
                    # Check if all non-wildcard parts are in the key in order
                    key_matches = True
                    search_from = 0
                    for part in parts:
                        if part:  # Skip empty parts from consecutive wildcards
                            idx = k.find(part, search_from)
                            if idx == -1:
                                key_matches = False
                                break
                            search_from = idx + len(part)
                    if key_matches:
                        keys_to_remove.append(k)
            else:
                if pattern in self._cache:
                    keys_to_remove = [pattern]

            for key in keys_to_remove:
                if self._remove(key, reason="manual"):
                    count += 1

            return count

    def clear(self):
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            self._stats = CacheStats()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            return {
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "hit_rate": round(self._stats.hit_rate, 3),
                "evictions": self._stats.evictions,
                "expired_evictions": self._stats.expired_evictions,
                "size_evictions": self._stats.size_evictions,
                "total_entries": self._stats.total_entries,
                "total_size_mb": round(self._stats.total_size_bytes / (1024 * 1024), 2),
                "max_size": self._max_size,
                "max_memory_mb": self._max_memory_bytes / (1024 * 1024),
            }

    def _remove(self, key: str, reason: str = "manual") -> bool:
        """Remove entry from cache."""
        if key in self._cache:
            entry = self._cache.pop(key)
            self._stats.total_size_bytes -= entry.size_bytes
            self._stats.total_entries = len(self._cache)
            self._stats.record_eviction(reason)
            return True
        return False

    def _evict_lru(self, reason: str = "size"):
        """Evict least recently used entry."""
        if self._cache:
            # OrderedDict maintains order, first item is LRU
            key = next(iter(self._cache))
            self._remove(key, reason)


class OdooTransport(Transport):
    """HTTP transport that injects X-Odoo-Database header for multi-DB routing."""

    def __init__(self, database: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.database = database

    def send_headers(self, connection, headers):
        super().send_headers(connection, headers)
        if self.database:
            connection.putheader("X-Odoo-Database", self.database)


class OdooSafeTransport(SafeTransport):
    """HTTPS transport that injects X-Odoo-Database header for multi-DB routing."""

    def __init__(self, database: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.database = database

    def send_headers(self, connection, headers):
        super().send_headers(connection, headers)
        if self.database:
            connection.putheader("X-Odoo-Database", self.database)


class ConnectionPool:
    """Thread-safe connection pool for XML-RPC connections."""

    def __init__(self, config: OdooConfig, max_connections: int = 10):
        """Initialize connection pool.

        Args:
            config: Odoo configuration
            max_connections: Maximum number of connections
        """
        self.config = config
        self.max_connections = max_connections
        self._connections: List[Tuple[ServerProxy, float]] = []
        self._endpoint_map: List[str] = []  # Track endpoints for each connection
        self._lock = threading.RLock()
        # Use OdooSafeTransport/OdooTransport to support X-Odoo-Database header
        if config.url.startswith("https://"):
            self._transport: Union[OdooTransport, OdooSafeTransport] = OdooSafeTransport()
        else:
            self._transport = OdooTransport()
        self._last_cleanup = time.time()
        self._stats = {
            "connections_created": 0,
            "connections_reused": 0,
            "connections_closed": 0,
            "active_connections": 0,
        }

    def get_connection(self, endpoint: str) -> ServerProxy:
        """Get a connection from the pool.

        Args:
            endpoint: The endpoint path (e.g., '/xmlrpc/2/common')

        Returns:
            ServerProxy connection
        """
        with self._lock:
            now = time.time()

            # Cleanup stale connections periodically
            if now - self._last_cleanup > 60:  # Every minute
                self._cleanup_stale_connections()
                self._last_cleanup = now

            # Try to find an existing connection
            url = f"{self.config.url}{endpoint}"
            for i, (conn, last_used) in enumerate(self._connections):
                # Store endpoint with connection for matching
                if i < len(self._endpoint_map) and self._endpoint_map[i] == endpoint:
                    # Connection is still fresh (used within last 5 minutes)
                    if now - last_used < 300:
                        self._connections[i] = (conn, now)
                        self._stats["connections_reused"] += 1
                        logger.debug(f"Reusing connection for {endpoint}")
                        return conn
                    else:
                        # Connection is stale, remove it
                        self._connections.pop(i)
                        self._endpoint_map.pop(i)
                        self._stats["connections_closed"] += 1
                        break

            # Create new connection
            if len(self._connections) >= self.max_connections:
                # Remove oldest connection
                self._connections.pop(0)
                self._endpoint_map.pop(0)
                self._stats["connections_closed"] += 1

            conn = ServerProxy(url, transport=self._transport, allow_none=True)
            self._connections.append((conn, now))
            self._endpoint_map.append(endpoint)
            self._stats["connections_created"] += 1
            self._stats["active_connections"] = len(self._connections)
            logger.debug(f"Created new connection for {endpoint}")
            return conn

    def _cleanup_stale_connections(self):
        """Remove stale connections from pool."""
        now = time.time()
        initial_count = len(self._connections)

        # Remove connections older than 5 minutes
        new_connections = []
        new_endpoints = []
        for i, (conn, last_used) in enumerate(self._connections):
            if now - last_used < 300:
                new_connections.append((conn, last_used))
                new_endpoints.append(self._endpoint_map[i])

        self._connections = new_connections
        self._endpoint_map = new_endpoints

        removed = initial_count - len(self._connections)
        if removed > 0:
            self._stats["connections_closed"] += removed
            self._stats["active_connections"] = len(self._connections)
            logger.debug(f"Cleaned up {removed} stale connections")

    def get_stats(self) -> Dict[str, Any]:
        """Get connection pool statistics."""
        with self._lock:
            return self._stats.copy()

    def set_database(self, db_name: str) -> None:
        """Set the database for X-Odoo-Database header and invalidate existing connections.

        Args:
            db_name: Database name to send in the header
        """
        with self._lock:
            self._transport.database = db_name
            # Invalidate existing connections — they were created without the header
            self._stats["connections_closed"] += len(self._connections)
            self._connections.clear()
            self._endpoint_map.clear()
            self._stats["active_connections"] = 0
            logger.debug(f"Set database header to '{db_name}', cleared connection pool")

    def clear(self):
        """Clear all connections."""
        with self._lock:
            self._stats["connections_closed"] += len(self._connections)
            self._connections.clear()
            self._endpoint_map.clear()
            self._stats["active_connections"] = 0


class RequestOptimizer:
    """Optimizes Odoo requests for better performance."""

    def __init__(self):
        """Initialize request optimizer."""
        self._batch_queue: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._field_usage: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._lock = threading.RLock()

    def track_field_usage(self, model: str, fields: List[str]):
        """Track which fields are commonly requested.

        Args:
            model: Model name
            fields: List of field names
        """
        with self._lock:
            for field in fields:
                self._field_usage[model][field] += 1

    def get_optimized_fields(self, model: str, requested_fields: Optional[List[str]]) -> List[str]:
        """Get optimized field list based on usage patterns.

        Args:
            model: Model name
            requested_fields: Explicitly requested fields

        Returns:
            Optimized field list
        """
        if requested_fields:
            return requested_fields

        with self._lock:
            usage = self._field_usage.get(model, {})
            if not usage:
                # Return common fields if no usage data
                # Use only universally available fields (not all models have 'name')
                return ["id", "display_name"]

            # Get top 20 most used fields
            sorted_fields = sorted(usage.items(), key=lambda x: x[1], reverse=True)
            return [field for field, _ in sorted_fields[:20]]

    def should_batch_request(self, model: str, operation: str, size: int) -> bool:
        """Determine if request should be batched.

        Args:
            model: Model name
            operation: Operation type (read, search, etc.)
            size: Number of records

        Returns:
            True if request should be batched
        """
        # Batch if requesting many records
        if operation == "read" and size > 50:
            return True

        # Batch if multiple small requests for same model
        with self._lock:
            queue_size = len(self._batch_queue.get(f"{model}:{operation}", []))
            return queue_size > 0

    def add_to_batch(self, model: str, operation: str, params: Dict[str, Any]):
        """Add request to batch queue.

        Args:
            model: Model name
            operation: Operation type
            params: Request parameters
        """
        with self._lock:
            key = f"{model}:{operation}"
            self._batch_queue[key].append(params)

    def get_batch(self, model: str, operation: str) -> List[Dict[str, Any]]:
        """Get and clear batch for processing.

        Args:
            model: Model name
            operation: Operation type

        Returns:
            List of batched requests
        """
        with self._lock:
            key = f"{model}:{operation}"
            batch = self._batch_queue[key]
            self._batch_queue[key] = []
            return batch


class PerformanceMonitor:
    """Monitors and tracks performance metrics."""

    def __init__(self):
        """Initialize performance monitor."""
        self._metrics: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.RLock()
        self._start_time = time.time()

    @contextmanager
    def track_operation(self, operation: str):
        """Context manager to track operation duration.

        Args:
            operation: Operation name
        """
        start = time.time()
        try:
            yield
        finally:
            duration = time.time() - start
            with self._lock:
                self._metrics[operation].append(duration)
                # Keep only last 1000 measurements
                if len(self._metrics[operation]) > 1000:
                    self._metrics[operation] = self._metrics[operation][-1000:]

    def get_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        with self._lock:
            stats: Dict[str, Any] = {
                "uptime_seconds": int(time.time() - self._start_time),
                "operations": {},
            }

            for operation, durations in self._metrics.items():
                if durations:
                    stats["operations"][operation] = {
                        "count": len(durations),
                        "avg_ms": round(sum(durations) / len(durations) * 1000, 2),
                        "min_ms": round(min(durations) * 1000, 2),
                        "max_ms": round(max(durations) * 1000, 2),
                        "last_ms": round(durations[-1] * 1000, 2),
                    }

            return stats


class PerformanceManager:
    """Central manager for all performance optimizations."""

    def __init__(self, config: OdooConfig):
        """Initialize performance manager.

        Args:
            config: Odoo configuration
        """
        self.config = config

        # Initialize components
        self.field_cache = Cache(max_size=100, max_memory_mb=10)
        self.record_cache = Cache(max_size=1000, max_memory_mb=50)
        self.permission_cache = Cache(max_size=500, max_memory_mb=5)
        self.connection_pool = ConnectionPool(config)
        self.request_optimizer = RequestOptimizer()
        self.monitor = PerformanceMonitor()

        logger.info("Performance manager initialized")

    def cache_key(self, prefix: str, **kwargs) -> str:
        """Generate cache key from parameters.

        Args:
            prefix: Key prefix
            **kwargs: Parameters to include in key

        Returns:
            Cache key string
        """
        # Sort kwargs for consistent keys
        sorted_items = sorted(kwargs.items())
        key_parts = [prefix]
        for k, v in sorted_items:
            if isinstance(v, (list, dict)):
                v = json.dumps(v, sort_keys=True)
            key_parts.append(f"{k}:{v}")
        return ":".join(key_parts)

    def get_cached_fields(self, model: str) -> Optional[Dict[str, Any]]:
        """Get cached field definitions.

        Args:
            model: Model name

        Returns:
            Cached fields or None
        """
        key = self.cache_key("fields", model=model)
        return self.field_cache.get(key)

    def cache_fields(self, model: str, fields: Dict[str, Any]):
        """Cache field definitions.

        Args:
            model: Model name
            fields: Field definitions
        """
        key = self.cache_key("fields", model=model)
        # Fields rarely change, cache for 1 hour
        self.field_cache.put(key, fields, ttl_seconds=3600)

    def get_cached_record(
        self, model: str, record_id: int, fields: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Get cached record.

        Args:
            model: Model name
            record_id: Record ID
            fields: Field list (for cache key)

        Returns:
            Cached record or None
        """
        key = self.cache_key("record", model=model, id=record_id, fields=fields)
        return self.record_cache.get(key)

    def cache_record(
        self,
        model: str,
        record: Dict[str, Any],
        fields: Optional[List[str]] = None,
        ttl_seconds: int = 300,
    ):
        """Cache record data.

        Args:
            model: Model name
            record: Record data
            fields: Field list (for cache key)
            ttl_seconds: Cache TTL
        """
        record_id = record.get("id")
        if record_id is not None:
            key = self.cache_key("record", model=model, id=record_id, fields=fields)
            self.record_cache.put(key, record, ttl_seconds=ttl_seconds)

    def invalidate_record_cache(self, model: str, record_id: Optional[int] = None):
        """Invalidate record cache.

        Args:
            model: Model name
            record_id: Specific record ID or None for all model records
        """
        if record_id:
            # Use wildcard pattern that will match any fields value
            pattern = f"record:*id:{record_id}:model:{model}*"
        else:
            pattern = f"record:*model:{model}*"

        count = self.record_cache.invalidate_pattern(pattern)
        if count > 0:
            logger.debug(f"Invalidated {count} cache entries for {pattern}")

    def get_cached_permission(self, model: str, operation: str, user_id: int) -> Optional[bool]:
        """Get cached permission check.

        Args:
            model: Model name
            operation: Operation type
            user_id: User ID

        Returns:
            Cached permission or None
        """
        key = self.cache_key("permission", model=model, operation=operation, user_id=user_id)
        return self.permission_cache.get(key)

    def cache_permission(self, model: str, operation: str, user_id: int, allowed: bool):
        """Cache permission check result.

        Args:
            model: Model name
            operation: Operation type
            user_id: User ID
            allowed: Permission result
        """
        key = self.cache_key("permission", model=model, operation=operation, user_id=user_id)
        # Permissions may change, cache for 5 minutes
        self.permission_cache.put(key, allowed, ttl_seconds=300)

    def get_optimized_connection(self, endpoint: str) -> Any:
        """Get optimized connection from pool.

        Args:
            endpoint: Endpoint path

        Returns:
            Connection object
        """
        with self.monitor.track_operation("connection_get"):
            return self.connection_pool.get_connection(endpoint)

    def optimize_search_fields(
        self, model: str, requested_fields: Optional[List[str]] = None
    ) -> List[str]:
        """Optimize field selection for search.

        Args:
            model: Model name
            requested_fields: Explicitly requested fields

        Returns:
            Optimized field list
        """
        optimized = self.request_optimizer.get_optimized_fields(model, requested_fields)
        self.request_optimizer.track_field_usage(model, optimized)
        return optimized

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive performance statistics."""
        return {
            "caches": {
                "field_cache": self.field_cache.get_stats(),
                "record_cache": self.record_cache.get_stats(),
                "permission_cache": self.permission_cache.get_stats(),
            },
            "connection_pool": self.connection_pool.get_stats(),
            "performance": self.monitor.get_stats(),
        }

    def set_database(self, db_name: str) -> None:
        """Set the database for X-Odoo-Database header on the connection pool.

        Args:
            db_name: Database name to send in the header
        """
        self.connection_pool.set_database(db_name)

    def clear_all_caches(self):
        """Clear all caches."""
        self.field_cache.clear()
        self.record_cache.clear()
        self.permission_cache.clear()
        logger.info("All caches cleared")
