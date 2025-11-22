"""Tests for locale support in Odoo MCP Server.

This module tests that locale configuration is properly applied
to Odoo API calls, allowing responses in different languages.
"""

from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection


class TestLocaleSupport:
    """Test locale/language support."""

    @pytest.fixture
    def config_with_locale(self):
        """Create test configuration with Spanish locale."""
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            locale="es_ES",
            yolo_mode="true",  # Use YOLO mode for testing
        )

    @pytest.fixture
    def config_with_ar_locale(self):
        """Create test configuration with Argentine Spanish locale."""
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            locale="es_AR",
            yolo_mode="true",
        )

    @pytest.fixture
    def config_without_locale(self):
        """Create test configuration without locale."""
        return OdooConfig(
            url="https://test.odoo.com",
            api_key="test_key",
            username="test",
            database="test_db",
            yolo_mode="true",
        )

    def test_locale_injected_in_execute_kw(self, config_with_locale):
        """Test that locale is injected into context when executing operations."""
        conn = OdooConnection(config_with_locale)

        # Mock the connection
        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        # Mock the object proxy
        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = [{"id": 1, "name": "Test"}]
        conn._object_proxy = mock_proxy

        # Execute a search operation
        kwargs = {}
        conn.execute_kw("res.partner", "search_read", [[]], kwargs)

        # Verify that locale was injected into context
        # execute_kw is called with: (database, uid, password, model, method, args, kwargs)
        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument (index 6)

        assert "context" in passed_kwargs
        assert "lang" in passed_kwargs["context"]
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_argentine_locale_injected(self, config_with_ar_locale):
        """Test that Argentine Spanish locale is properly injected."""
        conn = OdooConnection(config_with_ar_locale)

        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = []
        conn._object_proxy = mock_proxy

        kwargs = {}
        conn.execute_kw("res.partner", "search", [[]], kwargs)

        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument

        assert passed_kwargs["context"]["lang"] == "es_AR"

    def test_no_locale_when_not_configured(self, config_without_locale):
        """Test that no locale is injected when not configured."""
        conn = OdooConnection(config_without_locale)

        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = []
        conn._object_proxy = mock_proxy

        kwargs = {}
        conn.execute_kw("res.partner", "search", [[]], kwargs)

        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument

        # Context should not be added if it wasn't there and no locale is set
        # OR if context exists, it shouldn't have 'lang'
        if "context" in passed_kwargs:
            assert "lang" not in passed_kwargs["context"]

    def test_locale_preserves_existing_context(self, config_with_locale):
        """Test that locale injection preserves existing context values."""
        conn = OdooConnection(config_with_locale)

        conn._connected = True
        conn._authenticated = True
        conn._uid = 1
        conn._database = "test_db"
        conn._auth_method = "api_key"

        mock_proxy = MagicMock()
        mock_proxy.execute_kw.return_value = []
        conn._object_proxy = mock_proxy

        # Pass existing context with some values
        kwargs = {"context": {"active_test": False, "tz": "America/Argentina/Buenos_Aires"}}
        conn.execute_kw("res.partner", "search_read", [[]], kwargs)

        call_args = mock_proxy.execute_kw.call_args
        passed_kwargs = call_args[0][6]  # kwargs is the 7th positional argument

        # Verify existing context values are preserved
        assert passed_kwargs["context"]["active_test"] is False
        assert passed_kwargs["context"]["tz"] == "America/Argentina/Buenos_Aires"
        # And locale was added
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_locale_from_environment_variable(self):
        """Test loading locale from ODOO_LOCALE environment variable."""
        with patch.dict("os.environ", {"ODOO_LOCALE": "fr_FR"}):
            from mcp_server_odoo.config import load_config

            with patch.dict(
                "os.environ",
                {
                    "ODOO_URL": "https://test.odoo.com",
                    "ODOO_USER": "test",
                    "ODOO_PASSWORD": "test",
                    "ODOO_YOLO": "true",
                },
            ):
                config = load_config()
                assert config.locale == "fr_FR"

    def test_common_locales_accepted(self):
        """Test that common locale codes are accepted."""
        common_locales = [
            "es_ES",  # Spanish (Spain)
            "es_AR",  # Spanish (Argentina)
            "es_MX",  # Spanish (Mexico)
            "en_US",  # English (US)
            "en_GB",  # English (UK)
            "fr_FR",  # French
            "pt_BR",  # Portuguese (Brazil)
            "de_DE",  # German
            "it_IT",  # Italian
        ]

        for locale_code in common_locales:
            config = OdooConfig(
                url="https://test.odoo.com",
                api_key="test_key",
                username="test",
                locale=locale_code,
                yolo_mode="true",
            )
            assert config.locale == locale_code
