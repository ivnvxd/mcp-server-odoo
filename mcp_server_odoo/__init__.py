"""MCP Server implementation for Odoo.

This package provides a Model Context Protocol (MCP) server implementation
that interfaces with Odoo systems, exposing their data and functionality to
AI models in a standardized format.
"""

__version__ = "0.1.0"

from mcp_server_odoo.server import MCPOdooServer

__all__ = ["MCPOdooServer"]
