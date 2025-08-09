"""Tests for OdooConnection write operations (create, write, unlink)."""

import os
from unittest.mock import Mock, patch

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


class TestWriteOperations:
    """Test write operations in OdooConnection."""

    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = Mock(spec=OdooConfig)
        config.url = os.getenv("ODOO_URL", "http://localhost:8069")
        config.db = "test"
        config.username = "test"
        config.password = "test"
        config.api_key = None
        config.uses_api_key = False
        config.uses_credentials = True
        config.is_yolo_enabled = False
        config.yolo_mode = "off"
        config.get_endpoint_paths.return_value = {
            "db": "/mcp/xmlrpc/db",
            "common": "/mcp/xmlrpc/common",
            "object": "/mcp/xmlrpc/object",
        }
        return config

    @pytest.fixture
    def mock_performance_manager(self, mock_config):
        """Create mock performance manager."""
        manager = Mock()
        # Create a proper context manager mock
        cm = Mock()
        cm.__enter__ = Mock(return_value=None)
        cm.__exit__ = Mock(return_value=None)
        manager.monitor.track_operation.return_value = cm
        manager.invalidate_record_cache = Mock()
        manager.get_optimized_connection = Mock(return_value=Mock())
        return manager

    @pytest.fixture
    def connection(self, mock_config, mock_performance_manager):
        """Create OdooConnection with mocks."""
        conn = OdooConnection(mock_config, performance_manager=mock_performance_manager)
        conn._connected = True
        conn._authenticated = True
        conn._uid = 2
        conn._database = "test"
        conn._auth_method = "password"
        return conn

    def test_create_record(self, connection):
        """Test creating a record."""
        model = "res.partner"
        values = {"name": "Test Partner", "email": "test@example.com"}
        expected_id = 123

        with patch.object(connection, "execute_kw", return_value=expected_id) as mock_execute:
            result = connection.create(model, values)

            assert result == expected_id
            mock_execute.assert_called_once_with(model, "create", [values], {})
            connection._performance_manager.invalidate_record_cache.assert_called_once_with(model)

    def test_create_record_error(self, connection):
        """Test create record with error."""
        model = "res.partner"
        values = {"name": "Test"}

        with patch.object(
            connection, "execute_kw", side_effect=OdooConnectionError("Create failed")
        ):
            with pytest.raises(OdooConnectionError, match="Create failed"):
                connection.create(model, values)

    def test_write_records(self, connection):
        """Test updating records."""
        model = "res.partner"
        ids = [123, 124]
        values = {"email": "updated@example.com"}

        with patch.object(connection, "execute_kw", return_value=True) as mock_execute:
            result = connection.write(model, ids, values)

            assert result is True
            mock_execute.assert_called_once_with(model, "write", [ids, values], {})
            # Should invalidate cache for each record
            assert connection._performance_manager.invalidate_record_cache.call_count == 2
            connection._performance_manager.invalidate_record_cache.assert_any_call(model, 123)
            connection._performance_manager.invalidate_record_cache.assert_any_call(model, 124)

    def test_write_single_record(self, connection):
        """Test updating a single record."""
        model = "res.partner"
        ids = [123]
        values = {"name": "Updated Name"}

        with patch.object(connection, "execute_kw", return_value=True):
            result = connection.write(model, ids, values)

            assert result is True
            connection._performance_manager.invalidate_record_cache.assert_called_once_with(
                model, 123
            )

    def test_write_records_error(self, connection):
        """Test write records with error."""
        model = "res.partner"
        ids = [123]
        values = {"name": "Test"}

        with patch.object(
            connection, "execute_kw", side_effect=OdooConnectionError("Write failed")
        ):
            with pytest.raises(OdooConnectionError, match="Write failed"):
                connection.write(model, ids, values)

    def test_unlink_records(self, connection):
        """Test deleting records."""
        model = "res.partner"
        ids = [123, 124, 125]

        with patch.object(connection, "execute_kw", return_value=True) as mock_execute:
            result = connection.unlink(model, ids)

            assert result is True
            mock_execute.assert_called_once_with(model, "unlink", [ids], {})
            # Should invalidate cache for each record
            assert connection._performance_manager.invalidate_record_cache.call_count == 3
            connection._performance_manager.invalidate_record_cache.assert_any_call(model, 123)
            connection._performance_manager.invalidate_record_cache.assert_any_call(model, 124)
            connection._performance_manager.invalidate_record_cache.assert_any_call(model, 125)

    def test_unlink_single_record(self, connection):
        """Test deleting a single record."""
        model = "res.partner"
        ids = [123]

        with patch.object(connection, "execute_kw", return_value=True):
            result = connection.unlink(model, ids)

            assert result is True
            connection._performance_manager.invalidate_record_cache.assert_called_once_with(
                model, 123
            )

    def test_unlink_records_error(self, connection):
        """Test unlink records with error."""
        model = "res.partner"
        ids = [123]

        with patch.object(
            connection, "execute_kw", side_effect=OdooConnectionError("Delete failed")
        ):
            with pytest.raises(OdooConnectionError, match="Delete failed"):
                connection.unlink(model, ids)

    def test_write_operations_performance_tracking(self, connection):
        """Test that write operations track performance."""
        # Test create
        with patch.object(connection, "execute_kw", return_value=123):
            connection.create("res.partner", {"name": "Test"})
            connection._performance_manager.monitor.track_operation.assert_called_with(
                "create_res.partner"
            )

        # Test write
        with patch.object(connection, "execute_kw", return_value=True):
            connection.write("res.partner", [123], {"name": "Updated"})
            connection._performance_manager.monitor.track_operation.assert_called_with(
                "write_res.partner"
            )

        # Test unlink
        with patch.object(connection, "execute_kw", return_value=True):
            connection.unlink("res.partner", [123])
            connection._performance_manager.monitor.track_operation.assert_called_with(
                "unlink_res.partner"
            )

    def test_write_operations_not_authenticated(self, connection):
        """Test write operations when not authenticated."""
        connection._authenticated = False

        # Since execute_kw checks authentication, the error is raised from there
        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            connection.create("res.partner", {"name": "Test"})

        # Reset for next test
        connection._authenticated = False
        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            connection.write("res.partner", [123], {"name": "Test"})

        # Reset for next test
        connection._authenticated = False
        with pytest.raises(OdooConnectionError, match="Not authenticated"):
            connection.unlink("res.partner", [123])
