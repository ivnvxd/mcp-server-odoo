#!/usr/bin/env python3
"""Command-line interface for MCP Server Odoo.

This module provides a command-line entry point for the MCP Server Odoo package.
It handles argument parsing, environment variable loading, and server initialization.
"""

import argparse
import logging
import os
import re
import sys
from typing import Dict, Any

import dotenv
import requests

from mcp_server_odoo.server import MCPOdooServer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mcp_server_odoo")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        argparse.Namespace: Parsed command line arguments
    """
    parser = argparse.ArgumentParser(description="Run the Odoo MCP server")

    # Connection parameters
    parser.add_argument("--url", help="Odoo URL (env: ODOO_URL)")
    parser.add_argument(
        "--db",
        help="Odoo database name (env: ODOO_DB). If not specified, will try to auto-detect the default database.",
    )
    parser.add_argument("--token", help="Odoo API token (env: ODOO_MCP_TOKEN)")

    # Configuration parameters
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log level (env: ODOO_LOG_LEVEL, default: INFO)",
    )
    parser.add_argument(
        "--default-limit",
        type=int,
        help="Default record limit (env: ODOO_MCP_DEFAULT_LIMIT, default: 50)",
    )
    parser.add_argument(
        "--max-limit",
        type=int,
        help="Maximum allowed record limit (env: ODOO_MCP_MAX_LIMIT, default: 100)",
    )

    # Dotenv support
    parser.add_argument(
        "--env-file", help="Path to .env file for environment variables"
    )

    return parser.parse_args()


def detect_default_database(url: str) -> str:
    """Attempt to detect the default database for the current Odoo instance.

    This function tries multiple methods to discover the default database:
    1. Querying the database list endpoint (/web/database/list)
    2. Parsing the login page for database input field

    Args:
        url: The Odoo instance URL

    Returns:
        str: The detected database name or empty string if none found

    Note:
        This function handles its own exceptions to avoid breaking the
        application flow, returning an empty string on failure.
    """
    # Clean URL to ensure consistent formatting
    clean_url = url.rstrip("/")

    try:
        # First try the database list endpoint
        db_list_url = f"{clean_url}/web/database/list"
        logger.debug(f"Attempting to detect database from list endpoint: {db_list_url}")

        response = requests.get(db_list_url, timeout=5)

        if response.status_code == 200:
            try:
                data = response.json()
                if isinstance(data, dict) and "result" in data:
                    databases = data["result"]
                    if databases and len(databases) > 0:
                        db_name = databases[0]
                        logger.info(f"Auto-detected database: {db_name}")
                        return db_name
            except ValueError:
                logger.debug("Failed to parse JSON from database list endpoint")
    except requests.RequestException as e:
        logger.debug(f"Error accessing database list endpoint: {e}")
    except Exception as e:
        logger.debug(f"Unexpected error detecting database from list endpoint: {e}")

    try:
        # Fallback: try to get it from the login page
        login_url = f"{clean_url}/web/login"
        logger.debug(f"Attempting to detect database from login page: {login_url}")

        response = requests.get(login_url, timeout=5)

        if response.status_code == 200:
            # Look for database input field with a value
            match = re.search(
                r'<input[^>]*name="db"[^>]*value="([^"]*)"', response.text
            )
            if match and match.group(1):
                db_name = match.group(1)
                logger.info(f"Auto-detected database from login page: {db_name}")
                return db_name
    except requests.RequestException as e:
        logger.debug(f"Error accessing login page: {e}")
    except Exception as e:
        logger.debug(f"Unexpected error detecting database from login page: {e}")

    logger.warning("Failed to auto-detect database")
    return ""


def get_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Get configuration from args and environment variables.

    This function builds a configuration dictionary by combining command-line
    arguments and environment variables, with precedence given to command-line args.
    It also handles auto-detection of the database when not explicitly specified.

    Args:
        args: Parsed command line arguments

    Returns:
        dict: Configuration dictionary with proper types

    Raises:
        ValueError: If required configuration is missing or invalid
    """
    # Define configuration mapping with environment variables
    # Format: {config_key: (env_var, required, fallback, converter)}
    config_map = {
        "url": ("ODOO_URL", True, None, str),
        "db": ("ODOO_DB", False, None, str),
        "token": ("ODOO_MCP_TOKEN", True, None, str),
        "log_level": ("ODOO_LOG_LEVEL", False, "INFO", str),
        "default_limit": ("ODOO_MCP_DEFAULT_LIMIT", False, 50, int),
        "max_limit": ("ODOO_MCP_MAX_LIMIT", False, 100, int),
    }

    # Load environment variables from .env file if specified
    if args.env_file:
        dotenv.load_dotenv(args.env_file)

    # Build configuration
    config = {}
    missing = []

    for key, (env_var, required, fallback, converter) in config_map.items():
        # Get value from args or environment with fallback
        arg_value = getattr(args, key, None)
        env_value = os.environ.get(env_var)

        if arg_value is not None:
            try:
                config[key] = converter(arg_value)
            except (ValueError, TypeError) as e:
                logger.error(f"Invalid value for {key}: {arg_value} - {e}")
                if required:
                    missing.append(env_var)
        elif env_value is not None:
            try:
                config[key] = converter(env_value)
            except (ValueError, TypeError) as e:
                logger.error(f"Invalid value for {env_var}: {env_value} - {e}")
                if required:
                    missing.append(env_var)
        elif required:
            missing.append(env_var)
        else:
            config[key] = fallback

    # Check for missing required configuration
    if missing:
        raise ValueError(f"Missing required configuration: {', '.join(missing)}")

    # Auto-detect database if not specified
    if config.get("db") is None:
        # We need a URL to detect the database
        if "url" not in config:
            missing.append("ODOO_DB or ODOO_URL")
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")

        logger.info("Database not specified, attempting auto-detection...")
        detected_db = detect_default_database(config["url"])
        if not detected_db:
            raise ValueError("Missing required configuration: ODOO_DB")

        logger.info(f"Using auto-detected database: {detected_db}")
        config["db"] = detected_db

    return config


def main() -> None:
    """Main entry point for MCP Server Odoo.

    This function handles setting up the server with user configuration
    and gracefully managing errors during startup.
    """
    try:
        args = parse_args()

        # Set logging level from args
        if args.log_level:
            log_level = getattr(logging, args.log_level)
            logger.setLevel(log_level)
            logger.debug(f"Log level set to {args.log_level}")

        # Get configuration
        config = get_config(args)

        logger.info(
            f"Starting Odoo MCP server with URL: {config['url']}, DB: {config['db']}"
        )

        # Create and start server
        server = MCPOdooServer(
            odoo_url=config["url"],
            odoo_db=config["db"],
            odoo_token=config["token"],
            default_limit=config.get("default_limit", 50),
            max_limit=config.get("max_limit", 100),
        )

        # Start the server
        server.start()

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Error starting server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
