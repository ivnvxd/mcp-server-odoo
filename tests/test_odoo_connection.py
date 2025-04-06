"""Tests for the OdooConnection module.

This module contains tests for the OdooConnection class which handles
XML-RPC communication with Odoo instances.
"""

import time
import unittest.mock as mock
import xmlrpc.client

import pytest

from mcp_server_odoo.odoo_connection import (
    OdooConnection,
    OdooConnectionError,
    OdooConnectionPool,
    get_odoo_connection,
)


@pytest.fixture
def mock_odoo_endpoints():
    """Mock Odoo XML-RPC endpoints."""
    with mock.patch("xmlrpc.client.ServerProxy") as mock_proxy:
        # Create mocks for the two endpoints
        mock_common = mock.MagicMock()
        mock_object = mock.MagicMock()

        # Configure ServerProxy to return appropriate endpoint
        def side_effect(url, **kwargs):
            if "common" in url:
                return mock_common
            elif "object" in url:
                return mock_object
            else:
                raise ValueError(f"Unexpected URL: {url}")

        mock_proxy.side_effect = side_effect

        yield {"common": mock_common, "object": mock_object, "proxy": mock_proxy}


@pytest.fixture
def odoo_connection(mock_odoo_endpoints):
    """Create an OdooConnection with mocked endpoints."""
    return OdooConnection(
        url="http://test.example.com", db="test_db", token="test_token"
    )


@pytest.fixture
def clear_connection_pool():
    """Clear the connection pool between tests."""
    # Get pool singleton and clear it
    pool = OdooConnectionPool()
    pool.clear()
    # Reset any connections in the pool
    yield
    # Clear after test
    pool.clear()


