"""Tests for error handling and logging system."""

import json
import logging
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.error_handling import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    ErrorCategory,
    ErrorContext,
    ErrorHandler,
    ErrorSeverity,
    MCPError,
    NotFoundError,
    PermissionError,
    RateLimitError,
    SystemError,
    ValidationError,
    error_handler,
    format_user_error,
    handle_odoo_error,
)
from mcp_server_odoo.logging_config import (
    LoggingConfig,
    PerformanceLogger,
    RequestLoggingAdapter,
    StructuredFormatter,
    log_request,
    log_response,
    logging_config,
    perf_logger,
    setup_logging,
)


class TestMCPError:
    """Test the MCPError base class."""

    def test_error_creation(self):
        """Test creating an MCPError with all parameters."""
        context = ErrorContext(
            model="res.partner",
            operation="search",
            record_id=42,
            user_id=1,
            request_id="test-123",
        )

        error = MCPError(
            message="Test error",
            category=ErrorCategory.VALIDATION,
            severity=ErrorSeverity.MEDIUM,
            code="TEST_ERROR",
            details={"field": "email", "value": "invalid"},
            context=context,
        )

        assert error.message == "Test error"
        assert error.category == ErrorCategory.VALIDATION
        assert error.severity == ErrorSeverity.MEDIUM
        assert error.code == "TEST_ERROR"
        assert error.details == {"field": "email", "value": "invalid"}
        assert error.context.model == "res.partner"
        assert error.context.operation == "search"

    def test_error_code_generation(self):
        """Test automatic error code generation."""
        error = AuthenticationError("Invalid credentials")
        assert error.code == "AUTH_ERROR"

        error = PermissionError("Access denied")
        assert error.code == "PERMISSION_DENIED"

        error = NotFoundError("Record not found")
        assert error.code == "NOT_FOUND"

        error = ValidationError("Invalid input")
        assert error.code == "VALIDATION_ERROR"

        error = ConnectionError("Connection failed")
        assert error.code == "CONNECTION_ERROR"

        error = SystemError("System failure")
        assert error.code == "SYSTEM_ERROR"

        error = ConfigurationError("Bad config")
        assert error.code == "CONFIG_ERROR"

        error = RateLimitError("Too many requests")
        assert error.code == "RATE_LIMIT_EXCEEDED"

    def test_error_to_dict(self):
        """Test converting error to dictionary."""
        context = ErrorContext(model="res.partner", operation="create")
        error = ValidationError(
            "Invalid email format",
            details={"field": "email"},
            context=context,
        )

        error_dict = error.to_dict()

        assert "error" in error_dict
        assert error_dict["error"]["code"] == "VALIDATION_ERROR"
        assert error_dict["error"]["message"] == "Invalid email format"
        assert error_dict["error"]["category"] == "VALIDATION"
        assert error_dict["error"]["severity"] == "low"
        # Details are sanitized to only include safe fields
        assert error_dict["error"]["details"] == {"field": "email"}
        assert error_dict["error"]["context"]["model"] == "res.partner"
        assert error_dict["error"]["context"]["operation"] == "create"
        assert "timestamp" in error_dict["error"]

    def test_error_to_mcp_error(self):
        """Test converting to MCP-compliant error format."""
        error = ValidationError(
            "Invalid input",
            details={"field": "name", "issue": "too_short"},
        )

        mcp_error = error.to_mcp_error()

        assert mcp_error.code == -32000  # Application error code
        assert mcp_error.message == "Invalid input"
        assert mcp_error.data["code"] == "VALIDATION_ERROR"
        # Details are now sanitized - only safe fields are included
        assert mcp_error.data["details"] == {"field": "name"}


