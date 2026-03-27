"""Tests for SecurityPolicy, MutationPolicy, and ConfirmationManager."""

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.security import (
    ConfirmationManager,
    MutationPolicy,
    SecurityPolicy,
    SecurityPolicyError,
)


def _make_config(**overrides) -> OdooConfig:
    """Create a minimal config for testing."""
    defaults = {
        "url": "http://localhost:8069",
        "username": "admin",
        "password": "admin",
        "yolo_mode": "read",
    }
    defaults.update(overrides)
    return OdooConfig(**defaults)


# --- SecurityPolicy: Model Allowlist ---


class TestModelAllowlist:
    def test_model_allowed_when_in_list(self):
        config = _make_config(allowed_models=["res.partner", "res.company"])
        policy = SecurityPolicy(config)
        policy.check_model_allowed("res.partner")  # should not raise

    def test_model_blocked_when_not_in_list(self):
        config = _make_config(allowed_models=["res.partner"])
        policy = SecurityPolicy(config)
        with pytest.raises(SecurityPolicyError, match="not in the allowed models"):
            policy.check_model_allowed("sale.order")

    def test_all_models_allowed_when_no_list(self):
        config = _make_config(allowed_models=None)
        policy = SecurityPolicy(config)
        policy.check_model_allowed("anything.at.all")  # should not raise

    def test_is_model_allowed_returns_bool(self):
        config = _make_config(allowed_models=["res.partner"])
        policy = SecurityPolicy(config)
        assert policy.is_model_allowed("res.partner") is True
        assert policy.is_model_allowed("sale.order") is False

    def test_admin_mode_bypasses_allowlist(self):
        config = _make_config(allowed_models=["res.partner"], admin_mode=True)
        policy = SecurityPolicy(config)
        policy.check_model_allowed("sale.order")  # should not raise


# --- SecurityPolicy: Operation Allowlist ---


class TestOperationAllowlist:
    def test_read_operation_allowed_by_default(self):
        config = _make_config()
        policy = SecurityPolicy(config)
        policy.check_operation_allowed("res.partner", "search_read")

    def test_unknown_operation_blocked(self):
        config = _make_config()
        policy = SecurityPolicy(config)
        with pytest.raises(SecurityPolicyError, match="not in the allowed operations"):
            policy.check_operation_allowed("res.partner", "execute_kw")

    def test_per_model_operation_map(self):
        config = _make_config(
            model_operation_map={"res.partner": ["read", "create"]},
        )
        policy = SecurityPolicy(config)
        policy.check_operation_allowed("res.partner", "read")
        policy.check_operation_allowed("res.partner", "create")
        with pytest.raises(SecurityPolicyError, match="not allowed on model"):
            policy.check_operation_allowed("res.partner", "write")

    def test_model_not_in_map_uses_global(self):
        config = _make_config(
            model_operation_map={"res.partner": ["read"]},
        )
        policy = SecurityPolicy(config)
        # sale.order is not in the map, so global lists apply
        policy.check_operation_allowed("sale.order", "search_read")

    def test_admin_mode_bypasses_operations(self):
        config = _make_config(admin_mode=True)
        policy = SecurityPolicy(config)
        policy.check_operation_allowed("any.model", "anything")


# --- SecurityPolicy: Field Filtering ---


class TestFieldFiltering:
    def test_no_filtering_when_no_lists(self):
        config = _make_config()
        policy = SecurityPolicy(config)
        fields = ["name", "email", "phone"]
        result = policy.filter_read_fields("res.partner", fields)
        assert result == fields

    def test_allowlist_filters_fields(self):
        config = _make_config(
            field_allowlists={"res.partner": ["name", "email"]},
        )
        policy = SecurityPolicy(config)
        result = policy.filter_read_fields("res.partner", ["name", "email", "phone", "id"])
        assert set(result) == {"name", "email", "id"}  # id is protected

    def test_denylist_removes_fields(self):
        config = _make_config(
            field_denylists={"res.partner": ["password_crypt", "secret"]},
        )
        policy = SecurityPolicy(config)
        result = policy.filter_read_fields(
            "res.partner", ["name", "email", "password_crypt", "secret"]
        )
        assert set(result) == {"name", "email"}

    def test_protected_fields_never_removed(self):
        config = _make_config(
            field_allowlists={"res.partner": ["name"]},
        )
        policy = SecurityPolicy(config)
        result = policy.filter_read_fields("res.partner", ["id", "display_name", "name", "phone"])
        assert "id" in result
        assert "display_name" in result

    def test_write_fields_strips_system_fields(self):
        config = _make_config()
        policy = SecurityPolicy(config)
        values = {"name": "Test", "create_date": "2024-01-01", "write_uid": 1}
        result = policy.filter_write_fields("res.partner", values)
        assert "name" in result
        assert "create_date" not in result
        assert "write_uid" not in result

    def test_write_fields_applies_allowlist(self):
        config = _make_config(
            field_allowlists={"res.partner": ["name", "email"]},
        )
        policy = SecurityPolicy(config)
        values = {"name": "Test", "email": "test@test.com", "phone": "123"}
        result = policy.filter_write_fields("res.partner", values)
        assert "name" in result
        assert "email" in result
        assert "phone" not in result

    def test_none_fields_returns_allowlist(self):
        config = _make_config(
            field_allowlists={"res.partner": ["name", "email"]},
        )
        policy = SecurityPolicy(config)
        result = policy.filter_read_fields("res.partner", None)
        assert result is not None
        assert "name" in result
        assert "email" in result

    def test_admin_mode_skips_filtering(self):
        config = _make_config(
            field_allowlists={"res.partner": ["name"]},
            admin_mode=True,
        )
        policy = SecurityPolicy(config)
        result = policy.filter_read_fields("res.partner", ["name", "email", "phone"])
        assert result == ["name", "email", "phone"]


