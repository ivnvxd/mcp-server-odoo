"""Tests for performance optimization module."""

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

    def test_cache_memory_budget_enforced(self):
        """Total cached bytes never exceed max_memory_mb (loops evictions)."""
        cache = Cache(max_size=100, max_memory_mb=1)
        budget = 1024 * 1024

        # Many small entries, then large ones — one-shot eviction would
        # let the total drift far past the budget
        for i in range(60):
            cache.put(f"small{i}", "x" * 100)
        for i in range(10):
            cache.put(f"large{i}", "y" * 200_000)

        stats = cache.get_stats()
        assert stats["total_size_mb"] <= budget / (1024 * 1024)

    def test_cache_rejects_value_larger_than_budget(self):
        """A single value above the whole budget is not cached."""
        cache = Cache(max_size=10, max_memory_mb=1)

        cache.put("huge", "z" * (2 * 1024 * 1024))

        assert cache.get("huge") is None
        assert cache.get_stats()["total_entries"] == 0

    def test_cache_update_existing_key_evicts_nothing_else(self):
        """Replacing a key in a full cache must not evict an unrelated entry."""
        cache = Cache(max_size=3)
        cache.put("a", "1")
        cache.put("b", "2")
        cache.put("c", "3")

        cache.put("a", "1-updated")

        assert cache.get("a") == "1-updated"
        assert cache.get("b") == "2"
        assert cache.get("c") == "3"
        assert cache.get_stats()["total_entries"] == 3

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

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_get_connection(self, mock_proxy, mock_config):
        """Test getting connections from the factory."""
        pool = ConnectionPool(mock_config)

        # Get a connection
        pool.get_connection("/xmlrpc/2/common")

        # Should create a new connection
        mock_proxy.assert_called_once()
        stats = pool.get_stats()
        assert stats["connections_created"] == 1

        # Get same endpoint again (always creates a new proxy)
        pool.get_connection("/xmlrpc/2/common")

        assert mock_proxy.call_count == 2
        stats = pool.get_stats()
        assert stats["connections_created"] == 2

    def test_odoo_transport_sets_connection_timeout_http(self):
        """OdooTransport applies the configured timeout to its HTTP connections."""
        from mcp_server_odoo.performance import OdooTransport

        transport = OdooTransport(database="mydb", timeout=15)
        conn = transport.make_connection("localhost:8069")
        assert conn.timeout == 15

    def test_odoo_transport_sets_connection_timeout_https(self):
        """OdooSafeTransport applies the configured timeout to its HTTPS connections."""
        from mcp_server_odoo.performance import OdooSafeTransport

        transport = OdooSafeTransport(timeout=7)
        conn = transport.make_connection("example.com")
        assert conn.timeout == 7

    def test_odoo_transport_no_timeout_keeps_default(self):
        """Without an explicit timeout, the connection default is untouched."""
        import socket as socket_mod

        from mcp_server_odoo.performance import OdooTransport

        transport = OdooTransport()
        conn = transport.make_connection("localhost:8069")
        assert conn.timeout is socket_mod._GLOBAL_DEFAULT_TIMEOUT

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_pool_passes_timeout_to_transports(self, mock_proxy, mock_config):
        """ConnectionPool propagates its timeout to every transport it creates."""
        mock_config.url = "http://localhost:8069"
        pool = ConnectionPool(mock_config, timeout=12)

        pool.get_connection("/xmlrpc/2/common")

        transport = mock_proxy.call_args.kwargs["transport"]
        assert transport.timeout == 12

    def test_odoo_transport_sends_database_header(self, mock_config):
        """Test OdooTransport injects X-Odoo-Database header via send_headers."""
        from mcp_server_odoo.performance import OdooTransport

        transport = OdooTransport(database="mydb")
        mock_connection = Mock()

        # Call send_headers with empty headers list
        transport.send_headers(mock_connection, [])

        # Verify that putheader was called with the database header
        mock_connection.putheader.assert_called_with("X-Odoo-Database", "mydb")

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_each_proxy_gets_distinct_transport_http(self, mock_proxy, mock_config):
        """Each ServerProxy must get its own transport instance."""
        from mcp_server_odoo.performance import OdooTransport

        mock_config.url = "http://localhost:8069"
        pool = ConnectionPool(mock_config)

        pool.get_connection("/xmlrpc/2/common")
        pool.get_connection("/xmlrpc/2/object")

        # Two distinct endpoints → two ServerProxy calls with distinct transports
        assert mock_proxy.call_count == 2
        t1 = mock_proxy.call_args_list[0].kwargs["transport"]
        t2 = mock_proxy.call_args_list[1].kwargs["transport"]
        assert t1 is not t2
        assert isinstance(t1, OdooTransport)
        assert isinstance(t2, OdooTransport)

        # Same endpoint again → a new proxy with its own transport
        pool.get_connection("/xmlrpc/2/common")
        assert mock_proxy.call_count == 3
        t3 = mock_proxy.call_args_list[2].kwargs["transport"]
        assert t3 is not t1

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_each_proxy_gets_distinct_transport_https(self, mock_proxy, mock_config):
        """HTTPS scheme must produce distinct OdooSafeTransport per proxy."""
        from mcp_server_odoo.performance import OdooSafeTransport

        mock_config.url = "https://example.com"
        pool = ConnectionPool(mock_config)

        pool.get_connection("/xmlrpc/2/common")
        pool.get_connection("/xmlrpc/2/object")

        assert mock_proxy.call_count == 2
        t1 = mock_proxy.call_args_list[0].kwargs["transport"]
        t2 = mock_proxy.call_args_list[1].kwargs["transport"]
        assert t1 is not t2
        assert isinstance(t1, OdooSafeTransport)
        assert isinstance(t2, OdooSafeTransport)

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_per_proxy_transport_inherits_database(self, mock_proxy, mock_config):
        """After set_database, new proxies get transports with the new database."""
        mock_config.url = "http://localhost:8069"
        pool = ConnectionPool(mock_config)

        pool.set_database("mydb")
        pool.get_connection("/xmlrpc/2/object")

        transport = mock_proxy.call_args_list[0].kwargs["transport"]
        assert transport.database == "mydb"


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
        # Both ops have recorded positive durations. (No cross-operation
        # avg comparison: sleep() oversleep on loaded CI runners made the
        # ordering assertion flaky.)
        assert stats["operations"]["op1"]["avg_ms"] > 0
        assert stats["operations"]["op2"]["avg_ms"] > 0


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
        assert manager.connection_pool is not None
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

    def test_get_comprehensive_stats(self, mock_config):
        """Test getting comprehensive performance stats."""
        manager = PerformanceManager(mock_config)

        # Do some operations
        manager.cache_fields("res.partner", {"name": {"type": "char"}})

        with manager.monitor.track_operation("test_op"):
            time.sleep(0.001)

        # Get stats
        stats = manager.get_stats()

        assert "caches" in stats
        assert "field_cache" in stats["caches"]
        assert "connection_pool" in stats
        assert "performance" in stats

    def test_clear_all_caches(self, mock_config):
        """Test clearing all caches."""
        manager = PerformanceManager(mock_config)

        # Add data to caches
        manager.cache_fields("res.partner", {"name": {"type": "char"}})

        # Clear all
        manager.clear_all_caches()

        # Verify all caches are empty
        assert manager.get_cached_fields("res.partner") is None