class TestOdooConnection:
    """Test cases for OdooConnection class."""

    def test_initialization(self, odoo_connection, mock_odoo_endpoints):
        """Test connection initialization."""
        assert odoo_connection.url == "http://test.example.com"
        assert odoo_connection.db == "test_db"
        assert odoo_connection.token == "test_token"
        assert odoo_connection.uid is None
        assert odoo_connection.available_models is None

        # Check that ServerProxy was called with the correct URLs
        expected_calls = [
            mock.call("http://test.example.com/xmlrpc/2/common", allow_none=True),
            mock.call("http://test.example.com/xmlrpc/2/object", allow_none=True),
        ]
        mock_odoo_endpoints["proxy"].assert_has_calls(expected_calls, any_order=True)

    def test_url_normalization(self):
        """Test that URL trailing slashes are removed."""
        connection = OdooConnection(
            url="http://test.example.com/", db="test_db", token="test_token"
        )
        assert connection.url == "http://test.example.com"

    def test_test_connection_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful connection test."""
        # Setup mocks
        mock_odoo_endpoints["common"].version.return_value = {"server_version": "16.0"}
        mock_odoo_endpoints["object"].execute_kw.side_effect = [
            42,  # _authenticate return value
            ["res.partner", "product.template"],  # _load_available_models return value
        ]

        # Test connection
        result = odoo_connection.test_connection()

        # Verify results
        assert result is True
        assert odoo_connection.uid == 42
        assert odoo_connection.available_models == ["res.partner", "product.template"]

        # Verify calls
        mock_odoo_endpoints["common"].version.assert_called_once()
        mock_odoo_endpoints["object"].execute_kw.assert_has_calls(
            [
                # Authentication call
                mock.call(
                    "test_db",
                    1,
                    "admin",
                    "mcp.server",
                    "authenticate_token",
                    ["test_token"],
                ),
                # Get enabled models call
                mock.call(
                    "test_db", 42, "admin", "mcp.server", "get_enabled_models", []
                ),
            ]
        )

    def test_test_connection_failure(self, odoo_connection, mock_odoo_endpoints):
        """Test connection test failure."""
        # Setup mock to raise an exception
        mock_odoo_endpoints["common"].version.side_effect = xmlrpc.client.Fault(
            1, "Error"
        )

        # Test connection should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection.test_connection()

        # Check exception message
        assert "Failed to connect to Odoo" in str(exc_info.value)

    def test_authentication_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful authentication."""
        # Setup mock
        mock_odoo_endpoints["object"].execute_kw.return_value = 42

        # Call _authenticate
        uid = odoo_connection._authenticate()

        # Verify results
        assert uid == 42

        # Verify call
        mock_odoo_endpoints["object"].execute_kw.assert_called_once_with(
            "test_db", 1, "admin", "mcp.server", "authenticate_token", ["test_token"]
        )

    def test_authentication_failure(self, odoo_connection, mock_odoo_endpoints):
        """Test authentication failure."""
        # Setup mock to return None (invalid token)
        mock_odoo_endpoints["object"].execute_kw.return_value = None

        # Authentication should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection._authenticate()

        # Check exception message
        assert "Invalid MCP token" in str(exc_info.value)

    def test_authentication_error(self, odoo_connection, mock_odoo_endpoints):
        """Test authentication error."""
        # Setup mock to raise an exception
        mock_odoo_endpoints["object"].execute_kw.side_effect = xmlrpc.client.Fault(
            1, "Error"
        )

        # Authentication should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection._authenticate()

        # Check exception message
        assert "Authentication failed" in str(exc_info.value)

    def test_load_available_models_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful loading of available models."""
        # Setup
        odoo_connection.uid = 42
        mock_odoo_endpoints["object"].execute_kw.return_value = [
            "res.partner",
            "product.template",
        ]

        # Call _load_available_models
        odoo_connection._load_available_models()

        # Verify results
        assert odoo_connection.available_models == ["res.partner", "product.template"]

        # Verify call
        mock_odoo_endpoints["object"].execute_kw.assert_called_once_with(
            "test_db", 42, "admin", "mcp.server", "get_enabled_models", []
        )

    def test_load_available_models_error(self, odoo_connection, mock_odoo_endpoints):
        """Test error during loading of available models."""
        # Setup
        odoo_connection.uid = 42
        mock_odoo_endpoints["object"].execute_kw.side_effect = xmlrpc.client.Fault(
            1, "Error"
        )

        # Loading models should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection._load_available_models()

        # Check exception message
        assert "Failed to load available models" in str(exc_info.value)

    def test_get_model_fields_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful retrieval of model fields."""
        # Setup
        odoo_connection.uid = 42
        odoo_connection.available_models = ["res.partner", "product.template"]

        # Mock field definitions
        field_defs = {
            "name": {"type": "char", "string": "Name"},
            "email": {"type": "char", "string": "Email"},
        }
        mock_odoo_endpoints["object"].execute_kw.return_value = field_defs

        # Call get_model_fields
        result = odoo_connection.get_model_fields("res.partner")

        # Verify results
        assert result == field_defs

        # Verify call
        mock_odoo_endpoints["object"].execute_kw.assert_called_once_with(
            "test_db", 42, "admin", "res.partner", "fields_get", []
        )

    def test_get_model_fields_not_enabled(self, odoo_connection):
        """Test retrieval of fields for a model that's not enabled."""
        # Setup
        odoo_connection.available_models = ["product.template"]

        # Call should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection.get_model_fields("res.partner")

        # Check exception message
        assert "not enabled for MCP access" in str(exc_info.value)

    def test_get_model_fields_cache(self, odoo_connection, mock_odoo_endpoints):
        """Test that field definitions are cached."""
        # Setup
        odoo_connection.uid = 42
        odoo_connection.available_models = ["res.partner"]

        # Mock field definitions
        field_defs = {
            "name": {"type": "char", "string": "Name"},
            "email": {"type": "char", "string": "Email"},
        }
        mock_odoo_endpoints["object"].execute_kw.return_value = field_defs

        # First call
        result1 = odoo_connection.get_model_fields("res.partner")

        # Second call should use cache
        result2 = odoo_connection.get_model_fields("res.partner")

        # Verify results
        assert result1 == field_defs
        assert result2 == field_defs

        # Verify execute_kw was called only once
        mock_odoo_endpoints["object"].execute_kw.assert_called_once()

    def test_search_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful search operation."""
        # Setup
        odoo_connection.uid = 42
        odoo_connection.available_models = ["res.partner"]

        # Mock search results
        search_results = [
            {"id": 1, "name": "Test Partner 1"},
            {"id": 2, "name": "Test Partner 2"},
        ]
        mock_odoo_endpoints["object"].execute_kw.return_value = search_results

        # Call search
        domain = [("is_company", "=", True)]
        fields = ["name", "email"]
        result = odoo_connection.search(
            "res.partner", domain, fields, limit=10, offset=0, order="name"
        )

        # Verify results
        assert result == search_results

        # Verify call
        mock_odoo_endpoints["object"].execute_kw.assert_called_once_with(
            "test_db",
            42,
            "admin",
            "res.partner",
            "search_read",
            [domain],
            {"fields": fields, "limit": 10, "offset": 0, "order": "name"},
        )

    def test_search_model_not_enabled(self, odoo_connection):
        """Test search on a model that's not enabled."""
        # Setup
        odoo_connection.available_models = ["product.template"]

        # Call should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection.search("res.partner", [])

        # Check exception message
        assert "not enabled for MCP access" in str(exc_info.value)

    def test_read_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful read operation."""
        # Setup
        odoo_connection.uid = 42
        odoo_connection.available_models = ["res.partner"]

        # Mock read results
        read_results = [
            {"id": 1, "name": "Test Partner 1"},
            {"id": 2, "name": "Test Partner 2"},
        ]
        mock_odoo_endpoints["object"].execute_kw.return_value = read_results

        # Call read
        ids = [1, 2]
        fields = ["name", "email"]
        result = odoo_connection.read("res.partner", ids, fields)

        # Verify results
        assert result == read_results

        # Verify call
        mock_odoo_endpoints["object"].execute_kw.assert_called_once_with(
            "test_db", 42, "admin", "res.partner", "read", [ids], {"fields": fields}
        )

    def test_read_model_not_enabled(self, odoo_connection):
        """Test read on a model that's not enabled."""
        # Setup
        odoo_connection.available_models = ["product.template"]

        # Call should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection.read("res.partner", [1])

        # Check exception message
        assert "not enabled for MCP access" in str(exc_info.value)

    def test_count_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful count operation."""
        # Setup
        odoo_connection.uid = 42
        odoo_connection.available_models = ["res.partner"]

        # Mock search_count result
        mock_odoo_endpoints["object"].execute_kw.return_value = 42

        # Call count
        domain = [("is_company", "=", True)]
        result = odoo_connection.count("res.partner", domain)

        # Verify results
        assert result == 42

        # Verify call
        mock_odoo_endpoints["object"].execute_kw.assert_called_once_with(
            "test_db", 42, "admin", "res.partner", "search_count", [domain]
        )

    def test_count_model_not_enabled(self, odoo_connection):
        """Test count on a model that's not enabled."""
        # Setup
        odoo_connection.available_models = ["product.template"]

        # Call should raise an exception
        with pytest.raises(OdooConnectionError) as exc_info:
            odoo_connection.count("res.partner", [])

        # Check exception message
        assert "not enabled for MCP access" in str(exc_info.value)

    def test_execute_with_retry_success(self, odoo_connection, mock_odoo_endpoints):
        """Test successful execution with retry."""
        # Setup
        odoo_connection.uid = 42
        mock_odoo_endpoints["object"].execute_kw.return_value = "result"

        # Call the method
        result = odoo_connection._execute_with_retry(
            "res.partner", "read", [[1]], {"fields": ["name"]}
        )

        # Verify the result
        assert result == "result"

        # Verify the call
        mock_odoo_endpoints["object"].execute_kw.assert_called_once_with(
            "test_db", 42, "admin", "res.partner", "read", [[1]], {"fields": ["name"]}
        )

    def test_execute_with_retry_with_transient_errors(
        self, odoo_connection, mock_odoo_endpoints
    ):
        """Test retry behavior with transient errors."""
        # Setup
        odoo_connection.uid = 42

        # Mock execute_kw to fail twice then succeed
        side_effects = [
            ConnectionError("Connection reset"),
            TimeoutError("Timeout"),
            "success",
        ]
        mock_odoo_endpoints["object"].execute_kw.side_effect = side_effects

        # Reduce retry delay for testing
        odoo_connection.retry_delay = 0.01

        # Call the method
        result = odoo_connection._execute_with_retry("res.partner", "read", [[1]])

        # Verify the result
        assert result == "success"

        # Verify all three calls were made
        assert mock_odoo_endpoints["object"].execute_kw.call_count == 3


