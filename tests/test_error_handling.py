"""Test the error handling module."""

import unittest
from unittest.mock import patch

from mcp_server_odoo.error_handling import (
    AuthenticationError,
    ConnectionError,
    MCPOdooError,
    PermissionError,
    ResourceNotFoundError,
    ValidationError,
    format_error_response,
    handle_exceptions,
)


class TestErrorClasses(unittest.TestCase):
    """Test error classes from the error_handling module."""

    def test_base_error_class(self):
        """Test the base MCPOdooError class."""
        error = MCPOdooError("Test error message")
        self.assertEqual(error.message, "Test error message")
        self.assertEqual(error.status_code, 500)
        self.assertEqual(str(error), "Test error message")

        # Test with custom status code
        error = MCPOdooError("Custom status", 418)
        self.assertEqual(error.status_code, 418)

    def test_authentication_error(self):
        """Test the AuthenticationError class."""
        error = AuthenticationError("Auth failed")
        self.assertEqual(error.message, "Auth failed")
        self.assertEqual(error.status_code, 401)

    def test_permission_error(self):
        """Test the PermissionError class."""
        error = PermissionError("Permission denied")
        self.assertEqual(error.message, "Permission denied")
        self.assertEqual(error.status_code, 403)

    def test_resource_not_found_error(self):
        """Test the ResourceNotFoundError class."""
        error = ResourceNotFoundError("Resource not found")
        self.assertEqual(error.message, "Resource not found")
        self.assertEqual(error.status_code, 404)

    def test_validation_error(self):
        """Test the ValidationError class."""
        error = ValidationError("Invalid data")
        self.assertEqual(error.message, "Invalid data")
        self.assertEqual(error.status_code, 400)

    def test_connection_error(self):
        """Test the ConnectionError class."""
        error = ConnectionError("Connection failed")
        self.assertEqual(error.message, "Connection failed")
        self.assertEqual(error.status_code, 503)


class TestFormatErrorResponse(unittest.TestCase):
    """Test format_error_response function."""

    @patch("mcp_server_odoo.error_handling.logger")
    def test_format_mcp_error(self, mock_logger):
        """Test formatting an MCPOdooError."""
        error = ValidationError("Invalid input data")
        response = format_error_response(error)

        self.assertTrue(response["is_error"])
        self.assertEqual(response["content"][0]["type"], "text")
        self.assertEqual(
            response["content"][0]["text"], "Invalid request: Invalid input data"
        )

        # Verify logging (only error, not traceback for non-500 errors)
        mock_logger.error.assert_called_once_with("Error 400: Invalid input data")

    @patch("mcp_server_odoo.error_handling.logger")
    def test_format_standard_exception(self, mock_logger):
        """Test formatting a standard Python exception."""
        error = ValueError("Something went wrong")
        response = format_error_response(error)

        self.assertTrue(response["is_error"])
        self.assertEqual(response["content"][0]["type"], "text")
        self.assertEqual(
            response["content"][0]["text"], "Server error: Something went wrong"
        )

        # Verify both error and traceback logged for 500 errors
        self.assertEqual(mock_logger.error.call_count, 2)

    @patch("mcp_server_odoo.error_handling.logger")
    def test_format_error_with_custom_status(self, mock_logger):
        """Test formatting an error with a custom status code."""
        error = MCPOdooError("Custom teapot error", 418)
        response = format_error_response(error)

        self.assertTrue(response["is_error"])
        self.assertEqual(response["content"][0]["text"], "Error: Custom teapot error")
        mock_logger.error.assert_called_once_with("Error 418: Custom teapot error")


class TestHandleExceptions(unittest.TestCase):
    """Test the handle_exceptions decorator."""

    def test_normal_execution(self):
        """Test normal execution path with no exceptions."""

        @handle_exceptions
        def sample_function():
            return {"is_error": False, "data": "success"}

        result = sample_function()
        self.assertFalse(result["is_error"])
        self.assertEqual(result["data"], "success")

    def test_with_mcp_error(self):
        """Test with an MCPOdooError being raised."""

        @handle_exceptions
        def failing_function():
            raise ValidationError("Invalid data")

        result = failing_function()
        self.assertTrue(result["is_error"])
        self.assertEqual(result["content"][0]["text"], "Invalid request: Invalid data")

    def test_with_standard_exception(self):
        """Test with a standard exception being raised."""

        @handle_exceptions
        def failing_function():
            raise ValueError("Something went wrong")

        result = failing_function()
        self.assertTrue(result["is_error"])
        self.assertEqual(
            result["content"][0]["text"], "Server error: Something went wrong"
        )


if __name__ == "__main__":
    unittest.main()
