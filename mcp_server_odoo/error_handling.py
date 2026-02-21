"""Error handling and monitoring for Odoo MCP Server.

This module provides a centralized error handling system with:
- Error categorization and classification
- User-friendly error message generation
- Structured logging and monitoring
- MCP-compliant error response formatting
"""

import logging
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Union

from mcp.types import ErrorData

from .error_sanitizer import ErrorSanitizer

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


@dataclass
class ErrorMetrics:
    """Metrics for error tracking and monitoring."""

    total_errors: int = 0
    errors_by_category: Dict[ErrorCategory, int] = field(default_factory=dict)
    errors_by_severity: Dict[ErrorSeverity, int] = field(default_factory=dict)
    last_error_time: Optional[datetime] = None
    error_rate_per_minute: float = 0.0

    def record_error(self, category: ErrorCategory, severity: ErrorSeverity):
        """Record an error occurrence."""
        self.total_errors += 1
        self.errors_by_category[category] = self.errors_by_category.get(category, 0) + 1
        self.errors_by_severity[severity] = self.errors_by_severity.get(severity, 0) + 1
        self.last_error_time = datetime.now()


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

    def to_dict(self) -> Dict[str, Any]:
        """Convert error to dictionary for logging/API responses."""
        # Sanitize message and details for external consumption
        sanitized_message = ErrorSanitizer.sanitize_message(self.message)
        sanitized_details = ErrorSanitizer.sanitize_error_details(self.details)

        return {
            "error": {
                "code": self.code,
                "message": sanitized_message,
                "category": self.category.name,
                "severity": self.severity.value,
                "details": sanitized_details,
                "context": {
                    "model": self.context.model,
                    "operation": self.context.operation,
                    "record_id": self.context.record_id,
                    "request_id": self.context.request_id,
                },
                "timestamp": self.timestamp.isoformat(),
            }
        }

    def to_mcp_error(self) -> ErrorData:
        """Convert to MCP-compliant error format."""
        # Sanitize message and details for external consumption
        sanitized_message = ErrorSanitizer.sanitize_message(self.message)
        sanitized_details = ErrorSanitizer.sanitize_error_details(self.details)

        return ErrorData(
            code=-32000,  # Application error
            message=sanitized_message,
            data={"code": self.code, "details": sanitized_details},
        )


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


class PermissionError(MCPError):
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


class ConnectionError(MCPError):
    """Connection/network errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message=message,
            category=ErrorCategory.CONNECTION,
            severity=ErrorSeverity.HIGH,
            **kwargs,
        )


class SystemError(MCPError):
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
    """Central error handler with monitoring and logging capabilities."""

    def __init__(self):
        """Initialize error handler with metrics tracking."""
        self.metrics = ErrorMetrics()
        self._error_history: List[MCPError] = []
        self._max_history_size = 1000
        self._start_time = time.time()

    def handle_error(
        self,
        error: Exception,
        context: Optional[ErrorContext] = None,
        reraise: bool = True,
    ) -> Optional[MCPError]:
        """Handle an error with logging and monitoring.

        Args:
            error: The exception to handle
            context: Optional error context
            reraise: Whether to re-raise the error after handling

        Returns:
            MCPError instance if created, None otherwise

        Raises:
            The original error if reraise=True and it's not already an MCPError
        """
        # Convert to MCPError if needed
        if isinstance(error, MCPError):
            mcp_error = error
            if context:
                mcp_error.context = context
        else:
            # Map common exceptions to MCPError types
            mcp_error = self._convert_to_mcp_error(error, context)

        # Record metrics
        self.metrics.record_error(mcp_error.category, mcp_error.severity)

        # Add to history
        self._add_to_history(mcp_error)

        # Log the error
        self._log_error(mcp_error)

        # Re-raise if requested
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

        # Map common exceptions with sanitized messages
        if isinstance(error, (ConnectionRefusedError, TimeoutError)):
            return ConnectionError(
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
            return PermissionError(
                f"Access denied: {error_message}",
                details={"category": "permission_denied"},
                context=context,
            )
        else:
            # Default to system error for unknown exceptions
            # Don't include traceback in user-facing error
            return SystemError(
                f"Unexpected error: {error_message}",
                details={"category": "internal_error"},
                context=context,
            )

    def _add_to_history(self, error: MCPError):
        """Add error to history with size limit."""
        self._error_history.append(error)
        if len(self._error_history) > self._max_history_size:
            self._error_history.pop(0)

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

    def get_metrics(self) -> Dict[str, Any]:
        """Get current error metrics for monitoring."""
        uptime = time.time() - self._start_time
        error_rate = self.metrics.total_errors / (uptime / 60) if uptime > 0 else 0

        return {
            "total_errors": self.metrics.total_errors,
            "errors_by_category": {
                cat.name: count for cat, count in self.metrics.errors_by_category.items()
            },
            "errors_by_severity": {
                sev.value: count for sev, count in self.metrics.errors_by_severity.items()
            },
            "error_rate_per_minute": round(error_rate, 2),
            "last_error_time": (
                self.metrics.last_error_time.isoformat() if self.metrics.last_error_time else None
            ),
            "uptime_seconds": int(uptime),
        }

    def get_recent_errors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent errors from history."""
        recent = self._error_history[-limit:]
        return [error.to_dict() for error in reversed(recent)]

    def clear_metrics(self):
        """Clear error metrics (useful for testing)."""
        self.metrics = ErrorMetrics()
        self._error_history.clear()

    @contextmanager
    def error_context(self, **context_kwargs):
        """Context manager for handling errors with context.

        Usage:
            with error_handler.error_context(model="res.partner", operation="search"):
                # Code that might raise exceptions
                pass
        """
        context = ErrorContext(**context_kwargs)
        try:
            yield context
        except Exception as e:
            self.handle_error(e, context=context)