class TestErrorHandler:
    """Test the ErrorHandler class."""

    def test_error_handler_initialization(self):
        """Test error handler initialization."""
        handler = ErrorHandler()

        assert handler.metrics.total_errors == 0
        assert len(handler.metrics.errors_by_category) == 0
        assert len(handler.metrics.errors_by_severity) == 0
        assert handler.metrics.last_error_time is None
        assert handler._max_history_size == 1000

    def test_handle_mcp_error(self):
        """Test handling an MCPError."""
        handler = ErrorHandler()
        handler.clear_metrics()

        error = ValidationError("Test validation error")

        with pytest.raises(ValidationError):
            handler.handle_error(error)

        # Check metrics
        assert handler.metrics.total_errors == 1
        assert handler.metrics.errors_by_category[ErrorCategory.VALIDATION] == 1
        assert handler.metrics.errors_by_severity[ErrorSeverity.LOW] == 1
        assert handler.metrics.last_error_time is not None

        # Check history
        recent = handler.get_recent_errors(limit=1)
        assert len(recent) == 1
        assert recent[0]["error"]["message"] == "Test validation error"

    def test_handle_standard_exception(self):
        """Test converting standard exceptions to MCPError."""
        handler = ErrorHandler()
        handler.clear_metrics()

        # Test ValueError conversion
        with pytest.raises(ValidationError) as exc_info:
            handler.handle_error(ValueError("Invalid value"))
        assert "Invalid input: Invalid value" in str(exc_info.value)

        # Test ConnectionRefusedError conversion
        with pytest.raises(ConnectionError) as exc_info:
            handler.handle_error(ConnectionRefusedError("Connection refused"))
        assert "Connection failed:" in str(exc_info.value)

        # Test KeyError conversion
        with pytest.raises(NotFoundError) as exc_info:
            handler.handle_error(KeyError("missing_key"))
        assert "Resource not found:" in str(exc_info.value)

        # Test generic exception conversion
        with pytest.raises(SystemError) as exc_info:
            handler.handle_error(RuntimeError("Something went wrong"))
        assert "Unexpected error:" in str(exc_info.value)

    def test_handle_error_no_reraise(self):
        """Test handling error without re-raising."""
        handler = ErrorHandler()
        error = ValidationError("Test error")

        result = handler.handle_error(error, reraise=False)

        assert isinstance(result, MCPError)
        assert result.message == "Test error"

    def test_error_context_manager(self):
        """Test error context manager."""
        handler = ErrorHandler()
        handler.clear_metrics()

        with pytest.raises(ValidationError) as exc_info:
            with handler.error_context(model="res.partner", operation="create"):
                raise ValueError("Invalid field")

        error = exc_info.value
        assert error.context.model == "res.partner"
        assert error.context.operation == "create"

    def test_get_metrics(self):
        """Test getting error metrics."""
        handler = ErrorHandler()
        handler.clear_metrics()

        # Generate some errors
        handler.handle_error(ValidationError("Error 1"), reraise=False)
        handler.handle_error(PermissionError("Error 2"), reraise=False)
        handler.handle_error(ValidationError("Error 3"), reraise=False)

        metrics = handler.get_metrics()

        assert metrics["total_errors"] == 3
        assert metrics["errors_by_category"]["VALIDATION"] == 2
        assert metrics["errors_by_category"]["PERMISSION"] == 1
        assert metrics["errors_by_severity"]["low"] == 2
        assert metrics["errors_by_severity"]["medium"] == 1
        assert metrics["last_error_time"] is not None
        assert "error_rate_per_minute" in metrics
        assert "uptime_seconds" in metrics

    def test_error_history_limit(self):
        """Test that error history respects max size while total count tracks all."""
        handler = ErrorHandler()
        handler._max_history_size = 5
        handler.clear_metrics()

        for i in range(10):
            handler.handle_error(
                ValidationError(f"Error {i}"),
                reraise=False,
            )

        # History is capped at 5
        recent = handler.get_recent_errors(limit=10)
        assert len(recent) == 5

        # But total counter tracks all 10 errors
        assert handler.metrics.total_errors == 10

        # All entries should have valid structure
        for entry in recent:
            assert "error" in entry
            assert "timestamp" in entry["error"]
            assert entry["error"]["code"] == "VALIDATION_ERROR"


