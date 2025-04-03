"""Odoo XML-RPC connection handling.

This module manages connections to Odoo instances via XML-RPC,
handling authentication, requests, and error handling.
"""

import logging
import xmlrpc.client
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


class OdooConnectionError(Exception):
    """Exception raised for Odoo connection errors."""

    pass


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
            uid = self.object_endpoint.execute_kw(
                self.db, 1, "admin", "mcp.server", "authenticate_token", [self.token]
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
            self.available_models = self.object_endpoint.execute_kw(
                self.db, self.uid, "admin", "mcp.server", "get_enabled_models", []
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
            fields_info = self.object_endpoint.execute_kw(
                self.db, self.uid, "admin", model, "fields_get", []
            )

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
            records = self.object_endpoint.execute_kw(
                self.db,
                self.uid,
                "admin",
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
            records = self.object_endpoint.execute_kw(
                self.db,
                self.uid,
                "admin",
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
            count = self.object_endpoint.execute_kw(
                self.db, self.uid, "admin", model, "search_count", [domain]
            )

            return count

        except Exception as e:
            logger.error(f"Count failed for model '{model}': {e}")
            raise OdooConnectionError(f"Count failed for model '{model}': {e}")