# Global error handler instance
error_handler = ErrorHandler()


# Utility functions for common error scenarios
def handle_odoo_error(
    error: Exception, model: str | None = None, operation: str | None = None
) -> MCPError:
    """Handle Odoo-specific errors with appropriate categorization.

    Args:
        error: The exception from Odoo
        model: The model being accessed
        operation: The operation being performed

    Returns:
        MCPError instance with proper categorization
    """
    context = ErrorContext(model=model, operation=operation)
    error_str = str(error).lower()

    # Check for specific Odoo error patterns
    if "access denied" in error_str or "accessdenied" in error_str:
        return PermissionError(
            f"Access denied for {operation} on {model}: {error}",
            context=context,
        )
    elif "does not exist" in error_str or "not found" in error_str:
        return NotFoundError(
            f"Resource not found: {model if model else 'Unknown'}: {error}",
            context=context,
        )
    elif "invalid" in error_str or "validation" in error_str:
        return ValidationError(
            f"Validation failed for {operation}: {error}",
            context=context,
        )
    elif "connection" in error_str or "timeout" in error_str:
        return ConnectionError(
            f"Connection to Odoo failed: {error}",
            context=context,
        )
    else:
        return SystemError(
            f"Odoo error during {operation}: {error}",
            context=context,
        )


def format_user_error(error: MCPError) -> str:
    """Format error for user-friendly display.

    Args:
        error: The MCPError to format

    Returns:
        User-friendly error message
    """
    # Base message
    message = error.message

    # Add context if available
    if error.context.model:
        message = f"{message} (Model: {error.context.model})"

    # Add helpful suggestions based on error type
    suggestions = {
        ErrorCategory.AUTHENTICATION: "Please check your credentials and try again.",
        ErrorCategory.PERMISSION: "You don't have permission for this operation. Contact your administrator.",
        ErrorCategory.NOT_FOUND: "The requested resource doesn't exist or has been deleted.",
        ErrorCategory.VALIDATION: "Please check your input and try again.",
        ErrorCategory.CONNECTION: "Unable to connect to Odoo. Please check your connection settings.",
        ErrorCategory.SYSTEM: "An unexpected error occurred. Please try again later.",
        ErrorCategory.CONFIGURATION: "Server configuration error. Please contact your administrator.",
        ErrorCategory.RATE_LIMIT: "Too many requests. Please wait a moment and try again.",
    }

    suggestion = suggestions.get(error.category)
    if suggestion:
        message = f"{message}\n\n{suggestion}"

    return message
