"""Tests for access control integration with Odoo MCP module.

This module tests the AccessController class and its integration with
the Odoo MCP module's REST API endpoints.
"""

import io
import json
import os
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.access_control import (
    AccessControlError,
    AccessController,
    access_denied_message,
)
from mcp_server_odoo.config import OdooConfig


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

    def test_init_without_api_key_with_credentials(self):
        """Test initialization with credentials (no API key) prepares for session auth."""
        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
            database=os.getenv("ODOO_DB"),
        )

        controller = AccessController(config, database="testdb")
        assert controller.config == config
        assert controller._session_id is None
        assert controller.database == "testdb"

    def test_init_without_any_auth(self, caplog):
        """Test initialization without any auth logs a warning."""
        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="dummy",  # Need some auth to pass config validation
            database=os.getenv("ODOO_DB"),
        )
        # Simulate no auth by clearing after construction
        config.api_key = None
        config.username = None
        config.password = None

        AccessController(config)
        assert "No authentication configured" in caplog.text

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

        with pytest.raises(AccessControlError, match="API key rejected"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_http_403_surfaces_the_module_message(self, mock_urlopen, controller):
        """The module says exactly what was refused — keep its wording.

        Replacing it with a generic denial sends users hunting a credential
        problem when the real cause is an un-enabled model.
        """
        body = json.dumps(
            {
                "success": False,
                "error": {
                    "message": "Model 'ir.attachment' is not enabled for MCP access.",
                    "code": "E403",
                },
            }
        ).encode("utf-8")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            None, 403, "Forbidden", {}, io.BytesIO(body)
        )

        with pytest.raises(AccessControlError, match="not enabled for MCP access"):
            controller._make_request("/test/endpoint")

    @pytest.mark.parametrize(
        "body",
        [
            b"<html>not json</html>",
            json.dumps({"success": False}).encode("utf-8"),
            json.dumps({"success": False, "error": {"message": "   "}}).encode("utf-8"),
            json.dumps({"success": False, "error": {"message": None}}).encode("utf-8"),
        ],
        ids=["not-json", "no-error-key", "blank-message", "null-message"],
    )
    @patch("urllib.request.urlopen")
    def test_make_request_http_403_falls_back_when_body_is_unusable(
        self, mock_urlopen, controller, body
    ):
        """An unreadable or empty body must not produce an empty error string."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            None, 403, "Forbidden", {}, io.BytesIO(body)
        )

        with pytest.raises(AccessControlError, match="Access denied to MCP endpoints"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_http_403(self, mock_urlopen, controller):
        """Test REST API request with 403 error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(None, 403, "Forbidden", {}, None)

        with pytest.raises(AccessControlError, match="Access denied to MCP endpoints"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_http_404(self, mock_urlopen, controller):
        """Test REST API request with 404 error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(None, 404, "Not Found", {}, None)

        with pytest.raises(AccessControlError, match="Endpoint not found"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_http_500(self, mock_urlopen, controller):
        """Test REST API request with 500 error returns generic HTTP error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            None, 500, "Internal Server Error", {}, None
        )

        with pytest.raises(AccessControlError, match="HTTP error 500"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_url_error(self, mock_urlopen, controller):
        """Test REST API request with URLError (connection refused)."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with pytest.raises(AccessControlError, match="Connection error"):
            controller._make_request("/test/endpoint")

    @patch("urllib.request.urlopen")
    def test_make_request_json_decode_error(self, mock_urlopen, controller):
        """Test REST API request with malformed JSON response."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"not valid json"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with pytest.raises(AccessControlError, match="Invalid JSON response"):
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

        # Ensure clock has advanced past TTL
        time.sleep(0.01)

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
    def test_password_fallback_does_not_send_rejected_api_key(self, mock_urlopen):
        """After a password-auth fallback, permission checks must use session
        auth instead of resending the API key Odoo already rejected."""
        from mcp_server_odoo.config import OdooConfig

        config = OdooConfig(
            url="http://localhost:8069",
            api_key="rejected_key",
            username="admin",
            password="admin",
            database="test_db",
        )
        controller = AccessController(config, auth_method="password")

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"success": True, "data": {"models": []}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        with patch.object(controller, "_ensure_session") as mock_session:
            # The header must be built from _ensure_session's return value:
            # a concurrent 401 handler nulls the attribute, so leave it None
            # here — code that re-reads it would send "session_id=None".
            mock_session.return_value = "fresh_session"
            controller._session_id = None
            controller._do_request("/mcp/models", timeout=5, allow_session_retry=True)
            mock_session.assert_called_once()

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("X-api-key") is None
        cookie = request.get_header("Cookie", "")
        assert "session_id=fresh_session" in cookie
        assert "session_id=None" not in cookie

    @patch("urllib.request.urlopen")
    def test_api_key_auth_method_still_sends_key(self, mock_urlopen):
        from mcp_server_odoo.config import OdooConfig

        config = OdooConfig(
            url="http://localhost:8069",
            api_key="good_key",
            database="test_db",
        )
        controller = AccessController(config, auth_method="api_key")

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"success": True, "data": {"models": []}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_response

        controller._do_request("/mcp/models", timeout=5, allow_session_retry=True)

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("X-api-key") == "good_key"

    @patch("urllib.request.urlopen")
    def test_infrastructure_failure_is_not_reported_as_denial(self, mock_urlopen, controller):
        """A network outage must surface as 'could not evaluate', not 'denied'."""
        import urllib.error

        from mcp_server_odoo.access_control import AccessControlUnavailableError

        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        with pytest.raises(AccessControlUnavailableError, match="Connection error"):
            controller.check_operation_allowed("res.partner", "read")

        # validate_model_access propagates the same distinction (fail closed,
        # but retryable connection error rather than a permission denial)
        with pytest.raises(AccessControlUnavailableError):
            controller.validate_model_access("res.partner", "read")

    @patch("urllib.request.urlopen")
    def test_genuine_denial_still_reads_as_denial(self, mock_urlopen, controller):
        """403 from the MCP endpoints remains a plain denial."""
        import urllib.error

        from mcp_server_odoo.access_control import (
            AccessControlError,
            AccessControlUnavailableError,
        )

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://test", code=403, msg="Forbidden", hdrs=None, fp=None
        )

        allowed, msg = controller.check_operation_allowed("res.partner", "read")
        assert allowed is False
        assert "Access denied" in msg

        with pytest.raises(AccessControlError) as exc_info:
            controller.validate_model_access("res.partner", "read")
        # 403 must not classify as infrastructure failure
        assert not isinstance(exc_info.value, AccessControlUnavailableError)

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


