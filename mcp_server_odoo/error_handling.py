"""Error handling for Odoo MCP Server.

This module provides a centralized error handling system with:
- Error categorization and classification
- Typed MCPError subclasses per category
- Structured logging
"""

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """Categories of errors that can occur in the MCP server."""

    AUTHENTICATION = auto()  # Authentication failures
    PERMISSION = auto()  # Permission/access denied
    NOT_FOUND = auto()  # Resource/model/record not found
    VALIDATION = auto()  # Input validation errors
    CONNECTION = auto()  # Connection/network errors
    SYSTEM = auto()  # System/unexpected errors
    CONFIGURATION = auto()  # Configuration errors
    RATE_LIMIT = auto()  # Rate limiting errors


class ErrorSeverity(Enum):
    """Severity levels for errors."""

    LOW = "low"  # Informational, non-critical
    MEDIUM = "medium"  # User error, recoverable
    HIGH = "high"  # System error, may need intervention
    CRITICAL = "critical"  # Critical failure, immediate attention


@dataclass
class ErrorContext:
    """Context information for an error."""

    model: Optional[str] = None
    operation: Optional[str] = None
    record_id: Optional[Union[int, str]] = None
    user_id: Optional[int] = None
    request_id: Optional[str] = None
    additional_info: Dict[str, Any] = field(default_factory=dict)


class MCPError(Exception):
    """Base exception for MCP-related errors with enhanced tracking."""

    def __init__(
        self,
        message: str,
        category: ErrorCategory,
        severity: ErrorSeverity = ErrorSeverity.MEDIUM,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        context: Optional[ErrorContext] = None,
    ):
        """Initialize MCP error with tracking information.

        Args:
            message: Human-readable error message
            category: Error category for classification
            severity: Error severity level
            code: Optional error code for specific error types
            details: Additional error details
            context: Error context information
        """
        super().__init__(message)
        self.message = message
        self.category = category
        self.severity = severity
        self.code = code or self._generate_code(category)
        self.details = details or {}
        self.context = context or ErrorContext()
        self.timestamp = datetime.now()

    def _generate_code(self, category: ErrorCategory) -> str:
        """Generate error code based on category."""
        codes = {
            ErrorCategory.AUTHENTICATION: "AUTH_ERROR",
            ErrorCategory.PERMISSION: "PERMISSION_DENIED",
            ErrorCategory.NOT_FOUND: "NOT_FOUND",
            ErrorCategory.VALIDATION: "VALIDATION_ERROR",
            ErrorCategory.CONNECTION: "CONNECTION_ERROR",
            ErrorCategory.SYSTEM: "SYSTEM_ERROR",
            ErrorCategory.CONFIGURATION: "CONFIG_ERROR",
            ErrorCategory.RATE_LIMIT: "RATE_LIMIT_EXCEEDED",
        }
        return codes.get(category, "UNKNOWN_ERROR")


# Specific error classes for each category
class AuthenticationError(MCPError):
    """Authentication-related errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.AUTHENTICATION,
            severity=ErrorSeverity.HIGH,
            **kwargs,
        )


class MCPPermissionError(MCPError):
    """Permission/access denied errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.PERMISSION,
            severity=ErrorSeverity.MEDIUM,
            **kwargs,
        )


class NotFoundError(MCPError):
    """Resource not found errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.NOT_FOUND,
            severity=ErrorSeverity.LOW,
            **kwargs,
        )


class ValidationError(MCPError):
    """Input validation errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.VALIDATION,
            severity=ErrorSeverity.LOW,
            **kwargs,
        )


class MCPConnectionError(MCPError):
    """Connection/network errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.CONNECTION,
            severity=ErrorSeverity.HIGH,
            **kwargs,
        )


class MCPSystemError(MCPError):
    """System/unexpected errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.SYSTEM,
            severity=ErrorSeverity.CRITICAL,
            **kwargs,
        )


class ConfigurationError(MCPError):
    """Configuration errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.CONFIGURATION,
            severity=ErrorSeverity.HIGH,
            **kwargs,
        )


class RateLimitError(MCPError):
    """Rate limiting errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.RATE_LIMIT,
            severity=ErrorSeverity.MEDIUM,
            **kwargs,
        )


class ErrorHandler:
    """Central error handler: converts exceptions to MCPError and logs them."""

    def handle_error(
        self,
        error: Exception,
        context: Optional[ErrorContext] = None,
        reraise: bool = True,
    ) -> Optional[MCPError]:
        """Handle an error: convert it to an MCPError and log it.

        Args:
            error: The exception to handle
            context: Optional error context
            reraise: Whether to re-raise the error after handling

        Returns:
            The MCPError (converted if needed) when reraise=False

        Raises:
            MCPError: The (possibly converted) error when reraise=True
        """
        if isinstance(error, MCPError):
            mcp_error = error
            if context:
                mcp_error.context = context
        else:
            mcp_error = self._convert_to_mcp_error(error, context)

        self._log_error(mcp_error)

        if reraise:
            raise mcp_error

        return mcp_error

    def _convert_to_mcp_error(
        self, error: Exception, context: Optional[ErrorContext] = None
    ) -> MCPError:
        """Convert standard exceptions to MCPError instances."""
        error_message = str(error)
        error_type = type(error).__name__

        # Log the full traceback internally
        logger.debug(f"Full error details: {error_type}: {error_message}\n{traceback.format_exc()}")

        if isinstance(error, (ConnectionError, TimeoutError)):
            return MCPConnectionError(
                f"Connection failed: {error_message}",
                details={"category": "connection_error"},
                context=context,
            )
        elif isinstance(error, (ValueError, TypeError)):
            return ValidationError(
                f"Invalid input: {error_message}",
                details={"category": "validation_error"},
                context=context,
            )
        elif isinstance(error, KeyError):
            return NotFoundError(
                f"Resource not found: {error_message}",
                details={"category": "not_found"},
                context=context,
            )
        elif isinstance(error, PermissionError):
            # Builtin PermissionError (OSError subclass) — custom
            # MCPPermissionError instances never reach this function
            # (MCPError is returned early by handle_error)
            return MCPPermissionError(
                f"Access denied: {error_message}",
                details={"category": "permission_denied"},
                context=context,
            )
        else:
            return MCPSystemError(
                f"Unexpected error: {error_message}",
                details={"category": "internal_error"},
                context=context,
            )

    def _log_error(self, error: MCPError):
        """Log error with appropriate level."""
        log_levels = {
            ErrorSeverity.LOW: logging.INFO,
            ErrorSeverity.MEDIUM: logging.WARNING,
            ErrorSeverity.HIGH: logging.ERROR,
            ErrorSeverity.CRITICAL: logging.CRITICAL,
        }

        level = log_levels.get(error.severity, logging.ERROR)
        logger.log(
            level,
            f"[{error.category.name}] {error.message}",
            extra={
                "error_code": error.code,
                "error_details": error.details,
                "error_context": {
                    "model": error.context.model,
                    "operation": error.context.operation,
                    "record_id": error.context.record_id,
                    "request_id": error.context.request_id,
                },
            },
        )


# Global error handler instance
error_handler = ErrorHandler()
