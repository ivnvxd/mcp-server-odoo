"""Entry point for the mcp-server-odoo package.

This module provides the command-line interface for running the
Odoo MCP server via uvx or direct execution.
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv

from .config import load_config
from .server import SERVER_VERSION, OdooMCPServer


def main(argv: Optional[list[str]] = None) -> int:
    """Main entry point for the MCP server.

    This function handles command-line arguments, loads configuration,
    and runs the MCP server with the specified transport.

    Args:
        argv: Command line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for failure)
    """

    # Create argument parser
    parser = argparse.ArgumentParser(
        description="Odoo MCP Server - Model Context Protocol server for Odoo ERP",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Environment variables:
  ODOO_URL           Odoo server URL (required)
  ODOO_API_KEY       Odoo API key (preferred authentication)
  ODOO_USER          Odoo username (fallback if no API key)
  ODOO_PASSWORD      Odoo password (required with username)
  ODOO_DB            Odoo database name (auto-detected if not set)

Optional environment variables:
  ODOO_MCP_LOG_LEVEL    Log level (DEBUG, INFO, WARNING, ERROR)
  ODOO_MCP_DEFAULT_LIMIT Default record limit (default: 10)
  ODOO_MCP_MAX_LIMIT     Maximum record limit (default: 100)
  ODOO_MCP_TRANSPORT     Transport type: stdio or streamable-http (default: stdio)
  ODOO_MCP_HOST          Server host for HTTP transports (default: localhost)
  ODOO_MCP_PORT          Server port for HTTP transports (default: 8000)

For more information, visit: https://github.com/ivnvxd/mcp-server-odoo""",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"odoo-mcp-server v{SERVER_VERSION}",
    )

    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=os.getenv("ODOO_MCP_TRANSPORT", "stdio"),
        help="Transport type to use (default: stdio)",
    )

    parser.add_argument(
        "--host",
        default=os.getenv("ODOO_MCP_HOST", "localhost"),
        help="Server host for HTTP transports (default: localhost)",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("ODOO_MCP_PORT", "8000")),
        help="Server port for HTTP transports (default: 8000)",
    )

    # Parse arguments
    args = parser.parse_args(argv)

    try:
        # Override environment variables with CLI arguments
        if args.transport:
            os.environ["ODOO_MCP_TRANSPORT"] = args.transport
        if args.host:
            os.environ["ODOO_MCP_HOST"] = args.host
        if args.port:
            os.environ["ODOO_MCP_PORT"] = str(args.port)

        # Load configuration from environment
        config = load_config()

        # Create server instance
        server = OdooMCPServer(config)

        # Run the server with the specified transport
        if config.transport == "stdio":
            asyncio.run(server.run_stdio())
        elif config.transport == "streamable-http":
            asyncio.run(server.run_http(host=config.host, port=config.port))
        else:
            raise ValueError(f"Unsupported transport: {config.transport}")

        return 0

    except KeyboardInterrupt:
        # Handle graceful shutdown on Ctrl+C
        print("\nServer stopped by user", file=sys.stderr)
        return 0

    except ValueError as e:
        # Configuration errors
        print(f"Configuration error: {e}", file=sys.stderr)
        print("\nPlease check your environment variables or .env file", file=sys.stderr)
        return 1

    except Exception as e:
        # Other errors
        logging.error(f"Server error: {e}", exc_info=True)
        print(f"Error: {e}", file=sys.stderr)
        return 1


# Entry point for module execution
if __name__ == "__main__":
    sys.exit(main())