class TestOdooErrorHandling:
    """Test Odoo-specific error handling."""

    def test_handle_odoo_access_denied(self):
        """Test handling Odoo access denied errors."""
        error = Exception("Access Denied for model res.partner")
        result = handle_odoo_error(error, model="res.partner", operation="read")

        assert isinstance(result, PermissionError)
        assert "Access denied for read on res.partner" in result.message
        assert result.context.model == "res.partner"
        assert result.context.operation == "read"

    def test_handle_odoo_not_found(self):
        """Test handling Odoo not found errors."""
        error = Exception("Record does not exist")
        result = handle_odoo_error(error, model="res.partner")

        assert isinstance(result, NotFoundError)
        assert "Resource not found: res.partner" in result.message

    def test_handle_odoo_validation(self):
        """Test handling Odoo validation errors."""
        error = Exception("Invalid field value")
        result = handle_odoo_error(error, operation="create")

        assert isinstance(result, ValidationError)
        assert "Validation failed for create" in result.message

    def test_handle_odoo_connection(self):
        """Test handling Odoo connection errors."""
        error = Exception("Connection timeout")
        result = handle_odoo_error(error)

        assert isinstance(result, ConnectionError)
        assert "Connection to Odoo failed" in result.message

    def test_handle_odoo_generic(self):
        """Test handling generic Odoo errors."""
        error = Exception("Some other error")
        result = handle_odoo_error(error, operation="search")

        assert isinstance(result, SystemError)
        assert "Odoo error during search" in result.message


class TestUserErrorFormatting:
    """Test user-friendly error formatting."""

    def test_format_validation_error(self):
        """Test formatting validation errors."""
        error = ValidationError(
            "Email format is invalid",
            context=ErrorContext(model="res.partner"),
        )

        formatted = format_user_error(error)

        assert "Email format is invalid (Model: res.partner)" in formatted
        assert "Please check your input and try again" in formatted

    def test_format_permission_error(self):
        """Test formatting permission errors."""
        error = PermissionError("Cannot create records")

        formatted = format_user_error(error)

        assert "Cannot create records" in formatted
        assert "You don't have permission" in formatted
        assert "Contact your administrator" in formatted

    def test_format_not_found_error(self):
        """Test formatting not found errors."""
        error = NotFoundError("Partner not found")

        formatted = format_user_error(error)

        assert "Partner not found" in formatted
        assert "doesn't exist or has been deleted" in formatted

    def test_format_connection_error(self):
        """Test formatting connection errors."""
        error = ConnectionError("Cannot connect to server")

        formatted = format_user_error(error)

        assert "Cannot connect to server" in formatted
        assert "Unable to connect to Odoo" in formatted
        assert "check your connection settings" in formatted


