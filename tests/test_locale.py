"""Tests for locale support in Odoo MCP Server."""

import xmlrpc.client
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError


@pytest.fixture
def config_with_locale():
    return OdooConfig(
        url="https://test.odoo.com",
        api_key="test_key",
        username="test",
        database="test_db",
        locale="es_ES",
        yolo_mode="true",
    )


@pytest.fixture
def config_without_locale():
    return OdooConfig(
        url="https://test.odoo.com",
        api_key="test_key",
        username="test",
        database="test_db",
        yolo_mode="true",
    )


def _make_connected(conn):
    """Set up a connection as authenticated with a mocked proxy."""
    conn._connected = True
    conn._authenticated = True
    conn._uid = 1
    conn._database = "test_db"
    conn._auth_method = "api_key"
    mock_proxy = MagicMock()
    mock_proxy.execute_kw.return_value = []
    conn._object_proxy = mock_proxy
    return mock_proxy


class TestLocaleInjection:
    def test_locale_injected_into_context(self, config_with_locale):
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        conn.execute_kw("res.partner", "search_read", [[]], {})

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_no_locale_when_not_configured(self, config_without_locale):
        conn = OdooConnection(config_without_locale)
        mock_proxy = _make_connected(conn)

        conn.execute_kw("res.partner", "search", [[]], {})

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert "context" not in passed_kwargs or "lang" not in passed_kwargs.get("context", {})

    def test_locale_preserves_existing_context(self, config_with_locale):
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        conn.execute_kw(
            "res.partner",
            "search_read",
            [[]],
            {"context": {"active_test": False, "tz": "Europe/Berlin"}},
        )

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["active_test"] is False
        assert passed_kwargs["context"]["tz"] == "Europe/Berlin"
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_caller_lang_takes_precedence(self, config_with_locale):
        """Explicit lang in caller context should not be overwritten by ODOO_LOCALE."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        conn.execute_kw(
            "res.partner",
            "search_read",
            [[]],
            {"context": {"lang": "de_DE"}},
        )

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "de_DE"

    def test_locale_works_through_convenience_methods(self, config_with_locale):
        """Locale should be injected when using search/read/search_read helpers."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        conn.search_read("res.partner", [["is_company", "=", True]], fields=["name"])

        passed_kwargs = mock_proxy.execute_kw.call_args[0][6]
        assert passed_kwargs["context"]["lang"] == "es_ES"

    def test_locale_does_not_mutate_shared_kwargs(self, config_with_locale):
        """Ensure locale injection doesn't leak between calls via shared dicts."""
        conn = OdooConnection(config_with_locale)
        _make_connected(conn)

        shared_kwargs = {"limit": 5}
        conn.execute_kw("res.partner", "search", [[]], shared_kwargs)

        conn2 = OdooConnection(
            OdooConfig(
                url="https://test.odoo.com",
                api_key="test_key",
                username="test",
                database="test_db",
                yolo_mode="true",
            )
        )
        mock_proxy2 = _make_connected(conn2)
        fresh_kwargs = {"limit": 5}
        conn2.execute_kw("res.partner", "search", [[]], fresh_kwargs)

        passed_kwargs2 = mock_proxy2.execute_kw.call_args[0][6]
        assert "context" not in passed_kwargs2 or "lang" not in passed_kwargs2.get("context", {})


class TestLocaleInvalidFallback:
    def test_invalid_locale_falls_back_and_retries(self, config_with_locale):
        """Odoo rejects invalid locale → disable locale, retry succeeds."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        fault = xmlrpc.client.Fault(1, "Invalid language code: es_ES")
        mock_proxy.execute_kw.side_effect = [fault, [{"id": 1, "name": "Test"}]]

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 1, "name": "Test"}]
        assert conn.config.locale is None
        assert mock_proxy.execute_kw.call_count == 2

    def test_retry_does_not_include_lang(self, config_with_locale):
        """After fallback, the retry call should not have lang in context."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        fault = xmlrpc.client.Fault(1, "Invalid language code: es_ES")
        mock_proxy.execute_kw.side_effect = [fault, []]

        conn.execute_kw("res.partner", "search", [[]], {})

        # Second call (the retry) should not have lang
        retry_kwargs = mock_proxy.execute_kw.call_args_list[1][0][6]
        assert "lang" not in retry_kwargs.get("context", {})

    def test_invalid_locale_code_cleared_and_retried(self, config_without_locale):
        """Setting an invalid locale triggers fallback: locale cleared, call retried."""
        conn = OdooConnection(config_without_locale)
        mock_proxy = _make_connected(conn)

        # Simulate an invalid locale set at runtime
        conn.config.locale = "invalid_XX"

        fault = xmlrpc.client.Fault(1, "Invalid language code: invalid_XX")
        mock_proxy.execute_kw.side_effect = [fault, [{"id": 7}]]

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 7}]
        assert conn.config.locale is None
        assert mock_proxy.execute_kw.call_count == 2

    def test_other_faults_still_raise(self, config_with_locale):
        """Non-locale faults should propagate as OdooConnectionError."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        fault = xmlrpc.client.Fault(2, "Access denied")
        mock_proxy.execute_kw.side_effect = fault

        with pytest.raises(OdooConnectionError, match="Operation failed"):
            conn.execute_kw("res.partner", "search", [[]], {})

        # Locale should NOT be disabled for unrelated faults
        assert conn.config.locale == "es_ES"


class TestLocaleConfig:
    def test_locale_from_env(self):
        with patch.dict(
            "os.environ",
            {
                "ODOO_URL": "https://test.odoo.com",
                "ODOO_USER": "test",
                "ODOO_PASSWORD": "test",
                "ODOO_LOCALE": "fr_FR",
                "ODOO_YOLO": "true",
            },
        ):
            config = load_config()
            assert config.locale == "fr_FR"

    def test_no_locale_by_default(self):
        with patch.dict(
            "os.environ",
            {
                "ODOO_URL": "https://test.odoo.com",
                "ODOO_USER": "test",
                "ODOO_PASSWORD": "test",
                "ODOO_YOLO": "true",
            },
            clear=True,
        ):
            config = load_config()
            assert config.locale is None

    def test_empty_locale_treated_as_none(self):
        with patch.dict(
            "os.environ",
            {
                "ODOO_URL": "https://test.odoo.com",
                "ODOO_USER": "test",
                "ODOO_PASSWORD": "test",
                "ODOO_LOCALE": "  ",
                "ODOO_YOLO": "true",
            },
        ):
            config = load_config()
            assert config.locale is None
