#!/usr/bin/env python3
"""Command-line interface for MCP Server Odoo.

This module provides a command-line entry point for the MCP Server Odoo package.
It handles argument parsing, environment variable loading, and server initialization.
"""

import argparse
import logging
import os
import sys
from typing import Dict

import dotenv

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
    parser = argparse.ArgumentParser(description="MCP Server for Odoo")

    parser.add_argument(
        "--url",
        help="Odoo server URL (env: ODOO_URL)",
    )
    parser.add_argument(
        "--db",
        help="Odoo database name (env: ODOO_DB)",
    )
    parser.add_argument(
        "--token",
        help="Odoo MCP authentication token (env: ODOO_MCP_TOKEN)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set the logging level (env: ODOO_MCP_LOG_LEVEL)",
    )
    parser.add_argument(
        "--default-limit",
        type=int,
        default=20,
        help="Default record limit for search operations (env: ODOO_MCP_DEFAULT_LIMIT)",
    )
    parser.add_argument(
        "--max-limit",
        type=int,
        default=100,
        help="Maximum allowed record limit (env: ODOO_MCP_MAX_LIMIT)",
    )
    parser.add_argument(
        "--env-file",
        help="Path to .env file for configuration",
    )

    return parser.parse_args()


def get_config(args: argparse.Namespace) -> Dict[str, str]:
    """Get configuration from arguments and environment variables.

    Args:
        args: Parsed command line arguments

    Returns:
        Dict[str, str]: Configuration dictionary

    Raises:
        ValueError: If required configuration is missing
    """
    # Load environment variables from .env file if provided
    if args.env_file:
        dotenv.load_dotenv(args.env_file)
    else:
        dotenv.load_dotenv()  # Try to load from default .env location

    # Configuration mapping: arg_name -> (env_var_name, required)
    config_map = {
        "url": ("ODOO_URL", True),
        "db": ("ODOO_DB", True),
        "token": ("ODOO_MCP_TOKEN", True),
        "log_level": ("ODOO_MCP_LOG_LEVEL", False),
        "default_limit": ("ODOO_MCP_DEFAULT_LIMIT", False),
        "max_limit": ("ODOO_MCP_MAX_LIMIT", False),
    }

    config = {}
    missing_required = []

    # Process each configuration item
    for arg_name, (env_name, required) in config_map.items():
        # Get value from args or environment
        arg_value = getattr(args, arg_name)
        env_value = os.environ.get(env_name)

        # Use arg value if provided, otherwise use env value
        if arg_value is not None:
            config[arg_name] = arg_value
        elif env_value is not None:
            config[arg_name] = env_value
        elif required:
            missing_required.append(f"--{arg_name} or {env_name}")

    # Check for missing required configuration
    if missing_required:
        raise ValueError(
            f"Missing required configuration: {', '.join(missing_required)}"
        )

    return config


def main() -> None:
    """Main entry point for MCP Server Odoo."""
    try:
        args = parse_args()

        # Set logging level from args
        if args.log_level:
            logger.setLevel(getattr(logging, args.log_level))

        # Get configuration
        config = get_config(args)

        # Create and start server
        server = MCPOdooServer(
            odoo_url=config["url"],
            odoo_db=config["db"],
            odoo_token=config["token"],
            default_limit=int(config.get("default_limit", 20)),
            max_limit=int(config.get("max_limit", 100)),
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