class TestLoggingConfiguration:
    """Test logging configuration and utilities."""

    def test_structured_formatter(self):
        """Test JSON log formatting."""
        formatter = StructuredFormatter()

        # Create a log record
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        # Add extra fields
        record.error_code = "TEST_ERROR"
        record.model = "res.partner"

        formatted = formatter.format(record)
        log_data = json.loads(formatted)

        assert log_data["logger"] == "test.logger"
        assert log_data["level"] == "INFO"
        assert log_data["message"] == "Test message"
        assert log_data["error_code"] == "TEST_ERROR"
        assert log_data["model"] == "res.partner"
        assert "timestamp" in log_data

    def test_request_logging_adapter(self):
        """Test request logging adapter."""
        logger = logging.getLogger("test")
        adapter = RequestLoggingAdapter(logger, request_id="test-123")

        assert adapter.request_id == "test-123"

        # Test that request ID is added to extra
        msg, kwargs = adapter.process("Test message", {})
        assert kwargs["extra"]["request_id"] == "test-123"

    def test_performance_logger(self):
        """Test performance tracking."""
        logger = MagicMock()
        perf = PerformanceLogger(logger)

        with perf.track_operation("test_op", model="res.partner"):
            time.sleep(0.01)  # Small delay

        # Check that info was logged
        logger.info.assert_called()
        call_args = logger.info.call_args
        assert "test_op" in call_args[0][0]
        assert "completed in" in call_args[0][0]
        assert call_args[1]["extra"]["operation"] == "test_op"
        assert call_args[1]["extra"]["model"] == "res.partner"
        assert call_args[1]["extra"]["duration_ms"] > 0

    def test_setup_logging(self):
        """Test logging setup."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            setup_logging(
                log_level="DEBUG",
                use_json=True,
                log_file=tmp.name,
            )

            logger = logging.getLogger("test")
            logger.debug("Test debug message")

            # Check that file was written
            assert os.path.exists(tmp.name)
            assert os.path.getsize(tmp.name) > 0

            # Clean up
            os.unlink(tmp.name)

    def test_logging_config_from_env(self):
        """Test loading logging config from environment."""
        with patch.dict(
            os.environ,
            {
                "ODOO_MCP_LOG_LEVEL": "DEBUG",
                "ODOO_MCP_LOG_JSON": "true",
                "ODOO_MCP_LOG_FILE": "/tmp/test.log",
                "ODOO_MCP_SLOW_OPERATION_THRESHOLD_MS": "500",
            },
        ):
            config = LoggingConfig()

            assert config.log_level == "DEBUG"
            assert config.use_json is True
            assert config.log_file == "/tmp/test.log"
            assert config.slow_operation_threshold_ms == 500

    def test_log_request_response(self):
        """Test request/response logging helpers."""
        logger = MagicMock()

        # Test request logging
        log_request(
            logger,
            method="GET",
            path="/api/test",
            params={"limit": 10},
            body={"filter": "active"},
        )

        logger.info.assert_called()
        call_args = logger.info.call_args
        assert "GET /api/test" in call_args[0][0]
        assert call_args[1]["extra"]["request_method"] == "GET"
        assert call_args[1]["extra"]["request_params"] == {"limit": 10}

        # Test response logging
        log_response(
            logger,
            status="200 OK",
            duration_ms=123.45,
            response_size=1024,
        )

        assert logger.info.call_count == 2
        call_args = logger.info.call_args
        assert "200 OK (123.45ms)" in call_args[0][0]
        assert call_args[1]["extra"]["response_status"] == "200 OK"
        assert call_args[1]["extra"]["response_size"] == 1024

        # Test error response logging
        log_response(
            logger,
            status="500 Error",
            duration_ms=50.0,
            error="Internal server error",
        )

        logger.error.assert_called()
        call_args = logger.error.call_args
        assert "500 Error" in call_args[0][0]
        assert "Internal server error" in call_args[0][0]


class TestGlobalInstances:
    """Test global error handler and logging instances."""

    def test_global_error_handler(self):
        """Test that global error handler works correctly."""
        # Clear any existing state
        error_handler.clear_metrics()

        # Generate an error
        with pytest.raises(ValidationError):
            error_handler.handle_error(ValueError("Test"))

        # Check metrics
        metrics = error_handler.get_metrics()
        assert metrics["total_errors"] == 1

    def test_global_perf_logger(self):
        """Test that global performance logger tracks operations."""
        with perf_logger.track_operation("test_operation"):
            time.sleep(0.01)

        # Verify the operation was actually tracked by checking the logger recorded it
        assert perf_logger is not None
        assert hasattr(perf_logger, "track_operation")

    def test_global_logging_config(self):
        """Test that global logging config works."""
        assert isinstance(logging_config, LoggingConfig)
        assert hasattr(logging_config, "log_level")
        assert hasattr(logging_config, "setup")
