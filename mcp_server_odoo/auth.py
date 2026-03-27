"""Authentication layer for MCP client connections.

Provides auth strategies (NoAuth, API Key, OAuth2) and ASGI middleware
for authenticating MCP clients connecting to the remote server.
"""

import contextvars
import hmac
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import OdooConfig

logger = logging.getLogger(__name__)

# Context variable for propagating auth info to tool handlers
_current_auth_info: contextvars.ContextVar["AuthInfo"] = contextvars.ContextVar(
    "auth_info", default=None
)


def get_current_auth_info() -> Optional["AuthInfo"]:
    """Get the current request's auth info from context."""
    return _current_auth_info.get()


def set_current_auth_info(info: "AuthInfo") -> contextvars.Token:
    """Set auth info in the current context."""
    return _current_auth_info.set(info)


@dataclass
class AuthInfo:
    """Authenticated request metadata."""

    subject: str
    auth_mode: str  # "none" | "api_key" | "oauth2"
    scopes: List[str] = field(default_factory=list)
    claims: Dict[str, Any] = field(default_factory=dict)


# Singleton for anonymous/local access
ANONYMOUS_AUTH = AuthInfo(subject="anonymous", auth_mode="none")


class AuthProvider(ABC):
    """Abstract base for authentication strategies."""

    @abstractmethod
    async def authenticate(self, headers: Dict[str, str]) -> AuthInfo:
        """Authenticate a request from its headers.

        Returns AuthInfo on success.
        Raises AuthenticationError on failure.
        """


