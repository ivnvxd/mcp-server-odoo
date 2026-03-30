"""Test suite for view translation MCP tools."""

from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.tools import OdooToolHandler


class TestViewTranslationTools:
    """Test cases for read_view_translations and update_view_translation tools."""

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
    def valid_config(self):
        """Create a valid config."""
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        """Create an OdooToolHandler instance."""
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, valid_config)

    def test_translation_tools_registered(self, handler, mock_app):
        """Test that translation tools are registered with FastMCP."""
        assert "read_view_translations" in mock_app._tools
        assert "update_view_translation" in mock_app._tools

    @pytest.mark.asyncio
    async def test_read_view_translations_success(self, handler, mock_connection, mock_app):
        """Test successful read_view_translations with language filter."""
        # Mock execute_kw for get_field_translations
        mock_connection.execute_kw.return_value = (
            [
                {"lang": "en_US", "source": "Contact Us", "value": "Contact Us"},
                {"lang": "fi_FI", "source": "Contact Us", "value": "Ota yhteytta"},
            ],
            {"translation_type": "text", "translation_show_source": True},
        )
        # Mock read for view metadata
        mock_connection.read.return_value = [
            {"id": 42, "name": "Test View", "key": "website.test_view", "write_date": "2026-03-28 10:00:00", "write_uid": [9, "Admin"]}
        ]

        read_view_translations = mock_app._tools["read_view_translations"]
        result = await read_view_translations(view_id=42, langs=["en_US", "fi_FI"])

        # Verify the result is a ViewTranslationsResult
        assert len(result.translations) == 2
        assert result.translation_type == "text"
        assert result.translation_show_source is True
        assert result.view_info["name"] == "Test View"

        # Verify execute_kw was called correctly
        mock_connection.execute_kw.assert_called_once_with(
            "ir.ui.view", "get_field_translations",
            [[42], "arch_db"], {"langs": ["en_US", "fi_FI"]}
        )

    @pytest.mark.asyncio
    async def test_read_view_translations_no_langs(self, handler, mock_connection, mock_app):
        """Test read_view_translations without language filter (all installed)."""
        mock_connection.execute_kw.return_value = (
            [{"lang": "en_US", "source": "Hello", "value": "Hello"}],
            {"translation_type": "text", "translation_show_source": True},
        )
        mock_connection.read.return_value = [
            {"id": 10, "name": "View", "key": "website.view", "write_date": "2026-03-28", "write_uid": [1, "Admin"]}
        ]

        read_view_translations = mock_app._tools["read_view_translations"]
        result = await read_view_translations(view_id=10)

        # Should not pass langs kwarg when None
        mock_connection.execute_kw.assert_called_once_with(
            "ir.ui.view", "get_field_translations",
            [[10], "arch_db"], {}
        )

    @pytest.mark.asyncio
    async def test_read_view_translations_not_authenticated(self, handler, mock_connection, mock_app):
        """Test read_view_translations when not authenticated."""
        mock_connection.is_authenticated = False

        read_view_translations = mock_app._tools["read_view_translations"]
        with pytest.raises(ValidationError, match="Not authenticated"):
            await read_view_translations(view_id=42)

    @pytest.mark.asyncio
    async def test_update_view_translation_success(self, handler, mock_connection, mock_app):
        """Test successful update_view_translation."""
        mock_connection.execute_kw.return_value = True

        update_view_translation = mock_app._tools["update_view_translation"]
        result = await update_view_translation(
            view_id=42,
            translations={"fi_FI": {"Contact Us": "Ota yhteytta", "Learn More": "Lue lisaa"}},
        )

        assert result.success is True
        assert result.updated_langs == ["fi_FI"]
        assert "2" in result.message  # 2 terms
        assert "fi_FI" in result.message

        mock_connection.execute_kw.assert_called_once_with(
            "ir.ui.view", "update_field_translations",
            [[42], "arch_db", {"fi_FI": {"Contact Us": "Ota yhteytta", "Learn More": "Lue lisaa"}}],
            {}
        )

    @pytest.mark.asyncio
    async def test_update_view_translation_multiple_langs(self, handler, mock_connection, mock_app):
        """Test updating multiple languages at once."""
        mock_connection.execute_kw.return_value = True

        update_view_translation = mock_app._tools["update_view_translation"]
        result = await update_view_translation(
            view_id=42,
            translations={
                "fi_FI": {"Hello": "Hei"},
                "de_DE": {"Hello": "Hallo"},
            },
        )

        assert result.success is True
        assert set(result.updated_langs) == {"fi_FI", "de_DE"}

    @pytest.mark.asyncio
    async def test_update_view_translation_empty(self, handler, mock_connection, mock_app):
        """Test update_view_translation with empty translations dict."""
        update_view_translation = mock_app._tools["update_view_translation"]
        with pytest.raises(ValidationError, match="No translations provided"):
            await update_view_translation(view_id=42, translations={})

    @pytest.mark.asyncio
    async def test_update_view_translation_invalid_structure(self, handler, mock_connection, mock_app):
        """Test update_view_translation with invalid translation structure."""
        update_view_translation = mock_app._tools["update_view_translation"]
        with pytest.raises(ValidationError, match="must be a dict"):
            await update_view_translation(view_id=42, translations={"fi_FI": "not a dict"})

    @pytest.mark.asyncio
    async def test_update_view_translation_not_authenticated(self, handler, mock_connection, mock_app):
        """Test update_view_translation when not authenticated."""
        mock_connection.is_authenticated = False

        update_view_translation = mock_app._tools["update_view_translation"]
        with pytest.raises(ValidationError, match="Not authenticated"):
            await update_view_translation(
                view_id=42,
                translations={"fi_FI": {"Hello": "Hei"}},
            )
