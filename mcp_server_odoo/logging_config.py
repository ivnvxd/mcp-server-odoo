"""Structured logging configuration for Odoo MCP Server.

This module provides centralized logging setup with:
- Structured logging with JSON formatting option
- Log level configuration from environment
- Request/response logging
- Performance tracking
"""

import json
import logging
import logging.handlers
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Default log format
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
JSON_FORMAT = '{"timestamp": "%(asctime)s", "logger": "%(name)s", "level": "%(levelname)s", "message": "%(message)s"}'


class StructuredFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        # Base log data
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields if present
        if hasattr(record, "error_code"):
            log_data["error_code"] = record.error_code
        if hasattr(record, "error_details"):
            log_data["error_details"] = record.error_details
        if hasattr(record, "error_context"):
            log_data["error_context"] = record.error_context
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        if hasattr(record, "model"):
            log_data["model"] = record.model
        if hasattr(record, "operation"):
            log_data["operation"] = record.operation

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


class RequestLoggingAdapter(logging.LoggerAdapter):
    """Logging adapter that adds request context to log records."""

    def __init__(self, logger: logging.Logger, request_id: str | None = None):
        """Initialize adapter with request ID."""
        self.request_id = request_id or self._generate_request_id()
        super().__init__(logger, {"request_id": self.request_id})

    def _generate_request_id(self) -> str:
        """Generate a unique request ID."""
        import uuid

        return str(uuid.uuid4())

    def process(self, msg, kwargs):
        """Process log message to add request ID."""
        if "extra" not in kwargs:
            kwargs["extra"] = {}
        kwargs["extra"]["request_id"] = self.request_id
        return msg, kwargs


class PerformanceLogger:
    """Logger for tracking operation performance."""

    def __init__(self, logger: logging.Logger):
        """Initialize performance logger."""
        self.logger = logger
        self._timers: Dict[str, float] = {}

    @contextmanager
    def track_operation(
        self,
        operation: str,
        model: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        """Context manager for tracking operation duration.

        Usage:
            with perf_logger.track_operation("search", model="res.partner"):
                # Perform operation
                pass
        """
        start_time = time.time()
        timer_id = f"{operation}_{id(start_time)}"
        self._timers[timer_id] = start_time

        try:
            yield
        finally:
            duration_ms = (time.time() - start_time) * 1000
            self._timers.pop(timer_id, None)

            log_data = {
                "operation": operation,
                "duration_ms": round(duration_ms, 2),
            }
            if model:
                log_data["model"] = model
            if extra:
                log_data.update(extra)

            self.logger.info(
                f"Operation '{operation}' completed in {duration_ms:.2f}ms",
                extra=log_data,
            )

            # Log warning for slow operations
            if duration_ms > 1000:  # More than 1 second
                self.logger.warning(
                    f"Slow operation detected: '{operation}' took {duration_ms:.2f}ms",
                    extra=log_data,
                )


def setup_logging(
    log_level: Optional[str] = None,
    log_format: Optional[str] = None,
    use_json: bool = False,
    log_file: Optional[str] = None,
) -> None:
    """Set up structured logging for the MCP server.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: Custom log format string
        use_json: Whether to use JSON formatting
        log_file: Optional log file path
    """
    # Get log level from environment or parameter
    if log_level is None:
        log_level = os.getenv("ODOO_MCP_LOG_LEVEL", "INFO")

    # Convert to logging level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create formatter
    if use_json or os.getenv("ODOO_MCP_LOG_JSON", "").lower() == "true":
        formatter = StructuredFormatter()
    else:
        format_string = log_format or os.getenv("ODOO_MCP_LOG_FORMAT", DEFAULT_FORMAT)
        formatter = logging.Formatter(format_string)

    # Console handler - MUST use stderr for MCP servers
    # MCP uses stdout for JSON-RPC communication, so logging must go to stderr
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler if specified
    if log_file or os.getenv("ODOO_MCP_LOG_FILE"):
        file_path = log_file or os.getenv("ODOO_MCP_LOG_FILE")
        file_handler = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Set specific loggers
    logging.getLogger("mcp_server_odoo").setLevel(numeric_level)

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(
    name: str, request_id: Optional[str] = None
) -> logging.Logger | logging.LoggerAdapter:
    """Get a logger instance with optional request context.

    Args:
        name: Logger name (usually __name__)
        request_id: Optional request ID for correlation

    Returns:
        Logger instance with request context if provided
    """
    logger = logging.getLogger(name)

    if request_id:
        return RequestLoggingAdapter(logger, request_id)

    return logger


def log_request(
    logger: logging.Logger,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Any] = None,
):
    """Log an incoming request.

    Args:
        logger: Logger instance
        method: HTTP method or operation type
        path: Request path or resource URI
        params: Query parameters
        body: Request body
    """
    log_data = {
        "request_method": method,
        "request_path": path,
    }

    if params:
        log_data["request_params"] = params

    # Limit body size in logs
    if body:
        body_str = str(body)
        if len(body_str) > 1000:
            body_str = body_str[:1000] + "..."
        log_data["request_body"] = body_str

    logger.info(f"Request: {method} {path}", extra=log_data)


def log_response(
    logger: logging.Logger,
    status: str,
    duration_ms: float,
    response_size: Optional[int] = None,
    error: Optional[str] = None,
):
    """Log a response.

    Args:
        logger: Logger instance
        status: Response status
        duration_ms: Request duration in milliseconds
        response_size: Size of response in bytes
        error: Error message if applicable
    """
    log_data = {
        "response_status": status,
        "duration_ms": round(duration_ms, 2),
    }

    if response_size is not None:
        log_data["response_size"] = response_size

    if error:
        log_data["error"] = error
        logger.error(f"Response: {status} ({duration_ms:.2f}ms) - Error: {error}", extra=log_data)
    else:
        logger.info(f"Response: {status} ({duration_ms:.2f}ms)", extra=log_data)


class LoggingConfig:
    """Configuration class for logging settings."""

    def __init__(self):
        """Initialize logging configuration from environment."""
        self.log_level = os.getenv("ODOO_MCP_LOG_LEVEL", "INFO")
        self.log_format = os.getenv("ODOO_MCP_LOG_FORMAT", DEFAULT_FORMAT)
        self.use_json = os.getenv("ODOO_MCP_LOG_JSON", "false").lower() == "true"
        self.log_file = os.getenv("ODOO_MCP_LOG_FILE")
        self.log_request_body = os.getenv("ODOO_MCP_LOG_REQUEST_BODY", "false").lower() == "true"
        self.log_response_body = os.getenv("ODOO_MCP_LOG_RESPONSE_BODY", "false").lower() == "true"
        self.slow_operation_threshold_ms = int(
            os.getenv("ODOO_MCP_SLOW_OPERATION_THRESHOLD_MS", "1000")
        )

    def setup(self):
        """Set up logging with current configuration."""
        setup_logging(
            log_level=self.log_level,
            log_format=self.log_format,
            use_json=self.use_json,
            log_file=self.log_file,
        )


# Initialize logging configuration
logging_config = LoggingConfig()

# Create performance logger instance
perf_logger = PerformanceLogger(logging.getLogger("mcp_server_odoo.performance"))
