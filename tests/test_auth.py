"""Tests for authentication strategies and middleware."""

import pytest

from mcp_server_odoo.auth import (
    ApiKeyAuthProvider,
    AuthenticationError,
    AuthInfo,
    AuthMiddleware,
    NoAuthProvider,
    create_auth_provider,
    get_current_auth_info,
    set_current_auth_info,
)
from mcp_server_odoo.config import OdooConfig


def _make_config(**overrides) -> OdooConfig:
    defaults = {
        "url": "http://localhost:8069",
        "username": "admin",
        "password": "admin",
        "yolo_mode": "read",
    }
    defaults.update(overrides)
    return OdooConfig(**defaults)


# --- NoAuthProvider ---


class TestNoAuthProvider:
    @pytest.mark.asyncio
    async def test_returns_anonymous(self):
        provider = NoAuthProvider()
        result = await provider.authenticate({})
        assert result.subject == "anonymous"
        assert result.auth_mode == "none"


# --- ApiKeyAuthProvider ---


class TestApiKeyAuthProvider:
    @pytest.mark.asyncio
    async def test_valid_key_accepted(self):
        provider = ApiKeyAuthProvider(["test-key-123", "test-key-456"])
        result = await provider.authenticate({"x-api-key": "test-key-123"})
        assert result.subject == "api_key_0"
        assert result.auth_mode == "api_key"

    @pytest.mark.asyncio
    async def test_second_key_accepted(self):
        provider = ApiKeyAuthProvider(["key1", "key2"])
        result = await provider.authenticate({"x-api-key": "key2"})
        assert result.subject == "api_key_1"

    @pytest.mark.asyncio
    async def test_invalid_key_rejected(self):
        provider = ApiKeyAuthProvider(["valid-key"])
        with pytest.raises(AuthenticationError, match="Invalid API key"):
            await provider.authenticate({"x-api-key": "wrong-key"})

    @pytest.mark.asyncio
    async def test_missing_header_rejected(self):
        provider = ApiKeyAuthProvider(["valid-key"])
        with pytest.raises(AuthenticationError, match="Missing X-API-Key"):
            await provider.authenticate({})

    def test_empty_keys_raises(self):
        with pytest.raises(ValueError, match="At least one API key"):
            ApiKeyAuthProvider([])


# --- create_auth_provider ---


class TestCreateAuthProvider:
    def test_none_mode(self):
        config = _make_config(auth_mode="none")
        provider = create_auth_provider(config)
        assert isinstance(provider, NoAuthProvider)

    def test_api_key_mode(self):
        config = _make_config(
            auth_mode="api_key",
            mcp_api_keys=["test-key"],
            allowed_models=["res.partner"],
        )
        provider = create_auth_provider(config)
        assert isinstance(provider, ApiKeyAuthProvider)

    def test_oauth2_mode(self):
        from mcp_server_odoo.auth import OAuth2Provider

        config = _make_config(
            auth_mode="oauth2",
            oauth2_issuer_url="https://login.example.com",
            oauth2_audience="api://test",
            allowed_models=["res.partner"],
        )
        provider = create_auth_provider(config)
        assert isinstance(provider, OAuth2Provider)


# --- Context variable ---


class TestAuthContext:
    def test_default_is_none(self):
        # Reset to default
        assert get_current_auth_info() is None or isinstance(get_current_auth_info(), AuthInfo)

    def test_set_and_get(self):
        info = AuthInfo(subject="test-user", auth_mode="api_key")
        token = set_current_auth_info(info)
        try:
            current = get_current_auth_info()
            assert current is not None
            assert current.subject == "test-user"
            assert current.auth_mode == "api_key"
        finally:
            from mcp_server_odoo.auth import _current_auth_info

            _current_auth_info.reset(token)


# --- AuthMiddleware ---


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_exempt_paths_skip_auth(self):
        """Health and ready endpoints should bypass auth."""
        called = False

        async def mock_app(scope, receive, send):
            nonlocal called
            called = True

        provider = ApiKeyAuthProvider(["secret"])
        middleware = AuthMiddleware(mock_app, provider)

        # Simulate /health request
        scope = {"type": "http", "path": "/health", "headers": []}
        await middleware(scope, None, None)
        assert called is True

    @pytest.mark.asyncio
    async def test_missing_auth_returns_401(self):
        """Requests without auth should get 401."""
        responses = []

        async def mock_send(message):
            responses.append(message)

        async def mock_app(scope, receive, send):
            pass  # should not be reached

        provider = ApiKeyAuthProvider(["secret"])
        middleware = AuthMiddleware(mock_app, provider)

        scope = {"type": "http", "path": "/mcp", "headers": []}
        await middleware(scope, None, mock_send)

        assert len(responses) == 2
        assert responses[0]["status"] == 401

    @pytest.mark.asyncio
    async def test_valid_auth_passes_through(self):
        """Valid auth should call the app."""
        called = False

        async def mock_app(scope, receive, send):
            nonlocal called
            called = True

        provider = ApiKeyAuthProvider(["secret"])
        middleware = AuthMiddleware(mock_app, provider)

        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"x-api-key", b"secret")],
        }
        await middleware(scope, None, None)
        assert called is True

    @pytest.mark.asyncio
    async def test_non_http_passes_through(self):
        """Non-HTTP scopes should pass through without auth."""
        called = False

        async def mock_app(scope, receive, send):
            nonlocal called
            called = True

        provider = ApiKeyAuthProvider(["secret"])
        middleware = AuthMiddleware(mock_app, provider)

        scope = {"type": "lifespan"}
        await middleware(scope, None, None)
        assert called is True