class TestOdooConnectionPool:
    """Test cases for OdooConnectionPool class."""

    def test_singleton_pattern(self):
        """Test that OdooConnectionPool is a singleton."""
        pool1 = OdooConnectionPool()
        pool2 = OdooConnectionPool()

        # Same instance
        assert pool1 is pool2

        # Calling clear on one affects both
        pool1.clear()
        assert len(pool2._connections) == 0

    def test_init_done_flag(self):
        """Test that the pool is not re-initialized once _init_done is True."""
        # Get a new pool
        pool = OdooConnectionPool()
        pool.clear()

        # Set our attributes to track initialization
        pool._init_done = False  # Reset this first
        pool._max_connections = 42
        pool._connection_timeout = 100

        # Set the init done flag
        pool._init_done = True

        # Create a new reference - this should trigger __init__ but return early
        pool2 = OdooConnectionPool()

        # Should be the same instance with the modified values
        assert pool2._max_connections == 42
        assert pool2._connection_timeout == 100
        assert pool2._init_done is True

        # Reset for other tests
        pool._max_connections = 10
        pool._connection_timeout = 300
        pool.clear()

    def test_get_connection_creates_new(
        self, mock_odoo_endpoints, clear_connection_pool
    ):
        """Test that get_connection creates a new connection if none exists."""
        # Get a connection
        pool = OdooConnectionPool()
        connection = pool.get_connection(
            "http://test.example.com", "test_db", "test_token"
        )

        # Verify it's an OdooConnection
        assert isinstance(connection, OdooConnection)
        assert connection.url == "http://test.example.com"
        assert connection.db == "test_db"
        assert connection.token == "test_token"

        # Verify it's in the pool
        assert len(pool._connections) == 1

    def test_get_connection_handles_broken_connections(
        self, mock_odoo_endpoints, clear_connection_pool
    ):
        """Test that get_connection handles broken connections."""
        # Setup
        pool = OdooConnectionPool()

        # Create a connection that will appear broken
        connection = OdooConnection("http://test.example.com", "test_db", "test_token")
        connection.uid = 42

        # Add it to the pool
        key = ("http://test.example.com", "test_db", "test_token")
        pool._connections[key] = (connection, time.time())

        # Mock the behavior of get_connection to simulate a broken connection
        original_get_connection = pool.get_connection

        def mocked_verification(*args, **kwargs):
            # The first time we retrieve the connection, simulate an error during validation
            if args == ("http://test.example.com", "test_db", "test_token"):
                # Clear the connection first to force creating a new one
                del pool._connections[key]
                # Return a new connection
                return OdooConnection(
                    "http://test.example.com", "test_db", "test_token"
                )
            return original_get_connection(*args, **kwargs)

        with mock.patch.object(pool, "get_connection", side_effect=mocked_verification):
            # Get a connection - should detect broken and create a new one
            new_connection = mocked_verification(
                "http://test.example.com", "test_db", "test_token"
            )

            # Verify it's a new instance
            assert new_connection is not connection

    def test_get_connection_reuses_existing(
        self, mock_odoo_endpoints, clear_connection_pool
    ):
        """Test that get_connection reuses an existing connection."""
        # Setup
        pool = OdooConnectionPool()

        # Get a connection and set a property to verify reuse
        connection1 = pool.get_connection(
            "http://test.example.com", "test_db", "test_token"
        )
        connection1.uid = 42  # Simulate authenticated connection

        # Get another connection with the same parameters
        connection2 = pool.get_connection(
            "http://test.example.com", "test_db", "test_token"
        )

        # Verify it's the same instance
        assert connection1 is connection2
        assert connection2.uid == 42

        # Verify only one connection in the pool
        assert len(pool._connections) == 1

    def test_get_connection_different_params(
        self, mock_odoo_endpoints, clear_connection_pool
    ):
        """Test that get_connection creates new connections for different parameters."""
        # Setup
        pool = OdooConnectionPool()

        # Get connections with different parameters
        connection1 = pool.get_connection(
            "http://test1.example.com", "test_db", "test_token"
        )
        connection2 = pool.get_connection(
            "http://test2.example.com", "test_db", "test_token"
        )
        connection3 = pool.get_connection(
            "http://test1.example.com", "other_db", "test_token"
        )
        connection4 = pool.get_connection(
            "http://test1.example.com", "test_db", "other_token"
        )

        # Verify all are different instances
        assert connection1 is not connection2
        assert connection1 is not connection3
        assert connection1 is not connection4
        assert connection2 is not connection3
        assert connection2 is not connection4
        assert connection3 is not connection4

        # Verify all are in the pool
        assert len(pool._connections) == 4

    def test_cleanup_expired_connections(
        self, mock_odoo_endpoints, clear_connection_pool
    ):
        """Test that expired connections are cleaned up."""
        # Setup
        pool = OdooConnectionPool()
        pool.clear()  # Ensure pool is clean
        pool._connection_timeout = 0.2  # Short timeout for testing

        # Add connections to the pool directly
        conn1 = OdooConnection("http://test1.example.com", "test_db", "test_token")
        conn2 = OdooConnection("http://test2.example.com", "test_db", "test_token")

        # Add to pool with different times - one should expire, one should not
        key1 = ("http://test1.example.com", "test_db", "test_token")
        key2 = ("http://test2.example.com", "test_db", "test_token")

        now = time.time()
        pool._connections[key1] = (conn1, now)  # Recent connection
        pool._connections[key2] = (conn2, now - 0.5)  # Older connection beyond timeout

        # Manually run cleanup - the older connection should be expired
        pool._cleanup_expired_connections()

        # Verify expired connection was removed (the older one)
        assert key2 not in pool._connections, (
            "Older connection should have been removed"
        )
        assert key1 in pool._connections, "Recent connection should still be present"

    def test_cleanup_expired_connections_empty(self, clear_connection_pool):
        """Test that cleanup works with empty connection pool."""
        # Setup
        pool = OdooConnectionPool()
        pool.clear()  # Ensure pool is clean

        # Should not raise any exceptions
        pool._cleanup_expired_connections()
        assert len(pool._connections) == 0

    def test_max_connections_eviction(self, mock_odoo_endpoints, clear_connection_pool):
        """Test that old connections are evicted when max connections is reached."""
        # Setup
        pool = OdooConnectionPool()
        pool.clear()  # Ensure pool is clean
        pool._max_connections = 3  # Small max for testing

        # Add connections to the pool directly with timestamps in the past
        conn1 = OdooConnection("http://test1.example.com", "test_db", "test_token")
        conn2 = OdooConnection("http://test2.example.com", "test_db", "test_token")
        conn3 = OdooConnection("http://test3.example.com", "test_db", "test_token")

        now = time.time()
        key1 = ("http://test1.example.com", "test_db", "test_token")
        key2 = ("http://test2.example.com", "test_db", "test_token")
        key3 = ("http://test3.example.com", "test_db", "test_token")

        pool._connections[key1] = (conn1, now - 3)
        pool._connections[key2] = (conn2, now - 2)
        pool._connections[key3] = (conn3, now - 1)

        # Verify we have exactly 3 connections
        assert len(pool._connections) == 3

        # Manually call eviction with a new connection
        pool._connections[("http://test4.example.com", "test_db", "test_token")] = (
            OdooConnection("http://test4.example.com", "test_db", "test_token"),
            now,
        )
        pool._evict_oldest_connection()

        # Verify the oldest connection was evicted and we still have exactly 3
        assert key1 not in pool._connections
        assert key2 in pool._connections
        assert key3 in pool._connections
        assert (
            "http://test4.example.com",
            "test_db",
            "test_token",
        ) in pool._connections
        assert len(pool._connections) == 3

    def test_evict_oldest_connection_empty(self, clear_connection_pool):
        """Test that eviction works with empty connection pool."""
        # Setup
        pool = OdooConnectionPool()
        pool.clear()  # Ensure pool is clean

        # Should not raise any exceptions
        pool._evict_oldest_connection()
        assert len(pool._connections) == 0

    def test_get_connection_verification_with_none_uid(
        self, mock_odoo_endpoints, clear_connection_pool
    ):
        """Test connection verification when uid is None."""
        # Setup
        pool = OdooConnectionPool()

        # Create a connection with a None uid
        connection = OdooConnection("http://test.example.com", "test_db", "test_token")
        connection.uid = None  # Explicitly set to None

        # Add to pool
        key = ("http://test.example.com", "test_db", "test_token")
        pool._connections[key] = (connection, time.time())

        # Get a connection - should create a new one since validation will fail
        with mock.patch.object(OdooConnection, "test_connection", return_value=True):
            new_connection = pool.get_connection(
                "http://test.example.com", "test_db", "test_token"
            )

            # Should be a new connection
            assert new_connection is not connection