# --- SecurityPolicy: Limit Clamping ---


class TestLimitClamping:
    def test_clamp_to_max(self):
        config = _make_config(max_records_per_query=50)
        policy = SecurityPolicy(config)
        assert policy.clamp_limit(100) == 50

    def test_no_clamp_when_under_max(self):
        config = _make_config(max_records_per_query=100)
        policy = SecurityPolicy(config)
        assert policy.clamp_limit(50) == 50

    def test_none_returns_default(self):
        config = _make_config(default_limit=10, max_records_per_query=100)
        policy = SecurityPolicy(config)
        assert policy.clamp_limit(None) == 10

    def test_zero_returns_default(self):
        config = _make_config(default_limit=10, max_records_per_query=100)
        policy = SecurityPolicy(config)
        assert policy.clamp_limit(0) == 10


# --- MutationPolicy ---


class TestMutationPolicy:
    def test_mutations_blocked_by_default(self):
        config = _make_config(enable_mutations=False)
        policy = MutationPolicy(config)
        with pytest.raises(SecurityPolicyError, match="Mutations are disabled"):
            policy.check_mutation_allowed("create", "res.partner")

    def test_mutations_allowed_when_enabled(self):
        config = _make_config(enable_mutations=True)
        policy = MutationPolicy(config)
        policy.check_mutation_allowed("create", "res.partner")

    def test_deletes_blocked_when_mutations_disabled(self):
        config = _make_config(enable_mutations=False, enable_deletes=False)
        policy = MutationPolicy(config)
        with pytest.raises(SecurityPolicyError, match="Mutations are disabled"):
            policy.check_delete_allowed("res.partner")

    def test_deletes_blocked_when_deletes_disabled(self):
        config = _make_config(enable_mutations=True, enable_deletes=False)
        policy = MutationPolicy(config)
        with pytest.raises(SecurityPolicyError, match="Deletes are disabled"):
            policy.check_delete_allowed("res.partner")

    def test_deletes_allowed_when_both_enabled(self):
        config = _make_config(enable_mutations=True, enable_deletes=True)
        policy = MutationPolicy(config)
        policy.check_delete_allowed("res.partner")

    def test_admin_mode_bypasses_mutation_check(self):
        config = _make_config(enable_mutations=False, admin_mode=True)
        policy = MutationPolicy(config)
        policy.check_mutation_allowed("create", "res.partner")
        policy.check_delete_allowed("res.partner")


# --- ConfirmationManager ---


class TestConfirmationManager:
    def test_generate_and_validate_token(self):
        config = _make_config(enable_mutations=True, require_confirmation_for_mutations=True)
        mgr = ConfirmationManager(config)
        token = mgr.generate_token("create", "res.partner", "Create new partner")
        assert mgr.validate_token(token) is True

    def test_token_consumed_after_validation(self):
        config = _make_config(enable_mutations=True, require_confirmation_for_mutations=True)
        mgr = ConfirmationManager(config)
        token = mgr.generate_token("create", "res.partner", "Create new partner")
        assert mgr.validate_token(token) is True
        assert mgr.validate_token(token) is False  # consumed

    def test_invalid_token_rejected(self):
        config = _make_config(enable_mutations=True, require_confirmation_for_mutations=True)
        mgr = ConfirmationManager(config)
        assert mgr.validate_token("fake-token") is False

    def test_requires_confirmation_property(self):
        config1 = _make_config(enable_mutations=True, require_confirmation_for_mutations=True)
        config2 = _make_config(enable_mutations=True, require_confirmation_for_mutations=False)
        config3 = _make_config(enable_mutations=False, require_confirmation_for_mutations=True)
        assert ConfirmationManager(config1).requires_confirmation is True
        assert ConfirmationManager(config2).requires_confirmation is False
        assert ConfirmationManager(config3).requires_confirmation is False
