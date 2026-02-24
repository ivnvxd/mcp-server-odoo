"""MCP Server for Odoo - Model Context Protocol server for Odoo ERP systems."""

__version__ = "0.4.4"
__author__ = "Andrey Ivanov"
__license__ = "MPL-2.0"

from .access_control import AccessControlError, AccessController, ModelPermissions
from .config import OdooConfig, load_config
from .odoo_connection import OdooConnection, OdooConnectionError, create_connection
from .server import OdooMCPServer

__all__ = [
    "OdooMCPServer",
    "OdooConfig",
    "load_config",
    "OdooConnection",
    "OdooConnectionError",
    "create_connection",
    "AccessController",
    "AccessControlError",
    "ModelPermissions",
    "__version__",
]