class TestSessionAuth:
    """Test session-based authentication for access control."""

    @pytest.fixture
    def cred_config(self):
        """Create config with only username/password (no API key)."""
        return OdooConfig(
            url="http://localhost:8069",
            username="admin",
            password="admin",
            database="testdb",
        )

    @pytest.fixture
    def cred_controller(self, cred_config):
        """Create AccessController with credentials-only config."""
        return AccessController(cred_config, database="testdb")

    @patch("urllib.request.urlopen")
    def test_session_auth_on_first_request(self, mock_urlopen, cred_controller):
        """Test that session auth happens lazily on first REST request."""
        # First call: session authenticate
        session_response = MagicMock()
        session_response.headers = {"Set-Cookie": "session_id=abc123; Path=/"}
        session_response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"uid": 2}}
        ).encode()

        # Second call: actual REST request
        rest_response = MagicMock()
        rest_response.read.return_value = json.dumps(
            {"success": True, "data": {"models": []}}
        ).encode()

        mock_urlopen.return_value.__enter__.side_effect = [session_response, rest_response]

        cred_controller._make_request("/mcp/models")

        assert cred_controller._session_id == "abc123"
        assert mock_urlopen.call_count == 2

    @patch("urllib.request.urlopen")
    def test_session_reuses_cookie(self, mock_urlopen, cred_controller):
        """Test that subsequent requests reuse the session cookie."""
        cred_controller._session_id = "existing_session"

        rest_response = MagicMock()
        rest_response.read.return_value = json.dumps(
            {"success": True, "data": {"models": []}}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = rest_response

        cred_controller._make_request("/mcp/models")

        # Should only make one call (no session auth needed)
        mock_urlopen.assert_called_once()
        # Verify cookie was sent
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.get_header("Cookie") == "session_id=existing_session"

    @patch("urllib.request.urlopen")
    def test_session_retry_on_401(self, mock_urlopen, cred_controller):
        """Test that expired session triggers re-auth and retry."""
        cred_controller._session_id = "expired_session"

        # First call: 401 (expired session)
        # Second call: session authenticate
        session_response = MagicMock()
        session_response.headers = {"Set-Cookie": "session_id=new_session; Path=/"}
        session_response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"uid": 2}}
        ).encode()

        # Third call: retry REST request
        rest_response = MagicMock()
        rest_response.read.return_value = json.dumps(
            {"success": True, "data": {"models": []}}
        ).encode()

        mock_urlopen.return_value.__enter__.side_effect = [
            urllib.error.HTTPError(None, 401, "Unauthorized", {}, None),
            session_response,
            rest_response,
        ]
        # The first call raises, so side_effect on __enter__ won't work for HTTPError.
        # Instead, configure side_effect on urlopen itself for the first call.
        call_count = 0

        def urlopen_side_effect(req, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)
            return mock_urlopen.return_value

        mock_urlopen.side_effect = urlopen_side_effect
        mock_urlopen.return_value.__enter__.side_effect = [session_response, rest_response]

        cred_controller._make_request("/mcp/models")

        assert cred_controller._session_id == "new_session"

    @patch("urllib.request.urlopen")
    def test_session_auth_failure(self, mock_urlopen, cred_controller):
        """Test session auth with invalid credentials."""
        mock_urlopen.side_effect = urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)

        with pytest.raises(AccessControlError, match="Session authentication failed"):
            cred_controller._authenticate_session()

    @patch("urllib.request.urlopen")
    def test_session_auth_no_cookie(self, mock_urlopen, cred_controller):
        """Test session auth when server returns no session cookie."""
        response = MagicMock()
        response.headers = {"Set-Cookie": ""}
        response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"uid": 2}}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        with pytest.raises(AccessControlError, match="no session cookie"):
            cred_controller._authenticate_session()

    @patch("urllib.request.urlopen")
    def test_session_retry_disabled(self, mock_urlopen, cred_controller):
        """Test that allow_session_retry=False raises on 401 without retrying."""
        cred_controller._session_id = "some_session"

        mock_urlopen.side_effect = urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)

        with pytest.raises(AccessControlError, match="authentication failed"):
            cred_controller._do_request("/mcp/models", timeout=30, allow_session_retry=False)

        # Only one call — no retry attempt
        mock_urlopen.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_session_auth_json_error(self, mock_urlopen, cred_controller):
        """Test session auth when server returns JSON-RPC error."""
        response = MagicMock()
        response.headers = {"Set-Cookie": "session_id=abc; Path=/"}
        response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"message": "Access denied"}}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = response

        with pytest.raises(AccessControlError, match="invalid credentials"):
            cred_controller._authenticate_session()


