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

        # Check result structure and values
        assert result["total"] == 3
        assert len(result["models"]) == 3

        # Verify model names from the mock actually appear in the result
        model_names = [m["model"] for m in result["models"]]
        assert "res.partner" in model_names
        assert "product.product" in model_names
        assert "sale.order" in model_names

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

        # Verify search_read was called with correct domain
        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"
        assert ("transient", "=", False) in call_args[0][1]

        # In YOLO mode, models should NOT have per-model operations (unlike standard mode)
        for model in result["models"]:
            assert "operations" not in model

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

        # Check result structure and values
        assert result["total"] == 2
        assert len(result["models"]) == 2

        # Verify model names from the mock actually appear in the result
        model_names = [m["model"] for m in result["models"]]
        assert "res.partner" in model_names
        assert "account.move" in model_names

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

        # Verify search_read was called with correct domain and fields
        mock_connection.search_read.assert_called_once()
        call_args = mock_connection.search_read.call_args
        assert call_args[0][0] == "ir.model"

        # In YOLO mode, models should NOT have per-model operations (unlike standard mode)
        models = result["models"]
        for model in models:
            assert "operations" not in model

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

        # Verify result contains the models with correct permissions
        models = result["models"]
        assert len(models) == 2
        for model in models:
            assert "operations" in model
            assert model["operations"]["read"] is True
            assert model["operations"]["write"] is True
            assert model["operations"]["create"] is False
            assert model["operations"]["unlink"] is False

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

        # Call the method and verify empty result is handled
        result = await handler._handle_list_models_tool()
        assert result["models"] == []
        assert result["total"] == 0

        # Verify the domain passed to search_read
        call_args = mock_connection.search_read.call_args
        domain = call_args[0][1]

        # Check domain structure — verify the actual Polish-notation domain
        assert isinstance(domain, list)
        assert domain[0] == "&", "Domain should start with AND operator"
        assert ("transient", "=", False) in domain
        # Should have OR conditions for model filtering
        assert "|" in domain, "Domain should include OR conditions for model filtering"
        assert ("model", "not like", "ir.%") in domain
        assert ("model", "not like", "base.%") in domain
        # Should include whitelist of allowed ir.* models
        ir_whitelist = [
            c for c in domain if isinstance(c, tuple) and c[0] == "model" and c[1] == "in"
        ]
        assert len(ir_whitelist) == 1, "Should have exactly one 'model in [...]' whitelist"
        assert "ir.attachment" in ir_whitelist[0][2]

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

        # Call the method and verify result
        result = await handler._handle_list_models_tool()
        assert result["total"] == 1
        assert len(result["models"]) == 1
        assert result["models"][0]["model"] == "res.partner"

        # Check logs
        assert "YOLO mode (READ-ONLY)" in caplog.text
        assert "Listed 1 models from database" in caplog.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