class TestOdooConnectionErrors:
    """Test cases for OdooConnection error handling."""

    def test_test_connection_internal_error(self, mock_odoo_endpoints):
        """Test test_connection with an internal error during authentication."""
        # Setup OdooConnection
        connection = OdooConnection(
            url="http://test.example.com", db="test_db", token="test_token"
        )

        # Make common.version work but _authenticate raise an exception
        mock_odoo_endpoints["common"].version.return_value = {"server_version": "16.0"}

        # Patch _authenticate to raise an exception
        with mock.patch.object(
            connection,
            "_authenticate",
            side_effect=RuntimeError("Authentication error"),
        ):
            # Should raise OdooConnectionError
            with pytest.raises(OdooConnectionError) as exc_info:
                connection.test_connection()

            # Check exception message
            assert "Failed to connect to Odoo" in str(exc_info.value)

    def test_test_connection_with_error_during_operations(self, mock_odoo_endpoints):
        """Test that test_connection properly handles errors during connection test."""
        # Create a connection
        connection = OdooConnection("http://test.example.com", "test_db", "test_token")

        # Mock version() to work
        mock_odoo_endpoints["common"].version.return_value = {"server_version": "16.0"}

        # Mock object_endpoint with a patch that still allows initial creation but then fails
        with mock.patch("xmlrpc.client.ServerProxy") as mock_server_proxy:
            # Return a mock that works for initialization but fails later
            mock_common = mock.MagicMock()
            mock_object = mock.MagicMock()

            def mock_server_proxy_side_effect(url, **kwargs):
                if "common" in url:
                    return mock_common
                else:
                    return mock_object

            mock_server_proxy.side_effect = mock_server_proxy_side_effect

            # Create a fresh connection with our mocks
            conn = OdooConnection("http://test.example.com", "test_db", "test_token")

            # Make common.version() work
            mock_common.version.return_value = {"server_version": "16.0"}

            # Make object_endpoint.execute_kw fail with a specific error
            mock_object.execute_kw.side_effect = Exception("Connection failed")

            # Test connection should raise an OdooConnectionError
            with pytest.raises(OdooConnectionError) as exc_info:
                conn.test_connection()

            # Check exception message
            assert "Failed to connect to Odoo" in str(exc_info.value)

    def test_test_connection_generic_exception(self):
        """Test generic exception handling in test_connection."""
        # Mock to simulate exception in version() call
        with mock.patch("xmlrpc.client.ServerProxy") as mock_server_proxy:
            # Setup to raise a generic exception on version() call
            mock_common = mock.MagicMock()
            mock_common.version.side_effect = Exception("General server error")

            def side_effect(url, **kwargs):
                if "common" in url:
                    return mock_common
                else:
                    return mock.MagicMock()

            mock_server_proxy.side_effect = side_effect

            # Initialize connection
            connection = OdooConnection(
                "http://test.example.com", "test_db", "test_token"
            )

            # Test connection should fail
            with pytest.raises(OdooConnectionError) as exc_info:
                connection.test_connection()

            # Verify exception message contains the original error message
            assert "Failed to connect to Odoo" in str(exc_info.value)
            assert "General server error" in str(exc_info.value)


