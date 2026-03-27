"""MCP Server for Odoo - Model Context Protocol server for Odoo ERP systems."""

__version__ = "0.6.0"
__author__ = "Andrey Ivanov"
__license__ = "MPL-2.0"

from .access_control import AccessControlError, AccessController, ModelPermissions
from .audit import AuditLogger
from .auth import AuthInfo, AuthProvider, create_auth_provider
from .config import OdooConfig, load_config
from .odoo_connection import OdooConnection, OdooConnectionError, create_connection
from .security import ConfirmationManager, MutationPolicy, SecurityPolicy, SecurityPolicyError
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
    "SecurityPolicy",
    "SecurityPolicyError",
    "MutationPolicy",
    "ConfirmationManager",
    "AuditLogger",
    "AuthProvider",
    "AuthInfo",
    "create_auth_provider",
    "__version__",
]
