"""Tests for locale support in Odoo MCP Server."""

import xmlrpc.client
from unittest.mock import MagicMock, patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.odoo_connection import (
    OdooConnection,
    OdooConnectionError,
    OdooValidationFault,
)


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
        assert "lang" not in passed_kwargs.get("context", {})

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

        # faultCode 1 = application error; codes 2/4 are Odoo's business
        # classes and surface without the "Odoo error" label.
        fault = xmlrpc.client.Fault(1, "Some unrelated failure")
        mock_proxy.execute_kw.side_effect = fault

        with pytest.raises(OdooConnectionError, match="Odoo error"):
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


class TestInvalidLangAttribution:
    """A caller-supplied bad lang must not disable ODOO_MCP_LOCALE process-wide."""

    def test_caller_lang_failure_is_not_retried(self, config_with_locale):
        """Retried without the lang, a write would land in the default language."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)
        mock_proxy.execute_kw.side_effect = [
            xmlrpc.client.Fault(2, "Invalid language code: xx_XX"),
            True,
        ]

        with pytest.raises(OdooValidationFault, match="Language .xx_XX. is not installed in Odoo"):
            conn.execute_kw(
                "product.template",
                "write",
                [[1], {"name": "Chaise"}],
                {"context": {"lang": "xx_XX"}},
            )
        assert mock_proxy.execute_kw.call_count == 1
        assert conn.config.locale == "es_ES", "the configured locale was not at fault"

    def test_configured_locale_failure_still_disables_it(self, config_with_locale):
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)
        mock_proxy.execute_kw.side_effect = [
            xmlrpc.client.Fault(2, "Invalid language code: es_ES"),
            [{"id": 1}],
        ]

        result = conn.execute_kw("res.partner", "search_read", [[]], {})

        assert result == [{"id": 1}]
        assert conn.config.locale is None

    def test_caller_lang_equal_to_the_locale_is_not_retried(self, config_with_locale):
        """The caller asked for es_ES; a retry would write the default language."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)
        mock_proxy.execute_kw.side_effect = [xmlrpc.client.Fault(2, "Invalid language code: es_ES")]

        with pytest.raises(OdooValidationFault, match="Language .es_ES. is not installed in Odoo"):
            conn.execute_kw(
                "res.partner", "write", [[1], {"name": "x"}], {"context": {"lang": "es_ES"}}
            )
        assert mock_proxy.execute_kw.call_count == 1

    def test_injected_locale_is_retried_after_another_call_disabled_it(self, config_with_locale):
        """Two calls in flight both injected es_ES; the first to fail disables it."""
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)

        def first_call(*args, **kwargs):
            conn.config.locale = None  # the other call got there first
            raise xmlrpc.client.Fault(2, "Invalid language code: es_ES")

        calls = []

        def proxy_call(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return first_call()
            return [{"id": 1}]

        mock_proxy.execute_kw.side_effect = proxy_call

        assert conn.execute_kw("res.partner", "search_read", [[]], {}) == [{"id": 1}]
        assert len(calls) == 2

    def test_the_retry_keeps_the_call_unscoped(self, config_with_locale):
        """The read of the user's companies must stay unscoped after the locale retry."""
        config_with_locale.allowed_companies = [1, 9]
        conn = OdooConnection(config_with_locale)
        mock_proxy = _make_connected(conn)
        mock_proxy.execute_kw.side_effect = [
            xmlrpc.client.Fault(2, "Invalid language code: es_ES"),
            [{"id": 1, "company_ids": [1]}],
        ]

        conn.execute_kw("res.users", "read", [[1], ["company_ids"]], {}, scoped=False)

        retry_kwargs = mock_proxy.execute_kw.call_args_list[1].args[-1]
        assert "allowed_company_ids" not in retry_kwargs.get("context", {})
