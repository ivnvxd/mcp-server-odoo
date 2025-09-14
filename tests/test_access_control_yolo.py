"""Tests for YOLO mode access control functionality.

This module tests the AccessController class behavior in YOLO modes.
"""

import os
from unittest.mock import patch

import pytest

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig


class TestYoloModeAccessControl:
    """Test access control in YOLO mode."""

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

    def test_init_yolo_mode_skips_api_validation(self, config_yolo_read, caplog):
        """Test that YOLO mode skips API key validation."""
        # Should not raise error even without API key
        AccessController(config_yolo_read)

        # Check warning was logged
        assert "YOLO mode" in caplog.text
        assert "Access control bypassed" in caplog.text
        assert "READ-ONLY" in caplog.text

    def test_init_yolo_full_mode_warning(self, config_yolo_full, caplog):
        """Test that full YOLO mode shows appropriate warning."""
        AccessController(config_yolo_full)

        # Check warning was logged
        assert "YOLO mode" in caplog.text
        assert "FULL ACCESS" in caplog.text
        assert "MCP security disabled" in caplog.text

    def test_init_standard_mode_requires_api_key(self):
        """Test that standard mode requires API key."""
        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="off",
        )

        with pytest.raises(AccessControlError, match="API key required"):
            AccessController(config)

    def test_is_model_enabled_yolo_mode(self, config_yolo_read):
        """Test that all models are enabled in YOLO mode."""
        controller = AccessController(config_yolo_read)

        # Test various models
        assert controller.is_model_enabled("res.partner") is True
        assert controller.is_model_enabled("product.product") is True
        assert controller.is_model_enabled("ir.model") is True
        assert controller.is_model_enabled("any.random.model") is True

    def test_get_model_permissions_read_only(self, config_yolo_read):
        """Test permissions in read-only YOLO mode."""
        controller = AccessController(config_yolo_read)

        permissions = controller.get_model_permissions("res.partner")

        assert permissions.model == "res.partner"
        assert permissions.enabled is True
        assert permissions.can_read is True
        assert permissions.can_write is False
        assert permissions.can_create is False
        assert permissions.can_unlink is False

    def test_get_model_permissions_full_access(self, config_yolo_full):
        """Test permissions in full access YOLO mode."""
        controller = AccessController(config_yolo_full)

        permissions = controller.get_model_permissions("res.partner")

        assert permissions.model == "res.partner"
        assert permissions.enabled is True
        assert permissions.can_read is True
        assert permissions.can_write is True
        assert permissions.can_create is True
        assert permissions.can_unlink is True

    def test_check_operation_allowed_read_only(self, config_yolo_read):
        """Test operation checks in read-only YOLO mode."""
        controller = AccessController(config_yolo_read)

        # Read operations should be allowed
        for op in ["read", "search", "search_read", "fields_get", "count", "search_count"]:
            allowed, msg = controller.check_operation_allowed("res.partner", op)
            assert allowed is True
            assert msg is None

        # Write operations should be blocked
        for op in ["write", "create", "unlink"]:
            allowed, msg = controller.check_operation_allowed("res.partner", op)
            assert allowed is False
            assert "not allowed in read-only YOLO mode" in msg
            assert "Only read operations are permitted" in msg

    def test_check_operation_allowed_full_access(self, config_yolo_full):
        """Test operation checks in full access YOLO mode."""
        controller = AccessController(config_yolo_full)

        # All operations should be allowed
        for op in ["read", "search", "write", "create", "unlink", "delete"]:
            allowed, msg = controller.check_operation_allowed("res.partner", op)
            assert allowed is True
            assert msg is None

    def test_filter_enabled_models_yolo_mode(self, config_yolo_read):
        """Test that all models pass filter in YOLO mode."""
        controller = AccessController(config_yolo_read)

        models = ["res.partner", "product.product", "ir.model", "custom.model"]
        filtered = controller.filter_enabled_models(models)

        assert filtered == models  # All models should pass

    def test_get_enabled_models_yolo_mode(self, config_yolo_read):
        """Test that get_enabled_models returns empty list in YOLO mode."""
        controller = AccessController(config_yolo_read)

        # Should return empty list (indicating all models allowed)
        models = controller.get_enabled_models()
        assert models == []

    def test_validate_model_access_read_only(self, config_yolo_read):
        """Test validate_model_access in read-only YOLO mode."""
        controller = AccessController(config_yolo_read)

        # Read should succeed
        controller.validate_model_access("res.partner", "read")  # Should not raise

        # Write should raise
        with pytest.raises(AccessControlError, match="not allowed in read-only"):
            controller.validate_model_access("res.partner", "write")

    def test_validate_model_access_full_access(self, config_yolo_full):
        """Test validate_model_access in full access YOLO mode."""
        controller = AccessController(config_yolo_full)

        # All operations should succeed
        controller.validate_model_access("res.partner", "read")  # Should not raise
        controller.validate_model_access("res.partner", "write")  # Should not raise
        controller.validate_model_access("res.partner", "create")  # Should not raise
        controller.validate_model_access("res.partner", "unlink")  # Should not raise

    def test_no_api_calls_in_yolo_mode(self, config_yolo_read):
        """Test that no API calls are made to MCP endpoints in YOLO mode."""
        controller = AccessController(config_yolo_read)

        # Mock _make_request to ensure it's never called
        with patch.object(controller, "_make_request") as mock_request:
            # These operations should not trigger API calls
            controller.is_model_enabled("res.partner")
            controller.get_model_permissions("res.partner")
            controller.check_operation_allowed("res.partner", "read")
            controller.filter_enabled_models(["res.partner"])
            controller.get_enabled_models()

            # Verify no API calls were made
            mock_request.assert_not_called()

    def test_mode_transition_standard_to_yolo(self, config_yolo_read):
        """Test that we can switch from standard to YOLO mode."""
        # Create standard mode config without API key
        config_no_api = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
            yolo_mode="off",  # Standard mode
        )

        # Standard mode should fail without API key
        with pytest.raises(AccessControlError, match="API key required"):
            AccessController(config_no_api)

        # Switch to YOLO mode (new controller)
        controller_yolo = AccessController(config_yolo_read)

        # Should work without API
        assert controller_yolo.is_model_enabled("res.partner") is True

    def test_permission_caching_disabled_in_yolo(self, config_yolo_read):
        """Test that caching is effectively bypassed in YOLO mode."""
        controller = AccessController(config_yolo_read)

        # Get permissions twice
        perms1 = controller.get_model_permissions("res.partner")
        perms2 = controller.get_model_permissions("res.partner")

        # Should return consistent results
        assert perms1.can_read == perms2.can_read
        assert perms1.can_write == perms2.can_write

    def test_error_messages_clarity(self, config_yolo_read):
        """Test that error messages are clear in YOLO mode."""
        controller = AccessController(config_yolo_read)

        # Try a write operation
        allowed, msg = controller.check_operation_allowed("res.partner", "create")

        assert allowed is False
        assert "create" in msg  # Operation name should be in message
        assert "read-only YOLO mode" in msg  # Mode should be clear
        assert "Only read operations are permitted" in msg  # Guidance provided

    def test_all_read_operation_types(self, config_yolo_read):
        """Test that all read operation types are recognized."""
        controller = AccessController(config_yolo_read)

        # These should all be considered read operations
        read_ops = ["read", "search", "search_read", "fields_get", "count", "search_count"]

        for op in read_ops:
            allowed, msg = controller.check_operation_allowed("any.model", op)
            assert allowed is True, f"Operation {op} should be allowed in read-only mode"
            assert msg is None

    def test_all_write_operation_types(self, config_yolo_read):
        """Test that all write operation types are blocked in read-only."""
        controller = AccessController(config_yolo_read)

        # These should all be considered write operations
        # Note: "update" and "remove" are not standard Odoo operations,
        # so they would return False but without specific error messages
        write_ops = ["write", "create", "unlink", "delete"]

        for op in write_ops:
            allowed, msg = controller.check_operation_allowed("any.model", op)
            assert allowed is False, f"Operation {op} should be blocked in read-only mode"
            assert msg is not None
            assert "not allowed in read-only" in msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
