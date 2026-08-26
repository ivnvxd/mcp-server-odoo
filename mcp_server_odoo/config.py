"""Configuration management for Odoo MCP Server.

This module handles loading and validation of environment variables
for connecting to Odoo via XML-RPC.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Literal, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class OdooConfig:
    """Configuration for Odoo connection and MCP server settings."""

    # Required fields
    url: str

    # Authentication (one method required)
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

    # Idle seconds before a streamable-http session is evicted (None = never).
    # Without it, abandoned sessions pin their transport state (streams,
    # task, server instance) in the session manager until process restart.
    session_idle_timeout: Optional[float] = None

    # YOLO mode configuration
    yolo_mode: str = "off"  # "off", "read", or "true"

    # Opt-in for call_model_method (effective only with yolo_mode == "true").
    enable_method_calls: bool = False

    # Allowed hosts for DNS rebinding protection (HTTP transport)
    allowed_hosts: list[str] = field(default_factory=list)

    # Ceiling on a single binary resources/read (bytes). The read decodes the
    # payload and the MCP layer re-encodes it to base64, so peak memory runs
    # ~2.3x the stored size — one oversized attachment can take the process
    # down. Now that populated binaries are advertised as odoo:// URIs, a
    # client will follow them, so the ceiling is enforced rather than assumed.
    max_binary_size: int = 50 * 1024 * 1024

    def __post_init__(self):
        """Validate configuration after initialization."""
        if not self.url:
            raise ValueError("ODOO_URL is required")

        # Ensure URL format
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("ODOO_URL must start with http:// or https://")

        valid_yolo_modes = {"off", "read", "true"}
        if self.yolo_mode not in valid_yolo_modes:
            raise ValueError(
                f"Invalid YOLO mode: {self.yolo_mode}. "
                f"Must be one of: {', '.join(valid_yolo_modes)}"
            )

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

        if self.default_limit <= 0:
            raise ValueError("ODOO_MCP_DEFAULT_LIMIT must be positive")

        if self.max_limit <= 0:
            raise ValueError("ODOO_MCP_MAX_LIMIT must be positive")

        if self.default_limit > self.max_limit:
            raise ValueError("ODOO_MCP_DEFAULT_LIMIT cannot exceed ODOO_MCP_MAX_LIMIT")

        valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_log_levels:
            raise ValueError(
                f"Invalid log level: {self.log_level}. "
                f"Must be one of: {', '.join(valid_log_levels)}"
            )

        valid_transports = {"stdio", "streamable-http"}
        if self.transport not in valid_transports:
            raise ValueError(
                f"Invalid transport: {self.transport}. "
                f"Must be one of: {', '.join(valid_transports)}"
            )

        if self.port <= 0 or self.port > 65535:
            raise ValueError("Port must be between 1 and 65535")

        # Validate session idle timeout
        if self.session_idle_timeout is not None and self.session_idle_timeout <= 0:
            raise ValueError("ODOO_MCP_SESSION_IDLE_TIMEOUT must be positive")

        if self.max_binary_size <= 0:
            raise ValueError("ODOO_MCP_MAX_BINARY_SIZE must be positive")

        # Without this warning, the silent non-registration is hard to debug.
        if self.enable_method_calls and self.yolo_mode != "true":
            logger.warning(
                "ODOO_MCP_ENABLE_METHOD_CALLS=true ignored: requires ODOO_YOLO=true "
                "(full YOLO mode); current yolo_mode=%r",
                self.yolo_mode,
            )

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

    def get_optional_float_env(key: str) -> Optional[float]:
        value = os.getenv(key)
        if value is None or not value.strip():
            return None
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"{key} must be a valid number") from None

    def get_bool_env(key: str, default: bool = False) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return default
        return raw.strip().lower() in {"true", "1", "yes"}

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

    # Helper function to parse allowed hosts
    def parse_allowed_hosts() -> list[str]:
        hosts = os.getenv("ODOO_MCP_ALLOWED_HOSTS", "").strip()
        if not hosts:
            return []
        return [h.strip() for h in hosts.split(",") if h.strip()]

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
        session_idle_timeout=get_optional_float_env("ODOO_MCP_SESSION_IDLE_TIMEOUT"),
        locale=os.getenv("ODOO_LOCALE", "").strip() or None,
        yolo_mode=get_yolo_mode(),
        enable_method_calls=get_bool_env("ODOO_MCP_ENABLE_METHOD_CALLS", False),
        allowed_hosts=parse_allowed_hosts(),
        max_binary_size=get_int_env("ODOO_MCP_MAX_BINARY_SIZE", 50 * 1024 * 1024),
    )

    return config


# Singleton configuration instance
_config: Optional[OdooConfig] = None


def get_config() -> OdooConfig:
    """Get the singleton configuration instance.

    Returns:
        OdooConfig: The configuration object

    Raises:
        ValueError: If configuration cannot be loaded or is invalid.
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


# Deep pagination is query-cost amplification: Postgres still walks (and
# discards) every skipped row, so a huge offset is expensive even though the
# limit is capped. Reject offsets beyond this many pages of the effective
# limit — generous for real paging, but it stops an ``offset=999999999``
# forcing an unbounded skip.
MAX_OFFSET_PAGES = 1000

# Floor for the offset cap: small-limit deep pagination (fetch-the-Nth-record
# with limit=1) must not fail shallower than a larger page size would —
# without the floor, ``limit=1, offset=1500`` would be rejected while
# ``limit=100`` reaches the same depth fine.
MIN_OFFSET_CAP = 10_000


def max_offset_for(limit: int) -> int:
    """The deepest offset accepted for a given page size.

    ``MAX_OFFSET_PAGES`` pages of the effective limit, floored at
    ``MIN_OFFSET_CAP``. Lives beside the other paging limits rather than in
    ``tools``: the read-only resource layer enforces the same bound and must
    not import from the write-capable tool layer to get it.
    """
    return max(limit * MAX_OFFSET_PAGES, MIN_OFFSET_CAP)
