"""Tests for export_records MCP tool."""

from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.schemas import (
    ExportAccessDeniedError,
    ExportBlockedExceedsLimitError,
    ExportFileError,
    ExportSuccessResult,
)
from mcp_server_odoo.tools import OdooToolHandler


class TestExportRecordsToolRegistration:
    """Test cases for export_records tool registration."""

    @pytest.fixture
    def mock_app(self):
        """Create a mock FastMCP app."""
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        """Create a mock OdooConnection."""
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        """Create a mock AccessController."""
        return MagicMock(spec=AccessController)

    @pytest.fixture
    def config_with_export_enabled(self, tmp_path):
        """Config with export enabled."""
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
            export_enabled=True,
            _export_dir=tmp_path,
        )

    @pytest.fixture
    def config_with_export_disabled(self, tmp_path):
        """Config with export disabled."""
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
            export_enabled=False,
            _export_dir=tmp_path,
        )

    def test_export_records_tool_registered_when_enabled(
        self, mock_app, mock_connection, mock_access_controller, config_with_export_enabled
    ):
        """When export_enabled=True, export_records appears in registered tools."""
        OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_with_export_enabled
        )
        assert "export_records" in mock_app._tools

    def test_export_records_tool_not_registered_when_disabled(
        self, mock_app, mock_connection, mock_access_controller, config_with_export_disabled
    ):
        """When export_enabled=False, export_records does NOT appear in registered tools."""
        OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_with_export_disabled
        )
        assert "export_records" not in mock_app._tools

    def test_other_tools_still_registered_when_export_disabled(
        self, mock_app, mock_connection, mock_access_controller, config_with_export_disabled
    ):
        """Disabling export doesn't affect other tools."""
        OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_with_export_disabled
        )
        # These should still be registered
        assert "search_records" in mock_app._tools
        assert "aggregate_records" in mock_app._tools
        assert "list_models" in mock_app._tools


