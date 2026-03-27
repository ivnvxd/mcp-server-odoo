"""Configuration management for Odoo MCP Server.

This module handles loading and validation of environment variables
for connecting to Odoo via XML-RPC.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

from dotenv import load_dotenv


@dataclass
class OdooConfig:
    """Configuration for Odoo connection and MCP server settings."""

    # Required fields
    url: str

    # Authentication to Odoo (one method required)
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    # Optional fields with defaults
    database: Optional[str] = None
    log_level: str = "INFO"
    default_limit: int = 10
    max_limit: int = 100
    max_smart_fields: int = 15
    locale: Optional[str] = None

    # MCP transport configuration
    transport: Literal["stdio", "streamable-http"] = "stdio"
    host: str = "localhost"
    port: int = 8000

    # YOLO mode configuration
    yolo_mode: str = "off"  # "off", "read", or "true"

    # --- MCP client authentication ---
    auth_mode: str = "none"  # "none" | "api_key" | "oauth2"
    mcp_api_keys: Optional[List[str]] = None
    oauth2_issuer_url: Optional[str] = None
    oauth2_audience: Optional[str] = None
    oauth2_jwks_url: Optional[str] = None
    oauth2_client_id: Optional[str] = None
    oauth2_client_secret: Optional[str] = None
    oauth2_required_scopes: Optional[List[str]] = None

    # --- Safety guardrails ---
    allowed_models: Optional[List[str]] = None
    allowed_read_operations: Optional[List[str]] = None
    allowed_write_operations: Optional[List[str]] = None
    model_operation_map: Optional[Dict[str, List[str]]] = None
    field_allowlists: Dict[str, List[str]] = field(default_factory=dict)
    field_denylists: Dict[str, List[str]] = field(default_factory=dict)
    max_records_per_query: int = 100
    max_batch_size: int = 50
    enable_mutations: bool = False
    enable_deletes: bool = False
    require_confirmation_for_mutations: bool = True

    # --- CORS ---
    cors_origins: Optional[List[str]] = None
    cors_allow_credentials: bool = False

    # --- Audit ---
    audit_log_enabled: bool = True

    # --- Admin mode (dangerous, disabled by default) ---
    admin_mode: bool = False

    def __post_init__(self):
        """Validate configuration after initialization."""
        # Validate URL
        if not self.url:
            raise ValueError("ODOO_URL is required")

        # Ensure URL format
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("ODOO_URL must start with http:// or https://")

        # Validate YOLO mode
        valid_yolo_modes = {"off", "read", "true"}
        if self.yolo_mode not in valid_yolo_modes:
            raise ValueError(
                f"Invalid YOLO mode: {self.yolo_mode}. "
                f"Must be one of: {', '.join(valid_yolo_modes)}"
            )

        # Validate authentication to Odoo (relaxed for YOLO mode)
        has_api_key = bool(self.api_key)
        has_credentials = bool(self.username and self.password)

        # In YOLO mode, we might need username even with API key for standard auth
        if self.is_yolo_enabled:
            if not has_credentials and not (has_api_key and self.username):
                raise ValueError("YOLO mode requires either username/password or username/API key")
        else:
            if not has_api_key and not has_credentials:
                raise ValueError(
                    "Authentication required: provide either ODOO_API_KEY or "
                    "both ODOO_USER and ODOO_PASSWORD"
                )

        # Validate numeric fields
        if self.default_limit <= 0:
            raise ValueError("ODOO_MCP_DEFAULT_LIMIT must be positive")

        if self.max_limit <= 0:
            raise ValueError("ODOO_MCP_MAX_LIMIT must be positive")

        if self.default_limit > self.max_limit:
            raise ValueError("ODOO_MCP_DEFAULT_LIMIT cannot exceed ODOO_MCP_MAX_LIMIT")

        # Validate log level
        valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_log_levels:
            raise ValueError(
                f"Invalid log level: {self.log_level}. "
                f"Must be one of: {', '.join(valid_log_levels)}"
            )

        # Validate transport
        valid_transports = {"stdio", "streamable-http"}
        if self.transport not in valid_transports:
            raise ValueError(
                f"Invalid transport: {self.transport}. "
                f"Must be one of: {', '.join(valid_transports)}"
            )

        # Validate port
        if self.port <= 0 or self.port > 65535:
            raise ValueError("Port must be between 1 and 65535")

        # Validate MCP client auth mode
        valid_auth_modes = {"none", "api_key", "oauth2"}
        if self.auth_mode not in valid_auth_modes:
            raise ValueError(
                f"Invalid auth mode: {self.auth_mode}. "
                f"Must be one of: {', '.join(valid_auth_modes)}"
            )

        if self.auth_mode == "api_key":
            if not self.mcp_api_keys:
                raise ValueError(
                    "ODOO_MCP_API_KEYS is required when auth_mode is 'api_key'. "
                    "Provide one or more comma-separated API keys."
                )

        if self.auth_mode == "oauth2":
            if not self.oauth2_issuer_url:
                raise ValueError(
                    "ODOO_MCP_OAUTH2_ISSUER_URL is required when auth_mode is 'oauth2'."
                )
            if not self.oauth2_audience:
                raise ValueError("ODOO_MCP_OAUTH2_AUDIENCE is required when auth_mode is 'oauth2'.")

        # Fail-safe: require allowed_models when auth is enabled for remote access
        if self.auth_mode != "none" and not self.allowed_models and not self.admin_mode:
            raise ValueError(
                "ODOO_ALLOWED_MODELS is required when authentication is enabled (fail-safe). "
                "Provide a comma-separated list of allowed Odoo model names, "
                "or set ODOO_MCP_ADMIN_MODE=true to bypass (dangerous)."
            )

        # Validate mutation/delete consistency
        if self.enable_deletes and not self.enable_mutations:
            raise ValueError("ODOO_ENABLE_DELETES=true requires ODOO_ENABLE_MUTATIONS=true.")

        # Validate max_records_per_query
        if self.max_records_per_query <= 0:
            raise ValueError("ODOO_MAX_RECORDS_PER_QUERY must be positive")

        if self.max_batch_size <= 0:
            raise ValueError("ODOO_MAX_BATCH_SIZE must be positive")

    @property
    def uses_api_key(self) -> bool:
        """Check if configuration uses API key authentication."""
        return bool(self.api_key)

    @property
    def uses_credentials(self) -> bool:
        """Check if configuration uses username/password authentication."""
        return bool(self.username and self.password)

    @property
    def is_yolo_enabled(self) -> bool:
        """Check if any YOLO mode is active."""
        return self.yolo_mode != "off"

    @property
    def is_write_allowed(self) -> bool:
        """Check if write operations are allowed in current mode."""
        return self.yolo_mode == "true"

    def get_endpoint_paths(self) -> Dict[str, str]:
        """Get appropriate endpoint paths based on mode.

        The DB endpoint always uses the server-wide ``/xmlrpc/db`` path
        so that database listing works even when multiple databases exist
        (MCP addon routes require a DB context that isn't available yet).

        Returns:
            Dict[str, str]: Mapping of endpoint names to paths
        """
        if self.is_yolo_enabled:
            # Use standard Odoo endpoints in YOLO mode
            return {"db": "/xmlrpc/db", "common": "/xmlrpc/2/common", "object": "/xmlrpc/2/object"}
        else:
            # DB endpoint is always server-wide; common/object use MCP routes
            return {
                "db": "/xmlrpc/db",
                "common": "/mcp/xmlrpc/common",
                "object": "/mcp/xmlrpc/object",
            }

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "OdooConfig":
        """Create configuration from environment variables.

        Args:
            env_file: Optional path to .env file

        Returns:
            OdooConfig: Validated configuration object
        """
        return load_config(env_file)


def load_config(env_file: Optional[Path] = None) -> OdooConfig:
    """Load configuration from environment variables and .env file.

    Args:
        env_file: Optional path to .env file. If not provided,
                 looks for .env in current directory.

    Returns:
        OdooConfig: Validated configuration object

    Raises:
        ValueError: If required configuration is missing or invalid
    """
    # Check if we have a .env file or environment variables
    if env_file:
        if not env_file.exists():
            raise ValueError(
                f"Configuration file not found: {env_file}\n"
                "Please create a .env file based on .env.example"
            )
        load_dotenv(env_file)
    else:
        # Try to load .env from current directory
        default_env = Path(".env")
        env_loaded = False

        if default_env.exists():
            load_dotenv(default_env)
            env_loaded = True

        # If no .env file found and no ODOO_URL in environment, raise error
        if not env_loaded and not os.getenv("ODOO_URL"):
            raise ValueError(
                "No .env file found and ODOO_URL not set in environment.\n"
                "Please create a .env file based on .env.example or set environment variables."
            )

    # Helper function to get int with default
    def get_int_env(key: str, default: int) -> int:
        value = os.getenv(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{key} must be a valid integer") from None

    # Helper function to parse YOLO mode
    def get_yolo_mode() -> str:
        yolo_env = os.getenv("ODOO_YOLO", "off").strip().lower()
        # Map various inputs to valid modes
        if yolo_env in ["", "false", "0", "off", "no"]:
            return "off"
        elif yolo_env in ["read", "readonly", "read-only"]:
            return "read"
        elif yolo_env in ["true", "1", "yes", "full"]:
            return "true"
        else:
            # Invalid value - will be caught by validation
            return yolo_env

    # Helper to parse comma-separated lists
    def get_list_env(key: str, default: Optional[str] = None) -> Optional[List[str]]:
        value = os.getenv(key, default or "").strip()
        if not value:
            return None
        return [item.strip() for item in value.split(",") if item.strip()]

    # Helper to parse bool
    def get_bool_env(key: str, default: bool) -> bool:
        value = os.getenv(key, "").strip().lower()
        if not value:
            return default
        return value in ("true", "1", "yes")

    # Helper to parse JSON dict
    def get_json_dict_env(key: str) -> Optional[Dict[str, List[str]]]:
        value = os.getenv(key, "").strip()
        if not value:
            return None
        try:
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError(f"{key} must be a JSON object")
            return parsed
        except json.JSONDecodeError:
            raise ValueError(f"{key} must be valid JSON") from None

    # Scan for field allowlists/denylists: ODOO_FIELD_ALLOWLIST_RES_PARTNER -> res.partner
    def scan_field_lists(prefix: str) -> Dict[str, List[str]]:
        result = {}
        for key, value in os.environ.items():
            if key.startswith(prefix) and value.strip():
                model_part = key[len(prefix) :].lower().replace("_", ".")
                result[model_part] = [f.strip() for f in value.split(",") if f.strip()]
        return result

    # Create configuration
    config = OdooConfig(
        url=os.getenv("ODOO_URL", "").strip(),
        api_key=os.getenv("ODOO_API_KEY", "").strip() or None,
        username=os.getenv("ODOO_USER", "").strip() or None,
        password=os.getenv("ODOO_PASSWORD", "").strip() or None,
        database=os.getenv("ODOO_DB", "").strip() or None,
        log_level=os.getenv("ODOO_MCP_LOG_LEVEL", "INFO").strip(),
        default_limit=get_int_env("ODOO_MCP_DEFAULT_LIMIT", 10),
        max_limit=get_int_env("ODOO_MCP_MAX_LIMIT", 100),
        max_smart_fields=get_int_env("ODOO_MCP_MAX_SMART_FIELDS", 15),
        transport=os.getenv("ODOO_MCP_TRANSPORT", "stdio").strip(),
        host=os.getenv("ODOO_MCP_HOST", "localhost").strip(),
        port=get_int_env("ODOO_MCP_PORT", 8000),
        locale=os.getenv("ODOO_LOCALE", "").strip() or None,
        yolo_mode=get_yolo_mode(),
        # MCP client auth
        auth_mode=os.getenv("ODOO_MCP_AUTH_MODE", "none").strip().lower(),
        mcp_api_keys=get_list_env("ODOO_MCP_API_KEYS"),
        oauth2_issuer_url=os.getenv("ODOO_MCP_OAUTH2_ISSUER_URL", "").strip() or None,
        oauth2_audience=os.getenv("ODOO_MCP_OAUTH2_AUDIENCE", "").strip() or None,
        oauth2_jwks_url=os.getenv("ODOO_MCP_OAUTH2_JWKS_URL", "").strip() or None,
        oauth2_client_id=os.getenv("ODOO_MCP_OAUTH2_CLIENT_ID", "").strip() or None,
        oauth2_client_secret=os.getenv("ODOO_MCP_OAUTH2_CLIENT_SECRET", "").strip() or None,
        oauth2_required_scopes=get_list_env("ODOO_MCP_OAUTH2_SCOPES"),
        # Safety guardrails
        allowed_models=get_list_env("ODOO_ALLOWED_MODELS"),
        allowed_read_operations=get_list_env(
            "ODOO_ALLOWED_READ_OPERATIONS",
            "search_read,read,fields_get,search_count",
        ),
        allowed_write_operations=get_list_env(
            "ODOO_ALLOWED_WRITE_OPERATIONS",
            "create,write",
        ),
        model_operation_map=get_json_dict_env("ODOO_MODEL_OPERATION_MAP"),
        field_allowlists=scan_field_lists("ODOO_FIELD_ALLOWLIST_"),
        field_denylists=scan_field_lists("ODOO_FIELD_DENYLIST_"),
        max_records_per_query=get_int_env("ODOO_MAX_RECORDS_PER_QUERY", 100),
        max_batch_size=get_int_env("ODOO_MAX_BATCH_SIZE", 50),
        enable_mutations=get_bool_env("ODOO_ENABLE_MUTATIONS", False),
        enable_deletes=get_bool_env("ODOO_ENABLE_DELETES", False),
        require_confirmation_for_mutations=get_bool_env(
            "ODOO_REQUIRE_CONFIRMATION_FOR_MUTATIONS", True
        ),
        # CORS
        cors_origins=get_list_env("ODOO_MCP_CORS_ORIGINS"),
        cors_allow_credentials=get_bool_env("ODOO_MCP_CORS_CREDENTIALS", False),
        # Audit
        audit_log_enabled=get_bool_env("ODOO_MCP_AUDIT_LOG", True),
        # Admin mode
        admin_mode=get_bool_env("ODOO_MCP_ADMIN_MODE", False),
    )

    return config


# Singleton configuration instance
_config: Optional[OdooConfig] = None


def get_config() -> OdooConfig:
    """Get the singleton configuration instance.

    Returns:
        OdooConfig: The configuration object

    Raises:
        ValueError: If configuration is not yet loaded
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: OdooConfig) -> None:
    """Set the singleton configuration instance.

    This is primarily useful for testing.

    Args:
        config: The configuration object to set
    """
    global _config
    _config = config


def reset_config() -> None:
    """Reset the singleton configuration instance.

    This is primarily useful for testing.
    """
    global _config
    _config = None