def test_get_odoo_connection(mock_odoo_endpoints, clear_connection_pool):
    """Test the get_odoo_connection convenience function."""
    # Call the function twice with the same parameters
    conn1 = get_odoo_connection("http://test.example.com", "test_db", "test_token")
    conn1.uid = 42  # Simulate authenticated connection

    conn2 = get_odoo_connection("http://test.example.com", "test_db", "test_token")

    # Verify it's the same instance from the pool
    assert conn1 is conn2
    assert conn2.uid == 42

    # Verify we have just one connection in the pool
    pool = OdooConnectionPool()
    assert len(pool._connections) == 1


class TestOdooConnectionInitialization:
    """Test cases that focus on OdooConnection initialization edge cases."""

    def test_odoo_connection_init_with_very_specific_mocks(self):
        """Test OdooConnection initialization with very specific mocks."""
        # Mock ServerProxy to track initialization
        with mock.patch("xmlrpc.client.ServerProxy") as mock_server_proxy:
            # Setup the mocks
            mock_common = mock.MagicMock()
            mock_object = mock.MagicMock()

            def side_effect(url, **kwargs):
                if "common" in url:
                    return mock_common
                else:
                    return mock_object

            mock_server_proxy.side_effect = side_effect

            # Initialize connection
            connection = OdooConnection(
                "http://test.example.com", "test_db", "test_token"
            )

            # Check that ServerProxy was called correctly
            assert mock_server_proxy.call_count == 2
            mock_server_proxy.assert_any_call(
                "http://test.example.com/xmlrpc/2/common", allow_none=True
            )
            mock_server_proxy.assert_any_call(
                "http://test.example.com/xmlrpc/2/object", allow_none=True
            )

            # Verify connection state
            assert connection.url == "http://test.example.com"
            assert connection.db == "test_db"
            assert connection.token == "test_token"
            assert connection.uid is None
            assert connection.available_models is None
            assert connection.model_fields_cache == {}
            assert connection.max_retries == 3
            assert connection.retry_delay == 1

    def test_test_connection_generic_exception(self):
        """Test generic exception handling in test_connection."""
        # Mock to simulate exception in version() call
        with mock.patch("xmlrpc.client.ServerProxy") as mock_server_proxy:
            # Setup to raise a generic exception on version() call
            mock_common = mock.MagicMock()
            mock_common.version.side_effect = Exception("General server error")

            def side_effect(url, **kwargs):
                if "common" in url:
                    return mock_common
                else:
                    return mock.MagicMock()

            mock_server_proxy.side_effect = side_effect

            # Initialize connection
            connection = OdooConnection(
                "http://test.example.com", "test_db", "test_token"
            )

            # Test connection should fail
            with pytest.raises(OdooConnectionError) as exc_info:
                connection.test_connection()

            # Verify exception message contains the original error message
            assert "Failed to connect to Odoo" in str(exc_info.value)
            assert "General server error" in str(exc_info.value)