class TestExportRecordsToolHandler:
    """Test cases for export_records tool handler."""

    @pytest.fixture
    def mock_app(self):
        """Create a mock FastMCP app."""
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator(**kwargs):
            def decorator(func):
                app._tools[func.__name__] = func
                return func

            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        """Create a mock OdooConnection."""
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        """Create a mock AccessController."""
        controller = MagicMock(spec=AccessController)
        return controller

    @pytest.fixture
    def config_with_export(self, tmp_path):
        """Config with export enabled."""
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
            export_enabled=True,
            export_max_rows=10000,
            export_batch_size=500,
            _export_dir=tmp_path,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, config_with_export):
        """Create an OdooToolHandler instance with export enabled."""
        return OdooToolHandler(
            mock_app, mock_connection, mock_access_controller, config_with_export
        )

    @pytest.mark.asyncio
    async def test_export_records_success_path(
        self, handler, mock_connection, mock_access_controller, mock_app, tmp_path
    ):
        """Happy path: export returns ExportSuccessResult with file metadata."""
        # Setup mocks
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            3,  # search_count
            [
                {"id": 1, "display_name": "Rec 1"},
                {"id": 2, "display_name": "Rec 2"},
                {"id": 3, "display_name": "Rec 3"},
            ],
        ]

        export_records = mock_app._tools["export_records"]
        result = await export_records(
            model="res.partner",
            domain=[["active", "=", True]],
            fields=["id", "display_name"],
        )

        assert isinstance(result, ExportSuccessResult)
        assert result.success is True
        assert result.row_count == 3
        assert result.file_path.endswith(".csv")
        assert result.file_size_bytes > 0
        assert result.truncated is False
        assert result.max_rows_limit == 10000
        assert len(result.preview) <= 10

    @pytest.mark.asyncio
    async def test_export_records_calls_execute_export(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Verify execute_export is called with correct arguments."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1, "display_name": "Test"}],
        ]

        export_records = mock_app._tools["export_records"]

        with patch("mcp_server_odoo.tools.execute_export") as mock_exec:
            mock_exec.return_value = ExportSuccessResult(
                file_path="/tmp/test.csv",
                file_size_bytes=100,
                row_count=1,
                truncated=False,
                max_rows_limit=10000,
                preview=["id,display_name", "1,Test"],
                duration_ms=50,
                exported_at="2026-06-15T00:00:00Z",
            )

            await export_records(
                model="res.partner",
                domain=[["active", "=", True]],
                fields=["id", "display_name"],
            )

            mock_exec.assert_called_once()
            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs["model"] == "res.partner"
            assert call_kwargs["domain"] == [["active", "=", True]]
            assert call_kwargs["fields"] == ["id", "display_name"]
            assert "config" in call_kwargs
            assert "odoo_connection" in call_kwargs
            assert "access_controller" in call_kwargs

    @pytest.mark.asyncio
    async def test_export_records_passes_config_to_execute_export(
        self, handler, mock_connection, mock_access_controller, mock_app, tmp_path
    ):
        """Config values are passed through to execute_export."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1}],
        ]

        export_records = mock_app._tools["export_records"]

        with patch("mcp_server_odoo.tools.execute_export") as mock_exec:
            mock_exec.return_value = ExportSuccessResult(
                file_path="/tmp/test.csv",
                file_size_bytes=100,
                row_count=1,
                truncated=False,
                max_rows_limit=10000,
                preview=["id", "1"],
                duration_ms=50,
                exported_at="2026-06-15T00:00:00Z",
            )

            await export_records(
                model="res.partner",
                domain=[],
                fields=["id"],
            )

            call_kwargs = mock_exec.call_args.kwargs
            config = call_kwargs["config"]
            assert config.export_dir == tmp_path
            assert config.export_max_rows == 10000
            assert config.export_batch_size == 500

    @pytest.mark.asyncio
    async def test_export_records_blocked_exceeds_limit(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When execute_export returns ExportBlockedExceedsLimitError, tool returns it."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            250000,  # search_count exceeds limit
        ]

        export_records = mock_app._tools["export_records"]
        result = await export_records(
            model="res.partner",
            domain=[["active", "=", True]],
            fields=["id"],
        )

        assert isinstance(result, ExportBlockedExceedsLimitError)
        assert result.success is False
        assert result.error == "export_blocked_exceeds_limit"
        assert result.matched_count == 250000
        assert result.max_rows_limit == 10000
        assert "aggregate_records" in result.suggestion

    @pytest.mark.asyncio
    async def test_export_records_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When execute_export returns ExportAccessDeniedError, tool returns it."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = Exception("Access denied")

        export_records = mock_app._tools["export_records"]
        result = await export_records(
            model="restricted.model",
            domain=[],
            fields=["id"],
        )

        assert isinstance(result, ExportAccessDeniedError)
        assert result.success is False
        assert result.error == "access_denied"

    @pytest.mark.asyncio
    async def test_export_records_not_authenticated(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When not authenticated, raises ValidationError."""
        mock_connection.is_authenticated = False

        export_records = mock_app._tools["export_records"]

        from mcp_server_odoo.error_handling import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            await export_records(model="res.partner")

        assert "Not authenticated" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_export_records_domain_parsing(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Domain string is parsed correctly before being passed to execute_export."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1, "display_name": "Test"}],
        ]

        export_records = mock_app._tools["export_records"]

        with patch("mcp_server_odoo.tools.execute_export") as mock_exec:
            mock_exec.return_value = ExportSuccessResult(
                file_path="/tmp/test.csv",
                file_size_bytes=100,
                row_count=1,
                truncated=False,
                max_rows_limit=10000,
                preview=["id,display_name", "1,Test"],
                duration_ms=50,
                exported_at="2026-06-15T00:00:00Z",
            )

            await export_records(
                model="res.partner",
                domain='[["active", "=", true]]',
                fields=["id"],
            )

            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs["domain"] == [["active", "=", True]]

    @pytest.mark.asyncio
    async def test_export_records_empty_domain(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Empty domain (None) is handled correctly."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            2,
            [{"id": 1}, {"id": 2}],
        ]

        export_records = mock_app._tools["export_records"]

        with patch("mcp_server_odoo.tools.execute_export") as mock_exec:
            mock_exec.return_value = ExportSuccessResult(
                file_path="/tmp/test.csv",
                file_size_bytes=100,
                row_count=2,
                truncated=False,
                max_rows_limit=10000,
                preview=["id", "1", "2"],
                duration_ms=50,
                exported_at="2026-06-15T00:00:00Z",
            )

            await export_records(
                model="res.partner",
                domain=None,
                fields=["id"],
            )

            call_kwargs = mock_exec.call_args.kwargs
            assert call_kwargs["domain"] == []

    @pytest.mark.asyncio
    async def test_export_records_smarts_default_fields(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When fields is None, _get_smart_default_fields is called."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1, "display_name": "Test"}],
        ]
        mock_connection.fields_get.return_value = {
            "id": {"type": "integer"},
            "display_name": {"type": "char"},
            "email": {"type": "char"},
        }

        export_records = mock_app._tools["export_records"]

        with patch("mcp_server_odoo.tools.execute_export") as mock_exec:
            mock_exec.return_value = ExportSuccessResult(
                file_path="/tmp/test.csv",
                file_size_bytes=100,
                row_count=1,
                truncated=False,
                max_rows_limit=10000,
                preview=["id,display_name", "1,Test"],
                duration_ms=50,
                exported_at="2026-06-15T00:00:00Z",
            )

            await export_records(
                model="res.partner",
                domain=[],
                fields=None,
            )

            # execute_export should have been called with smart default fields
            call_kwargs = mock_exec.call_args.kwargs
            # fields should be the result of _get_smart_default_fields
            assert call_kwargs["fields"] is not None

    @pytest.mark.asyncio
    async def test_export_records_file_error(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """When execute_export returns ExportFileError, tool returns it."""
        mock_access_controller.validate_model_access.return_value = None
        mock_connection.execute_kw.side_effect = [
            1,
            [{"id": 1}],
        ]

        export_records = mock_app._tools["export_records"]

        with patch("mcp_server_odoo.tools.execute_export") as mock_exec:
            mock_exec.return_value = ExportFileError(message="Disk full or permission denied")

            result = await export_records(
                model="res.partner",
                domain=[],
                fields=["id"],
            )

            assert isinstance(result, ExportFileError)
            assert result.success is False
            assert result.error == "file_write_error"