class TestSharedSSLContext:
    """HTTPS transports from one factory share a single ssl.SSLContext."""

    @pytest.fixture
    def mock_config(self):
        config = Mock(spec=OdooConfig)
        config.url = "https://example.com"
        return config

    @patch("mcp_server_odoo.performance.ServerProxy")
    def test_safe_transports_share_one_context(self, mock_proxy, mock_config):
        """Avoids per-transport context construction and lets TLS session
        tickets ride between the per-proxy keepalive sockets."""
        pool = ConnectionPool(mock_config)

        pool.get_connection("/xmlrpc/2/common")
        pool.get_connection("/xmlrpc/2/object")
        pool.get_connection("/xmlrpc/db")

        transports = [call.kwargs["transport"] for call in mock_proxy.call_args_list]
        assert len(transports) == 3
        assert pool._ssl_context is not None
        for transport in transports:
            assert transport.context is pool._ssl_context

    def test_http_pool_has_no_ssl_context(self, mock_config):
        """HTTP factories must not allocate an SSL context."""
        mock_config.url = "http://localhost:8069"
        pool = ConnectionPool(mock_config)
        assert pool._ssl_context is None


class TestDeadKeepaliveRecovery:
    """Half-open keepalive handling at the transport layer (issue #68).

    Pure unit-level coverage — tiny in-process TCP servers, no live Odoo.
    """

    @staticmethod
    def _silent_server():
        """TCP server that accepts, drains the request, and never responds."""
        import socket as socket_mod
        import threading

        server = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
        server.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        stop = threading.Event()
        connections = []

        def accept_loop():
            server.settimeout(0.5)
            while not stop.is_set():
                try:
                    sock, _ = server.accept()
                except socket_mod.timeout:
                    continue
                except OSError:
                    return
                connections.append(sock)
                try:
                    sock.settimeout(0.5)
                    sock.recv(4096)
                except (socket_mod.timeout, OSError):
                    pass

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        return server, port, stop, connections, thread

    def test_fresh_connection_timeout_not_retried(self):
        """A timeout on a freshly-opened connection (no cached keepalive
        socket) must raise after ONE attempt — retrying a genuinely slow
        server would double the wait and could double-submit writes."""
        from mcp_server_odoo.performance import OdooTransport

        server, port, stop, connections, thread = self._silent_server()
        try:
            transport = OdooTransport(timeout=1.0)
            t0 = time.monotonic()
            with pytest.raises((TimeoutError, OSError)):
                transport.request(
                    f"127.0.0.1:{port}",
                    "/xmlrpc/2/common",
                    b"<?xml version='1.0'?><methodCall><methodName>x</methodName></methodCall>",
                )
            elapsed = time.monotonic() - t0

            assert elapsed < 2.5, f"took {elapsed:.2f}s — looks like a doubled (retried) wait"
            assert len(connections) == 1, "fresh-connection timeout must not open a retry socket"
        finally:
            stop.set()
            for sock in connections:
                try:
                    sock.close()
                except OSError:
                    pass
            server.close()
            thread.join(timeout=2.0)

    def test_half_open_keepalive_triggers_transparent_retry(self):
        """The actual issue #68 mechanism: request 1 succeeds and the server
        keeps the connection alive; the server then goes silent (half-open);
        request 2 times out on the REUSED socket and must be retried once on
        a fresh connection, succeeding transparently."""
        import socket as socket_mod
        import threading

        from mcp_server_odoo.performance import OdooTransport

        body = (
            b'<?xml version="1.0"?>\n<methodResponse>\n<params>\n<param>\n'
            b"<value><int>1</int></value>\n</param>\n</params>\n</methodResponse>\n"
        )
        keepalive_response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: keep-alive\r\n\r\n"
            + body
        )

        server = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
        server.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        stop = threading.Event()
        connections_seen = [0]

        def drain_one_request(sock):
            buf = b""
            sock.settimeout(2.0)
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    return False
                buf += chunk
            # Drain the body (Content-Length from headers)
            headers = buf.split(b"\r\n\r\n", 1)[0].lower()
            for line in headers.split(b"\r\n"):
                if line.startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
                    have = len(buf.split(b"\r\n\r\n", 1)[1])
                    while have < length:
                        chunk = sock.recv(4096)
                        if not chunk:
                            return False
                        have += len(chunk)
            return True

        def handle_first(sock):
            # First connection: answer request 1 with keep-alive, then GO
            # SILENT — drain request 2 but never respond (half-open from
            # the client's perspective).
            try:
                if drain_one_request(sock):
                    sock.sendall(keepalive_response)
                drain_one_request(sock)  # request 2 — swallow it
                stop.wait(10)
            except OSError:
                pass

        def handle_retry(sock):
            # Retry connection: respond normally.
            try:
                if drain_one_request(sock):
                    sock.sendall(keepalive_response)
            except OSError:
                pass
            finally:
                try:
                    sock.close()
                except OSError:
                    pass

        def accept_loop():
            server.settimeout(0.5)
            while not stop.is_set():
                try:
                    sock, _ = server.accept()
                except socket_mod.timeout:
                    continue
                except OSError:
                    return
                connections_seen[0] += 1
                # Each connection gets its own thread: connection 1 blocks
                # in stop.wait() while the retry's connection 2 must be
                # accepted and served concurrently.
                handler = handle_first if connections_seen[0] == 1 else handle_retry
                threading.Thread(target=handler, args=(sock,), daemon=True).start()

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()

        request_body = b"<?xml version='1.0'?><methodCall><methodName>x</methodName></methodCall>"
        try:
            transport = OdooTransport(timeout=1.5)
            host = f"127.0.0.1:{port}"

            # Request 1: primes the keepalive socket
            assert transport.request(host, "/x", request_body) == (1,)
            cached = transport._connection[1]
            assert cached is not None and cached.sock is not None, (
                "expected a live cached keepalive socket after request 1"
            )

            # Request 2: half-open — must transparently retry and succeed
            t0 = time.monotonic()
            result = transport.request(host, "/x", request_body)
            elapsed = time.monotonic() - t0

            assert result == (1,)
            assert connections_seen[0] == 2, "retry must open a fresh connection"
            # Bounded: ~timeout (dead read) + fast retry
            assert 1.0 <= elapsed < 4.0, f"took {elapsed:.2f}s, expected ~1.5s + retry"
        finally:
            stop.set()
            server.close()
            thread.join(timeout=2.0)

    def test_write_timeout_on_reused_keepalive_not_retried(self):
        """A timed-out call marked non-retry-safe (create/write/unlink/...)
        must NOT be re-sent over a reused keepalive socket — the server may
        still be processing the first attempt, and re-sending would
        double-execute the write."""
        import socket as socket_mod
        import threading

        from mcp_server_odoo.performance import OdooTransport

        body = (
            b'<?xml version="1.0"?>\n<methodResponse>\n<params>\n<param>\n'
            b"<value><int>1</int></value>\n</param>\n</params>\n</methodResponse>\n"
        )
        keepalive_response = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: keep-alive\r\n\r\n"
            + body
        )

        server = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
        server.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        stop = threading.Event()
        connections_seen = [0]

        def handle_connection(sock):
            # Answer request 1 with keep-alive, then GO SILENT — request 2
            # times out on the reused socket and must NOT be retried.
            try:
                sock.settimeout(2.0)
                buf = b""
                while b"</methodCall>" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                sock.sendall(keepalive_response)
                stop.wait(10)
            except OSError:
                pass

        def accept_loop():
            server.settimeout(0.5)
            while not stop.is_set():
                try:
                    sock, _ = server.accept()
                except socket_mod.timeout:
                    continue
                except OSError:
                    return
                connections_seen[0] += 1
                threading.Thread(target=handle_connection, args=(sock,), daemon=True).start()

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()

        request_body = b"<?xml version='1.0'?><methodCall><methodName>x</methodName></methodCall>"
        try:
            transport = OdooTransport(timeout=1.5)
            host = f"127.0.0.1:{port}"

            # Request 1: primes the keepalive socket
            assert transport.request(host, "/x", request_body) == (1,)
            cached = transport._connection[1]
            assert cached is not None and cached.sock is not None, (
                "expected a live cached keepalive socket after request 1"
            )

            # Request 2: marked as a write — must raise, never re-send
            transport.timeout_retry_safe = False
            t0 = time.monotonic()
            with pytest.raises(TimeoutError):
                transport.request(host, "/x", request_body)
            elapsed = time.monotonic() - t0

            assert connections_seen[0] == 1, "a timed-out write must not open a retry connection"
            assert elapsed < 3.0, f"took {elapsed:.2f}s — looks like a doubled (retried) wait"
        finally:
            stop.set()
            server.close()
            thread.join(timeout=2.0)
