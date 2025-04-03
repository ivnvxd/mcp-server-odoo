"""Error handling for MCP Server Odoo.

This module provides error classes and utilities for consistent
error handling throughout the MCP server.
"""

import logging
import traceback
from typing import Any, Dict

logger = logging.getLogger(__name__)


class MCPOdooError(Exception):
    """Base exception for all MCP Odoo errors."""

    def __init__(self, message: str, status_code: int = 500):
        """Initialize the error.

        Args:
            message: Error message
            status_code: HTTP-like status code
        """
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class AuthenticationError(MCPOdooError):
    """Error raised for authentication failures."""

    def __init__(self, message: str):
        """Initialize the error.

        Args:
            message: Error message
        """
        super().__init__(message, status_code=401)


class PermissionError(MCPOdooError):
    """Error raised for permission issues."""

    def __init__(self, message: str):
        """Initialize the error.

        Args:
            message: Error message
        """
        super().__init__(message, status_code=403)


class ResourceNotFoundError(MCPOdooError):
    """Error raised when a resource is not found."""

    def __init__(self, message: str):
        """Initialize the error.

        Args:
            message: Error message
        """
        super().__init__(message, status_code=404)


class ValidationError(MCPOdooError):
    """Error raised for validation failures."""

    def __init__(self, message: str):
        """Initialize the error.

        Args:
            message: Error message
        """
        super().__init__(message, status_code=400)


class ConnectionError(MCPOdooError):
    """Error raised for connection issues."""

    def __init__(self, message: str):
        """Initialize the error.

        Args:
            message: Error message
        """
        super().__init__(message, status_code=503)


def format_error_response(error: Exception) -> Dict[str, Any]:
    """Format an exception as an MCP error response.

    Args:
        error: The exception to format

    Returns:
        Dict[str, Any]: Formatted error response dictionary
    """
    # Extract error details
    if isinstance(error, MCPOdooError):
        error_message = error.message
        status_code = error.status_code
    else:
        error_message = str(error)
        status_code = 500

    # Convert technical status codes to user-friendly messages
    status_messages = {
        400: "Invalid request",
        401: "Authentication failed",
        403: "Permission denied",
        404: "Resource not found",
        500: "Server error",
        503: "Service unavailable",
    }

    status_message = status_messages.get(status_code, "Error")

    # Format the error message
    user_message = f"{status_message}: {error_message}"

    # Log the error with stack trace for server-side debugging
    logger.error(f"Error {status_code}: {error_message}")
    if status_code >= 500:
        logger.error(traceback.format_exc())

    # Return MCP error response
    return {
        "is_error": True,
        "content": [
            {
                "type": "text",
                "text": user_message,
            },
        ],
    }


def handle_exceptions(func):
    """Decorator to handle exceptions in resource handlers.

    Args:
        func: Function to wrap

    Returns:
        Function: Wrapped function
    """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return format_error_response(e)

    return wrapper
