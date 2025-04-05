"""Odoo XML-RPC connection handling.

This module manages connections to Odoo instances via XML-RPC,
handling authentication, requests, and error handling.
"""

import logging
import time
import xmlrpc.client
from threading import RLock
from typing import Any, ClassVar, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class OdooConnectionError(Exception):
    """Exception raised for Odoo connection errors."""

    pass


class OdooConnectionPool:
    """Pool of OdooConnection instances for reuse."""

    _instance: ClassVar[Optional["OdooConnectionPool"]] = None
    _lock: ClassVar[RLock] = RLock()

    def __new__(cls):
        """Singleton pattern to ensure only one pool exists."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(OdooConnectionPool, cls).__new__(cls)
                cls._instance._connections = {}
                cls._instance._init_done = False
            return cls._instance

    def __init__(self):
        """Initialize the connection pool."""
        # Avoid re-initialization if already done
        if self._init_done:
            return

        self._connections = {}  # {conn_key: (connection, last_used_time)}
        self._max_connections = 10
        self._connection_timeout = 300  # 5 minutes
        self._init_done = True
        logger.debug("OdooConnectionPool initialized")

    def get_connection(self, url: str, db: str, token: str) -> "OdooConnection":
        """Get a connection from the pool or create a new one.

        Args:
            url: URL of the Odoo instance
            db: Database name
            token: MCP authentication token

        Returns:
            OdooConnection: A connection object
        """
        conn_key = (url, db, token)

        with self._lock:
            # Clean up expired connections first
            self._cleanup_expired_connections()

            # Check if connection exists and is valid
            if conn_key in self._connections:
                connection, last_used = self._connections[conn_key]

                # Update last used time
                self._connections[conn_key] = (connection, time.time())

                # Verify the connection is still valid
                try:
                    # Minimal validation to avoid costly operations
                    if connection.uid is not None:
                        logger.debug(f"Reusing existing connection for {url}, {db}")
                        return connection
                except:
                    # Connection is broken, remove it
                    logger.warning(
                        f"Found broken connection for {url}, {db}, will create new one"
                    )
                    del self._connections[conn_key]

            # Create new connection
            logger.info(f"Creating new connection for {url}, {db}")
            connection = OdooConnection(url, db, token)

            # Save to the pool
            self._connections[conn_key] = (connection, time.time())

            # Make room if needed
            if len(self._connections) > self._max_connections:
                self._evict_oldest_connection()

            return connection

    def _cleanup_expired_connections(self) -> None:
        """Remove expired connections from the pool."""
        now = time.time()
        expired_keys = []

        for key, (_, last_used) in self._connections.items():
            if now - last_used > self._connection_timeout:
                expired_keys.append(key)

        for key in expired_keys:
            logger.debug(f"Removing expired connection {key[0]}, {key[1]}")
            del self._connections[key]

    def _evict_oldest_connection(self) -> None:
        """Remove the oldest connection from the pool."""
        if not self._connections:
            return

        oldest_key = None
        oldest_time = float("inf")

        for key, (_, last_used) in self._connections.items():
            if last_used < oldest_time:
                oldest_time = last_used
                oldest_key = key

        if oldest_key:
            logger.debug(f"Evicting oldest connection {oldest_key[0]}, {oldest_key[1]}")
            del self._connections[oldest_key]

    def clear(self) -> None:
        """Clear all connections from the pool."""
        with self._lock:
            self._connections.clear()
            logger.debug("Connection pool cleared")


class OdooConnection:
    """Handles connections to Odoo via XML-RPC.

    This class manages authentication and communication with
    Odoo instances, providing a simplified interface for making
    XML-RPC calls to Odoo.
    """

    def __init__(self, url: str, db: str, token: str):
        """Initialize Odoo connection.

        Args:
            url: URL of the Odoo instance
            db: Database name
            token: MCP authentication token
        """
        self.url = url.rstrip("/")
        self.db = db
        self.token = token

        # Initialize XML-RPC endpoints
        self.common_endpoint = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.object_endpoint = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

        # Session state
        self.uid = None
        self.available_models = None
        self.model_fields_cache = {}

        # Add connection retry settings
        self.max_retries = 3
        self.retry_delay = 1  # seconds

        logger.debug(f"Created new OdooConnection instance for {url}, {db}")

    def test_connection(self) -> bool:
        """Test connection to Odoo and authenticate.

        Returns:
            bool: True if connection and authentication successful

        Raises:
            OdooConnectionError: If connection or authentication fails
        """
        try:
            # Check if server is reachable
            self.common_endpoint.version()

            # Authenticate with token
            self.uid = self._authenticate()

            # Get available models
            self._load_available_models()

            return True

        except Exception as e:
            logger.error(f"Failed to connect to Odoo: {e}")
            raise OdooConnectionError(f"Failed to connect to Odoo: {e}")

    def _authenticate(self) -> int:
        """Authenticate with Odoo using MCP token.

        Returns:
            int: User ID (uid) if authentication successful

        Raises:
            OdooConnectionError: If authentication fails
        """
        try:
            # Authenticate with custom MCP method
            uid = self._execute_with_retry(
                "mcp.server", "authenticate_token", [self.token], use_uid=False
            )

            if not uid:
                raise OdooConnectionError("Invalid MCP token")

            logger.info(f"Authenticated with Odoo as user #{uid}")
            return uid

        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            raise OdooConnectionError(f"Authentication failed: {e}")

    def _load_available_models(self) -> None:
        """Load available models that are enabled for MCP access.

        Raises:
            OdooConnectionError: If loading models fails
        """
        try:
            # Get models enabled for MCP
            self.available_models = self._execute_with_retry(
                "mcp.server", "get_enabled_models", []
            )

            logger.info(f"Loaded {len(self.available_models)} available models")

        except Exception as e:
            logger.error(f"Failed to load available models: {e}")
            raise OdooConnectionError(f"Failed to load available models: {e}")

    def get_model_fields(self, model: str) -> Dict[str, Dict[str, Any]]:
        """Get field definitions for a model.

        Args:
            model: Model name

        Returns:
            Dict[str, Dict[str, Any]]: Field definitions

        Raises:
            OdooConnectionError: If model is not available or fields can't be loaded
        """
        # Check if model is available
        if self.available_models and model not in self.available_models:
            raise OdooConnectionError(f"Model '{model}' is not enabled for MCP access")

        # Check cache first
        if model in self.model_fields_cache:
            return self.model_fields_cache[model]

        try:
            # Get fields info
            fields_info = self._execute_with_retry(model, "fields_get", [])

            # Cache for future use
            self.model_fields_cache[model] = fields_info
            return fields_info

        except Exception as e:
            logger.error(f"Failed to get fields for model '{model}': {e}")
            raise OdooConnectionError(f"Failed to get fields for model '{model}': {e}")

    def search(
        self,
        model: str,
        domain: List[Tuple],
        fields: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search for records in a model.

        Args:
            model: Model name
            domain: Search domain
            fields: Fields to return (None for all)
            limit: Maximum number of records
            offset: Pagination offset
            order: Order specification

        Returns:
            List[Dict[str, Any]]: List of records

        Raises:
            OdooConnectionError: If search fails
        """
        try:
            # Check if model is available
            if self.available_models and model not in self.available_models:
                raise OdooConnectionError(
                    f"Model '{model}' is not enabled for MCP access"
                )

            # Perform search_read operation
            records = self._execute_with_retry(
                model,
                "search_read",
                [domain],
                {
                    "fields": fields,
                    "limit": limit,
                    "offset": offset,
                    "order": order,
                },
            )

            return records

        except Exception as e:
            logger.error(f"Search failed for model '{model}': {e}")
            raise OdooConnectionError(f"Search failed for model '{model}': {e}")

    def read(
        self, model: str, ids: List[int], fields: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Read records by IDs.

        Args:
            model: Model name
            ids: Record IDs
            fields: Fields to return (None for all)

        Returns:
            List[Dict[str, Any]]: List of records

        Raises:
            OdooConnectionError: If read fails
        """
        try:
            # Check if model is available
            if self.available_models and model not in self.available_models:
                raise OdooConnectionError(
                    f"Model '{model}' is not enabled for MCP access"
                )

            # Perform read operation
            records = self._execute_with_retry(
                model,
                "read",
                [ids],
                {
                    "fields": fields,
                },
            )

            return records

        except Exception as e:
            logger.error(f"Read failed for model '{model}': {e}")
            raise OdooConnectionError(f"Read failed for model '{model}': {e}")

    def count(self, model: str, domain: List[Tuple]) -> int:
        """Count records matching domain.

        Args:
            model: Model name
            domain: Search domain

        Returns:
            int: Number of matching records

        Raises:
            OdooConnectionError: If count fails
        """
        try:
            # Check if model is available
            if self.available_models and model not in self.available_models:
                raise OdooConnectionError(
                    f"Model '{model}' is not enabled for MCP access"
                )

            # Perform search_count operation
            count = self._execute_with_retry(model, "search_count", [domain])

            return count

        except Exception as e:
            logger.error(f"Count failed for model '{model}': {e}")
            raise OdooConnectionError(f"Count failed for model '{model}': {e}")

    def _execute_with_retry(
        self,
        model: str,
        method: str,
        args: List,
        kwargs: Optional[Dict[str, Any]] = None,
        use_uid: bool = True,
    ) -> Any:
        """Execute an XML-RPC method with retry logic.

        Args:
            model: Model name
            method: Method name
            args: Positional arguments
            kwargs: Keyword arguments
            use_uid: Whether to use UID for authentication (False for auth methods)

        Returns:
            Any: Method result

        Raises:
            OdooConnectionError: If the call fails after retries
        """
        if kwargs is None:
            kwargs = {}

        # Use UID 1 (admin) for unauthenticated calls
        uid = self.uid if use_uid and self.uid else 1

        # Prepare call arguments - ensure empty lists are explicitly passed
        base_args = [self.db, uid, "admin", model, method, args]
        if kwargs:
            base_args.append(kwargs)

        retries = 0
        last_error = None

        while retries <= self.max_retries:
            try:
                # Perform call
                if retries > 0:
                    logger.info(f"Retry {retries} for {model}.{method}")

                result = self.object_endpoint.execute_kw(*base_args)
                return result

            except xmlrpc.client.Fault as e:
                # XML-RPC faults are usually permanent errors
                logger.error(f"XML-RPC fault in {model}.{method}: {e}")
                raise OdooConnectionError(f"Error executing {model}.{method}: {e}")

            except Exception as e:
                # Other exceptions like connection issues can be retried
                logger.warning(f"Error in {model}.{method}, will retry: {e}")
                last_error = e
                retries += 1

                if retries <= self.max_retries:
                    # Exponential backoff (1s, 2s, 4s, etc.)
                    delay = self.retry_delay * (2 ** (retries - 1))
                    time.sleep(delay)

        # All retries failed
        logger.error(
            f"Failed to execute {model}.{method} after {self.max_retries} retries"
        )
        raise OdooConnectionError(
            f"Failed to execute {model}.{method} after {self.max_retries} retries: {last_error}"
        )


def get_odoo_connection(url: str, db: str, token: str) -> OdooConnection:
    """Get a connection from the pool or create a new one.

    This is the preferred way to get an OdooConnection instead of
    creating one directly.

    Args:
        url: URL of the Odoo instance
        db: Database name
        token: MCP authentication token

    Returns:
        OdooConnection: A connection object from the pool
    """
    pool = OdooConnectionPool()
    return pool.get_connection(url, db, token)
