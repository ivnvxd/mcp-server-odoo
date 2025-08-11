"""Tests for tools functionality in YOLO mode.

This module tests the tool handlers behavior in YOLO modes.
"""

import os
from unittest.mock import MagicMock

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.tools import OdooToolHandler


class TestYoloModeTools:
    """Test tools in YOLO mode."""

    @pytest.fixture
    def config_yolo_read(self):
        """Create configuration for read-only YOLO mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="read",
        )

    @pytest.fixture
    def config_yolo_full(self):
        """Create configuration for full access YOLO mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="true",
        )

    @pytest.fixture
    def config_standard(self):
        """Create configuration for standard mode."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="test_api_key",
            database=os.getenv("ODOO_DB"),
            yolo_mode="off",
        )

    @pytest.fixture
    def mock_connection(self):
        """Create mock OdooConnection."""
        mock = MagicMock()
        mock.is_authenticated = True
        mock.search_read = MagicMock()
        return mock

    @pytest.fixture
    def mock_access_controller(self):
        """Create mock AccessController."""
        mock = MagicMock()
        mock.get_enabled_models = MagicMock()
        mock.get_model_permissions = MagicMock()
        return mock

    @pytest.fixture
    def mock_app(self):
        """Create mock FastMCP app."""
        mock = MagicMock()
        return mock

    @pytest.mark.asyncio
    async def test_list_models_yolo_read_mode(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models in read-only YOLO mode."""
        # Setup mock data
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "product.product", "name": "Product"},
            {"model": "sale.order", "name": "Sales Order"},
        ]

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Verify connection was called to query models
        mock_connection.search_read.assert_called_once()
        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"  # Model name
        assert "transient" in str(call_args[0][1])  # Domain includes transient filter

        # Check result structure
        assert "yolo_mode" in result
        assert "models" in result
        assert "total" in result

        # Check YOLO mode metadata
        yolo_meta = result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "read"
        assert "READ-ONLY" in yolo_meta["description"]
        assert "🚨" in yolo_meta["warning"]
        assert yolo_meta["operations"]["read"] is True
        assert yolo_meta["operations"]["write"] is False
        assert yolo_meta["operations"]["create"] is False
        assert yolo_meta["operations"]["unlink"] is False

        # Check actual models are clean (no operations field)
        models = result["models"]
        assert len(models) == 3  # Should match mock data
        for model in models:
            assert "operations" not in model
            assert "model" in model
            assert "name" in model

    @pytest.mark.asyncio
    async def test_list_models_yolo_full_mode(
        self, config_yolo_full, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models in full access YOLO mode."""
        # Setup mock data
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "account.move", "name": "Journal Entry"},
        ]

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_full
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Check result structure
        assert "yolo_mode" in result
        assert "models" in result
        assert "total" in result

        # Check YOLO mode metadata
        yolo_meta = result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "true"
        assert "FULL ACCESS" in yolo_meta["description"]
        assert "🚨" in yolo_meta["warning"]
        assert yolo_meta["operations"]["read"] is True
        assert yolo_meta["operations"]["write"] is True
        assert yolo_meta["operations"]["create"] is True
        assert yolo_meta["operations"]["unlink"] is True

        # Check actual models are clean (no operations field)
        models = result["models"]
        assert len(models) == 2  # Should match mock data
        for model in models:
            assert "operations" not in model
            assert "model" in model
            assert "name" in model

    @pytest.mark.asyncio
    async def test_list_models_standard_mode(
        self, config_standard, mock_connection, mock_access_controller, mock_app
    ):
        """Test list_models in standard mode uses MCP access controller."""
        # Setup mock data
        mock_access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "res.users", "name": "Users"},
        ]

        def mock_get_permissions(model):
            mock_perm = MagicMock()
            mock_perm.can_read = True
            mock_perm.can_write = True
            mock_perm.can_create = False
            mock_perm.can_unlink = False
            return mock_perm

        mock_access_controller.get_model_permissions.side_effect = mock_get_permissions

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_standard
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Verify connection was NOT called (standard mode uses access controller)
        mock_connection.search_read.assert_not_called()

        # Verify access controller was called
        mock_access_controller.get_enabled_models.assert_called_once()

        # Check result structure
        assert "models" in result
        models = result["models"]
        assert len(models) == 2

        # No YOLO warning in standard mode
        for model in models:
            assert "YOLO MODE" not in model["model"]
            assert model["model"] in ["res.partner", "res.users"]

    @pytest.mark.asyncio
    async def test_list_models_yolo_error_handling(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """Test error handling in YOLO mode model listing."""
        # Setup connection to raise error
        mock_connection.search_read.side_effect = Exception("Database connection failed")

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Check error response structure
        assert "yolo_mode" in result
        assert "models" in result
        assert "error" in result

        # Check YOLO mode metadata in error case
        yolo_meta = result["yolo_mode"]
        assert yolo_meta["enabled"] is True
        assert yolo_meta["level"] == "read"
        assert "Error querying models" in yolo_meta["warning"]
        assert yolo_meta["operations"]["read"] is False
        assert yolo_meta["operations"]["write"] is False

        # Models should be empty on error
        assert result["models"] == []
        assert result["total"] == 0
        assert "Database connection failed" in result["error"]

    @pytest.mark.asyncio
    async def test_list_models_yolo_domain_construction(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app
    ):
        """Test that domain is properly constructed in YOLO mode."""
        mock_connection.search_read.return_value = []

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method
        await handler._handle_list_models_tool()

        # Verify the domain passed to search_read
        call_args = mock_connection.search_read.call_args
        domain = call_args[0][1]

        # Check domain structure
        assert isinstance(domain, list)
        # Should filter out transient models
        assert ("transient", "=", False) in domain
        # Should have complex domain with OR conditions
        assert "&" in domain or "|" in domain

    @pytest.mark.asyncio
    async def test_list_models_yolo_includes_common_system_models(
        self, config_yolo_full, mock_connection, mock_access_controller, mock_app
    ):
        """Test that common system models are included in YOLO mode."""
        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "ir.attachment", "name": "Attachment"},
            {"model": "ir.model", "name": "Models"},
        ]

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_full
        )

        # Call the method
        result = await handler._handle_list_models_tool()

        # Check that system models are included
        models = result["models"]
        model_names = [m["model"] for m in models]
        assert "ir.attachment" in model_names
        assert "ir.model" in model_names

    @pytest.mark.asyncio
    async def test_yolo_mode_logging(
        self, config_yolo_read, mock_connection, mock_access_controller, mock_app, caplog
    ):
        """Test that appropriate logging occurs in YOLO mode."""
        import logging

        # Set logging level to capture INFO messages
        caplog.set_level(logging.INFO)

        mock_connection.search_read.return_value = [
            {"model": "res.partner", "name": "Contact"},
        ]

        # Create handler
        handler = OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_yolo_read
        )

        # Call the method
        await handler._handle_list_models_tool()

        # Check logs
        assert "YOLO mode (READ-ONLY)" in caplog.text
        assert "Listed 1 models from database" in caplog.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
