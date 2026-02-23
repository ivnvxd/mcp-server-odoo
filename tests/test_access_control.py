"""Tests for access control integration with Odoo MCP module.

This module tests the AccessController class and its integration with
the Odoo MCP module's REST API endpoints.
"""

import json
import os
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.access_control import (
    AccessControlError,
    AccessController,
)
from mcp_server_odoo.config import OdooConfig

from .conftest import ODOO_SERVER_AVAILABLE


class TestAccessControl:
    """Test access control functionality."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="test_api_key",
            database=os.getenv("ODOO_DB"),
        )

    @pytest.fixture
    def controller(self, config):
        """Create AccessController instance."""
        return AccessController(config, cache_ttl=60)

    def test_init_without_api_key(self, caplog):
        """Test initialization succeeds without API key (warns instead of crashing)."""
        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
        )

        controller = AccessController(config)
        assert controller.config == config
        assert "No API key configured" in caplog.text

    @patch("urllib.request.urlopen")
    def test_make_request_success(self, mock_urlopen, controller):
        """Test successful REST API request."""
        # Mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"success": True, "data": {"test": "value"}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Make request
        result = controller._make_request("/test/endpoint")

        assert result["success"] is True
        assert result["data"]["test"] == "value"

    @patch("urllib.request.urlopen")
    def test_make_request_api_error(self, mock_urlopen, controller):
        """Test REST API request with API error response."""
        # Mock error response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"success": False, "error": {"message": "Test error"}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Should raise error
        with pytest.raises(AccessControlError, match="API error: Test error"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_http_401(self, mock_urlopen, controller):
        """Test REST API request with 401 error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)

        with pytest.raises(AccessControlError, match="Invalid API key"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_http_404(self, mock_urlopen, controller):
        """Test REST API request with 404 error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(None, 404, "Not Found", {}, None)

        with pytest.raises(AccessControlError, match="Endpoint not found"):
            controller._make_request("/test/endpoint")

    def test_cache_operations(self, controller):
        """Test cache get/set operations."""
        # Test cache miss
        assert controller._get_from_cache("test_key") is None

        # Test cache set and hit
        controller._set_cache("test_key", {"data": "value"})
        assert controller._get_from_cache("test_key") == {"data": "value"}

        # Test cache clear
        controller.clear_cache()
        assert controller._get_from_cache("test_key") is None

    def test_cache_expiration(self, controller):
        """Test cache expiration."""
        # Set cache with short TTL
        controller.cache_ttl = 0  # Immediate expiration
        controller._set_cache("test_key", "value")

        # Should be expired
        assert controller._get_from_cache("test_key") is None

    @patch("urllib.request.urlopen")
    def test_get_enabled_models(self, mock_urlopen, controller):
        """Test getting enabled models list."""
        # Mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "models": [
                        {"model": "res.partner", "name": "Contact"},
                        {"model": "res.users", "name": "Users"},
                    ]
                },
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Get models
        models = controller.get_enabled_models()

        assert len(models) == 2
        assert models[0]["model"] == "res.partner"
        assert models[1]["name"] == "Users"

        # Second call should use cache
        models2 = controller.get_enabled_models()
        assert models2 == models
        mock_urlopen.assert_called_once()  # Only called once due to cache

    @patch("urllib.request.urlopen")
    def test_is_model_enabled(self, mock_urlopen, controller):
        """Test checking if model is enabled."""
        # Mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "models": [
                        {"model": "res.partner", "name": "Contact"},
                        {"model": "res.users", "name": "Users"},
                    ]
                },
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Check models
        assert controller.is_model_enabled("res.partner") is True
        assert controller.is_model_enabled("res.users") is True
        assert controller.is_model_enabled("account.move") is False

    @patch("urllib.request.urlopen")
    def test_get_model_permissions(self, mock_urlopen, controller):
        """Test getting model permissions."""
        # Mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "model": "res.partner",
                    "enabled": True,
                    "operations": {"read": True, "write": True, "create": False, "unlink": False},
                },
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Get permissions
        perms = controller.get_model_permissions("res.partner")

        assert perms.model == "res.partner"
        assert perms.enabled is True
        assert perms.can_read is True
        assert perms.can_write is True
        assert perms.can_create is False
        assert perms.can_unlink is False

        # Test can_perform method
        assert perms.can_perform("read") is True
        assert perms.can_perform("write") is True
        assert perms.can_perform("create") is False
        assert perms.can_perform("delete") is False  # Alias for unlink

    @patch("urllib.request.urlopen")
    def test_check_operation_allowed(self, mock_urlopen, controller):
        """Test checking if operation is allowed."""
        # Mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "model": "res.partner",
                    "enabled": True,
                    "operations": {"read": True, "write": False, "create": False, "unlink": False},
                },
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Check operations
        allowed, msg = controller.check_operation_allowed("res.partner", "read")
        assert allowed is True
        assert msg is None

        allowed, msg = controller.check_operation_allowed("res.partner", "write")
        assert allowed is False
        assert "Operation 'write' not allowed" in msg

    @patch("urllib.request.urlopen")
    def test_check_operation_model_disabled(self, mock_urlopen, controller):
        """Test checking operation on disabled model."""
        # Mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"success": True, "data": {"model": "res.partner", "enabled": False, "operations": {}}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Check operation
        allowed, msg = controller.check_operation_allowed("res.partner", "read")
        assert allowed is False
        assert "not enabled for MCP access" in msg

    @patch("urllib.request.urlopen")
    def test_validate_model_access(self, mock_urlopen, controller):
        """Test validate_model_access method."""
        # Mock allowed response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {"model": "res.partner", "enabled": True, "operations": {"read": True}},
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Should not raise for allowed operation
        controller.validate_model_access("res.partner", "read")

        # Mock denied response
        mock_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {"model": "res.partner", "enabled": True, "operations": {"read": False}},
            }
        ).encode("utf-8")

        # Clear cache to force new request
        controller.clear_cache()

        # Should raise for denied operation
        with pytest.raises(AccessControlError):
            controller.validate_model_access("res.partner", "read")

    @patch("urllib.request.urlopen")
    def test_filter_enabled_models(self, mock_urlopen, controller):
        """Test filtering enabled models."""
        # Mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "models": [
                        {"model": "res.partner", "name": "Contact"},
                        {"model": "res.users", "name": "Users"},
                    ]
                },
            }
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Filter models
        models = ["res.partner", "account.move", "res.users", "stock.picking"]
        filtered = controller.filter_enabled_models(models)

        assert filtered == ["res.partner", "res.users"]

    @patch("urllib.request.urlopen")
    def test_get_all_permissions(self, mock_urlopen, controller):
        """Test getting permissions for all models."""
        # Mock models list response
        models_response = MagicMock()
        models_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "models": [
                        {"model": "res.partner", "name": "Contact"},
                        {"model": "res.users", "name": "Users"},
                    ]
                },
            }
        ).encode("utf-8")

        # Mock permissions responses
        partner_response = MagicMock()
        partner_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "model": "res.partner",
                    "enabled": True,
                    "operations": {"read": True, "write": True},
                },
            }
        ).encode("utf-8")

        users_response = MagicMock()
        users_response.read.return_value = json.dumps(
            {
                "success": True,
                "data": {
                    "model": "res.users",
                    "enabled": True,
                    "operations": {"read": True, "write": False},
                },
            }
        ).encode("utf-8")

        # Configure mock to return different responses
        mock_urlopen.return_value.__enter__.side_effect = [
            models_response,
            partner_response,
            users_response,
        ]

        # Get all permissions
        all_perms = controller.get_all_permissions()

        assert len(all_perms) == 2
        assert all_perms["res.partner"].can_write is True
        assert all_perms["res.users"].can_write is False


@pytest.mark.skipif(not ODOO_SERVER_AVAILABLE, reason="Odoo server not available")
class TestAccessControlIntegration:
    """Integration tests with real Odoo server."""

    @pytest.fixture
    def real_config(self):
        """Create configuration with real credentials."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key=os.getenv("ODOO_API_KEY"),
            database=os.getenv("ODOO_DB"),
        )

    def test_real_get_enabled_models(self, real_config):
        """Test getting enabled models from real server."""
        controller = AccessController(real_config)

        models = controller.get_enabled_models()

        assert isinstance(models, list)
        print(f"Found {len(models)} enabled models")

        # Just verify we got some models
        if models:
            # Print first few models as example
            for model in models[:3]:
                print(f"  - {model.get('model', 'unknown')}")

    def test_real_model_permissions(self, real_config, readable_model):
        """Test getting model permissions from real server."""
        controller = AccessController(real_config)

        # Use the discovered readable model
        model_name = readable_model.model

        # Get model permissions
        perms = controller.get_model_permissions(model_name)

        assert perms.model == model_name
        assert perms.enabled is True
        assert perms.can_read is True  # We specifically requested a readable model
        print(
            f"{model_name} permissions: read={perms.can_read}, "
            f"write={perms.can_write}, create={perms.can_create}, "
            f"unlink={perms.can_unlink}"
        )

    def test_real_check_operations(self, real_config, readable_model, disabled_model):
        """Test checking operations on real server."""
        controller = AccessController(real_config)

        # Check enabled model operations
        allowed, msg = controller.check_operation_allowed(readable_model.model, "read")
        print(f"{readable_model.model} read: allowed={allowed}, msg={msg}")
        assert allowed is True

        # Check a model we know is not enabled
        allowed, msg = controller.check_operation_allowed(disabled_model, "read")
        print(f"{disabled_model} read: allowed={allowed}, msg={msg}")
        assert allowed is False

    def test_real_validate_access(self, real_config, readable_model, disabled_model):
        """Test access validation on real server."""
        controller = AccessController(real_config)

        # Should not raise for enabled model with permission
        try:
            controller.validate_model_access(readable_model.model, "read")
            print(f"{readable_model.model} read access validated")
        except AccessControlError as e:
            print(f"{readable_model.model} read access denied: {e}")

        # Should raise for non-enabled model
        with pytest.raises(AccessControlError):
            controller.validate_model_access(disabled_model, "read")

    def test_real_cache_performance(self, real_config):
        """Test cache improves performance."""
        controller = AccessController(real_config)

        import time

        # First call - no cache
        start = time.time()
        models1 = controller.get_enabled_models()
        time1 = time.time() - start

        # Second call - from cache
        start = time.time()
        models2 = controller.get_enabled_models()
        time2 = time.time() - start

        assert models1 == models2
        assert time2 < time1  # Cache should be faster
        print(f"First call: {time1:.3f}s, Cached call: {time2:.3f}s")

    def test_real_all_permissions(self, real_config):
        """Test getting all permissions from real server."""
        controller = AccessController(real_config)

        all_perms = controller.get_all_permissions()

        print(f"Retrieved permissions for {len(all_perms)} models")

        # Print a sample
        for model, perms in list(all_perms.items())[:3]:
            print(f"{model}: read={perms.can_read}, write={perms.can_write}")


if __name__ == "__main__":
    # Run integration tests when executed directly
    pytest.main([__file__, "-v", "-k", "Integration"])