@pytest.mark.mcp
class TestAccessControlIntegration:
    """Integration tests with real Odoo server."""

    @pytest.fixture
    def real_config(self):
        """Create configuration with real credentials."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key=os.getenv("ODOO_API_KEY") or None,
            username=os.getenv("ODOO_USER") or None,
            password=os.getenv("ODOO_PASSWORD") or None,
            database=os.getenv("ODOO_DB"),
            yolo_mode=os.getenv("ODOO_YOLO", "off"),
        )

    def test_real_get_enabled_models(self, real_config):
        """Test getting enabled models from real server."""
        controller = AccessController(real_config)

        models = controller.get_enabled_models()

        assert models, "MCP test instance must have at least one enabled model"
        print(f"Found {len(models)} enabled models")

        # Each entry must identify a model
        for model in models:
            assert model.get("model"), f"enabled model entry without model name: {model}"

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
        controller.validate_model_access(readable_model.model, "read")
        print(f"{readable_model.model} read access validated")

        # Should raise for non-enabled model
        with pytest.raises(AccessControlError):
            controller.validate_model_access(disabled_model, "read")

    def test_real_cache_performance(self, real_config):
        """Test cache returns consistent results on repeated calls."""
        controller = AccessController(real_config)

        # First call populates cache
        models1 = controller.get_enabled_models()

        # Second call should return from cache
        models2 = controller.get_enabled_models()

        assert models1 == models2

        # Verify cache is populated (deterministic check instead of timing)
        cached = controller._get_from_cache("enabled_models")
        assert cached is not None

    def test_real_all_permissions(self, real_config):
        """Test getting all permissions from real server."""
        controller = AccessController(real_config)

        all_perms = controller.get_all_permissions()

        assert isinstance(all_perms, dict)
        assert len(all_perms) > 0

        # Verify permission structure
        for model, perms in list(all_perms.items())[:3]:
            assert hasattr(perms, "can_read")
            print(f"{model}: read={perms.can_read}, write={perms.can_write}")


if __name__ == "__main__":
    # Run integration tests when executed directly
    pytest.main([__file__, "-v", "-k", "Integration"])


class TestAccessDeniedMessage:
    """The MCP module labels its own refusals; do not label them twice."""

    def test_self_labelled_refusal_is_not_double_prefixed(self):
        e = AccessControlError(
            "Access denied: your user is not authorized for MCP. "
            "Ask your Odoo administrator for the 'MCP User' group."
        )

        message = access_denied_message(e)

        assert message.lower().count("access denied") == 1
        assert message.startswith("Access denied: your user is not authorized")

    @pytest.mark.parametrize(
        "raw",
        ["Access denied", "access denied: nope", "ACCESS DENIED: nope"],
        ids=["bare", "lower", "upper"],
    )
    def test_prefix_detection_is_case_insensitive(self, raw):
        assert (
            not access_denied_message(AccessControlError(raw))
            .lower()
            .startswith("access denied: access denied")
        )

    def test_unlabelled_refusal_gets_the_prefix(self):
        e = AccessControlError("Operation 'create' not allowed on model 'res.partner'")

        assert access_denied_message(e) == (
            "Access denied: Operation 'create' not allowed on model 'res.partner'"
        )