class AuthenticationError(Exception):
    """Raised when authentication fails."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


class NoAuthProvider(AuthProvider):
    """No authentication. For local development only."""

    async def authenticate(self, headers: Dict[str, str]) -> AuthInfo:
        return ANONYMOUS_AUTH


class ApiKeyAuthProvider(AuthProvider):
    """Validates X-API-Key header against configured keys."""

    HEADER_NAME = "x-api-key"

    def __init__(self, api_keys: List[str]):
        if not api_keys:
            raise ValueError("At least one API key must be configured")
        self._api_keys = api_keys

    async def authenticate(self, headers: Dict[str, str]) -> AuthInfo:
        key = headers.get(self.HEADER_NAME) or headers.get("X-API-Key")
        if not key:
            raise AuthenticationError("Missing X-API-Key header. Provide a valid API key.", 401)

        # Timing-safe comparison against all configured keys
        for i, valid_key in enumerate(self._api_keys):
            if hmac.compare_digest(key.encode(), valid_key.encode()):
                return AuthInfo(
                    subject=f"api_key_{i}",
                    auth_mode="api_key",
                )

        raise AuthenticationError("Invalid API key.", 401)


class OAuth2Provider(AuthProvider):
    """Validates Bearer tokens via JWKS (JWT) or introspection.

    Compatible with:
    - Microsoft Entra ID
    - Auth0
    - Keycloak
    - Any standard OIDC provider
    """

    JWKS_CACHE_TTL = 3600  # 1 hour

    def __init__(
        self,
        issuer_url: str,
        audience: str,
        jwks_url: Optional[str] = None,
        required_scopes: Optional[List[str]] = None,
    ):
        self.issuer_url = issuer_url.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self.required_scopes = set(required_scopes) if required_scopes else set()
        self._jwks_cache: Optional[Dict[str, Any]] = None
        self._jwks_cache_time: float = 0

    async def authenticate(self, headers: Dict[str, str]) -> AuthInfo:
        auth_header = headers.get("authorization") or headers.get("Authorization")
        if not auth_header:
            raise AuthenticationError("Missing Authorization header. Provide a Bearer token.", 401)

        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AuthenticationError("Invalid Authorization header format. Use 'Bearer <token>'.")

        token = parts[1].strip()
        if not token:
            raise AuthenticationError("Empty Bearer token.")

        claims = await self._validate_token(token)

        # Extract subject
        subject = claims.get("sub") or claims.get("oid") or claims.get("client_id", "unknown")

        # Check scopes if required
        if self.required_scopes:
            token_scopes = set()
            scope_claim = claims.get("scp") or claims.get("scope", "")
            if isinstance(scope_claim, str):
                token_scopes = set(scope_claim.split())
            elif isinstance(scope_claim, list):
                token_scopes = set(scope_claim)

            missing = self.required_scopes - token_scopes
            if missing:
                raise AuthenticationError(
                    f"Insufficient scopes. Missing: {', '.join(missing)}", 403
                )

        return AuthInfo(
            subject=str(subject),
            auth_mode="oauth2",
            scopes=list(token_scopes) if self.required_scopes else [],
            claims=claims,
        )

    async def _validate_token(self, token: str) -> Dict[str, Any]:
        """Validate a JWT token using JWKS."""
        try:
            import jwt
            from jwt import PyJWKClient
        except ImportError as exc:
            raise AuthenticationError(
                "PyJWT with cryptography is required for OAuth2. "
                "Install with: pip install 'PyJWT[crypto]>=2.8.0'"
            ) from exc

        jwks_url = self.jwks_url or f"{self.issuer_url}/.well-known/jwks.json"

        try:
            jwks_client = PyJWKClient(
                jwks_url,
                cache_jwk_set=True,
                lifespan=self.JWKS_CACHE_TTL,
            )
            signing_key = jwks_client.get_signing_key_from_jwt(token)

            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
                audience=self.audience,
                issuer=self.issuer_url,
                options={
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "require": ["exp", "sub"],
                },
            )
            return claims

        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Token has expired.", 401) from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError("Token audience mismatch.", 401) from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError("Token issuer mismatch.", 401) from exc
        except jwt.InvalidTokenError as e:
            raise AuthenticationError(f"Invalid token: {e}", 401) from e
        except Exception as e:
            logger.error("Token validation error: %s", e)
            raise AuthenticationError("Token validation failed.", 401) from e


def create_auth_provider(config: OdooConfig) -> AuthProvider:
    """Factory to create the appropriate auth provider from config."""
    if config.auth_mode == "none":
        logger.info("Auth mode: none (no client authentication)")
        if config.transport == "streamable-http":
            logger.warning(
                "Running HTTP transport without authentication. "
                "This is only appropriate for local development."
            )
        return NoAuthProvider()

    if config.auth_mode == "api_key":
        logger.info("Auth mode: API key")
        return ApiKeyAuthProvider(config.mcp_api_keys)

    if config.auth_mode == "oauth2":
        logger.info(
            "Auth mode: OAuth2 (issuer=%s, audience=%s)",
            config.oauth2_issuer_url,
            config.oauth2_audience,
        )
        return OAuth2Provider(
            issuer_url=config.oauth2_issuer_url,
            audience=config.oauth2_audience,
            jwks_url=config.oauth2_jwks_url,
            required_scopes=config.oauth2_required_scopes,
        )

    raise ValueError(f"Unknown auth mode: {config.auth_mode}")


class AuthMiddleware:
    """ASGI middleware that authenticates requests before MCP processing.

    Exempt paths (e.g., /health, /ready) bypass authentication.
    Authenticated subject is stored in the context variable for downstream use.
    """

    EXEMPT_PATHS = {"/health", "/ready"}

    def __init__(self, app, provider: AuthProvider):
        self.app = app
        self.provider = provider

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Skip auth for exempt paths
        if path in self.EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # Extract headers
        headers = {}
        for name, value in scope.get("headers", []):
            headers[name.decode("latin-1").lower()] = value.decode("latin-1")

        try:
            auth_info = await self.provider.authenticate(headers)
            token = set_current_auth_info(auth_info)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_auth_info.reset(token)
        except AuthenticationError as e:
            await self._send_error(send, e.status_code, str(e))

    async def _send_error(self, send, status_code: int, message: str):
        body = json.dumps({"error": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"www-authenticate", b"Bearer" if status_code == 401 else b""],
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )
