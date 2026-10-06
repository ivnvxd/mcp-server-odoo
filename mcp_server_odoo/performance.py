"""Performance optimization and caching for Odoo MCP Server.

This module provides:
- Per-endpoint XML-RPC proxy creation and reuse (`ConnectionPool`)
- `fields_get` result caching
- Performance monitoring and metrics
"""

import errno
import http.client
import json
import ssl
import threading
import time
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
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

            # Refuse to cache values larger than the whole budget
            if size_bytes > self._max_memory_bytes:
                logger.debug(
                    f"Skipping cache for {key}: value ({size_bytes} bytes) "
                    f"exceeds cache budget ({self._max_memory_bytes} bytes)"
                )
                return

            # Replacing an existing key frees its size and doesn't change
            # the entry count — remove it first so the eviction checks
            # below don't evict an unrelated entry.
            if key in self._cache:
                old_size = self._cache.pop(key).size_bytes
                self._stats.total_size_bytes -= old_size

            # Enforce memory limit (loop: one LRU eviction may not free
            # enough for the incoming entry)
            while self._cache and (
                self._stats.total_size_bytes + size_bytes > self._max_memory_bytes
            ):
                self._evict_lru(reason="size")

            # Enforce entry-count limit
            while len(self._cache) >= self._max_size:
                self._evict_lru(reason="size")

            now = datetime.now()

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


# OSErrors the stdlib xmlrpc Transport treats as "cached connection went
# cold" and retries once on (see xmlrpc.client.Transport.request)
_RETRYABLE_ERRNOS = (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE)


def _retry_once_request(self, host, handler, request_body, verbose=False):
    """Like ``xmlrpc.client.Transport.request``, but also recovers from
    half-open keepalive sockets (issue #68).

    Stdlib retries once on ECONNRESET/ECONNABORTED/EPIPE/RemoteDisconnected —
    the cases where the peer closed the cached connection cleanly. But a load
    balancer that drops its upstream WITHOUT sending FIN/RST leaves the cached
    socket looking alive: the write succeeds into the dead socket's buffer and
    the read blocks until the socket timeout fires as ``TimeoutError``.

    We retry that case too — but ONLY when the timed-out attempt went over a
    REUSED keepalive socket. A timeout on a freshly-opened connection means
    the server is genuinely slow; retrying would double the wait and, worse,
    could double-submit a write the server is still processing.

    On a reused socket a half-open peer and a genuinely slow server are
    indistinguishable too, so the timeout retry is additionally gated on
    ``timeout_retry_safe``: OdooConnection.execute_kw sets it per call, and
    only read-only methods may be re-sent — a timed-out write could already
    be committing server-side and re-sending it would double-execute it.
    """
    for i in (0, 1):
        # Half-open detection: is there a cached connection with a live
        # socket about to be reused? (Fresh connections have sock=None
        # until the request is sent.)
        cached = getattr(self, "_connection", (None, None))[1]
        reused_keepalive = cached is not None and getattr(cached, "sock", None) is not None
        try:
            return self.single_request(host, handler, request_body, verbose)
        except TimeoutError:
            if i or not reused_keepalive or not getattr(self, "timeout_retry_safe", True):
                raise
            self.close()
        except http.client.RemoteDisconnected:
            if i:
                raise
            self.close()
        except OSError as e:
            if i or e.errno not in _RETRYABLE_ERRNOS:
                raise
            self.close()


class OdooTransport(Transport):
    """HTTP transport that injects X-Odoo-Database header for multi-DB routing
    and bounds socket I/O with a timeout."""

    # Set per call by OdooConnection.execute_kw (under the per-proxy lock):
    # only read-only methods may be re-sent after a keepalive timeout. The
    # True default means the common/db proxies always retry — those
    # endpoints carry only idempotent calls (authenticate/version/list).
    timeout_retry_safe: bool = True

    def __init__(self, database: Optional[str] = None, timeout: Optional[float] = None, **kwargs):
        super().__init__(**kwargs)
        self.database = database
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        if self.timeout is not None:
            connection.timeout = self.timeout
        return connection

    def send_headers(self, connection, headers):
        super().send_headers(connection, headers)
        if self.database:
            connection.putheader("X-Odoo-Database", self.database)

    request = _retry_once_request


class OdooSafeTransport(SafeTransport):
    """HTTPS transport that injects X-Odoo-Database header for multi-DB routing
    and bounds socket I/O with a timeout."""

    # See OdooTransport.timeout_retry_safe
    timeout_retry_safe: bool = True

    def __init__(
        self,
        database: Optional[str] = None,
        timeout: Optional[float] = None,
        context: Optional[ssl.SSLContext] = None,
        **kwargs,
    ):
        super().__init__(context=context, **kwargs)
        self.database = database
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        if self.timeout is not None:
            connection.timeout = self.timeout
        return connection

    def send_headers(self, connection, headers):
        super().send_headers(connection, headers)
        if self.database:
            connection.putheader("X-Odoo-Database", self.database)

    request = _retry_once_request


class ConnectionPool:
    """Per-endpoint ServerProxy factory.

    Despite the name this is a factory, not a pool: proxies are created
    at startup and held by OdooConnection for the server's lifetime.
    Each proxy gets its own transport (xmlrpc Transport keep-alive state
    races under concurrent calls) carrying the configured socket timeout
    and the X-Odoo-Database header.
    """

    def __init__(self, config: OdooConfig, timeout: int = 30):
        """Initialize the factory.

        Args:
            config: Odoo configuration
            timeout: Socket timeout in seconds applied to every connection
        """
        self.config = config
        self.timeout = timeout
        self._database: Optional[str] = None
        self._lock = threading.Lock()
        self._connections_created = 0
        # One shared context amortizes construction and lets TLS session
        # resumption work across sockets; never mutate it per-call.
        self._ssl_context: Optional[ssl.SSLContext] = (
            ssl.create_default_context() if config.url.startswith("https://") else None
        )

    def get_connection(self, endpoint: str) -> ServerProxy:
        """Create a ServerProxy for an endpoint.

        Args:
            endpoint: The endpoint path (e.g., '/xmlrpc/2/common')

        Returns:
            ServerProxy connection with its own transport
        """
        url = f"{self.config.url}{endpoint}"
        with self._lock:
            database = self._database
            self._connections_created += 1
        if self.config.url.startswith("https://"):
            transport: Union[OdooTransport, OdooSafeTransport] = OdooSafeTransport(
                database=database, timeout=self.timeout, context=self._ssl_context
            )
        else:
            transport = OdooTransport(database=database, timeout=self.timeout)
        logger.debug(f"Created new connection for {endpoint}")
        return ServerProxy(url, transport=transport, allow_none=True)

    def set_database(self, db_name: str) -> None:
        """Set the database for the X-Odoo-Database header on new connections.

        Args:
            db_name: Database name to send in the header
        """
        with self._lock:
            self._database = db_name
        logger.debug(f"Set database header to '{db_name}'")

    def get_stats(self) -> Dict[str, Any]:
        """Get connection factory statistics."""
        with self._lock:
            return {"connections_created": self._connections_created}


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

    def __init__(self, config: OdooConfig, timeout: int = 30):
        """Initialize performance manager.

        Args:
            config: Odoo configuration
            timeout: Socket timeout in seconds for pooled connections
        """
        self.config = config

        # Initialize components
        self.field_cache = Cache(max_size=100, max_memory_mb=10)
        self.connection_pool = ConnectionPool(config, timeout=timeout)
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

    def get_optimized_connection(self, endpoint: str) -> Any:
        """Get optimized connection from pool.

        Args:
            endpoint: Endpoint path

        Returns:
            Connection object
        """
        with self.monitor.track_operation("connection_get"):
            return self.connection_pool.get_connection(endpoint)

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive performance statistics."""
        return {
            "caches": {
                "field_cache": self.field_cache.get_stats(),
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
        logger.info("All caches cleared")
