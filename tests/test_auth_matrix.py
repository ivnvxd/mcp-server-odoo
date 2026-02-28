"""Auth matrix integration tests — covers all credential combinations.

Tests every auth scenario from docs/auth-test-matrix.md:
- Standard mode (S1–S7): API key and/or user/pass, with/without DB
- YOLO read (Y1–Y6): Various credential combos, writes blocked
- YOLO full (F1–F6): Same as read, writes allowed

Config validation tests (S7, Y6, F6) are pure unit tests — no server needed.
Integration tests require a live Odoo server and are auto-skipped otherwise.
"""

import os
import time
from dataclasses import dataclass
from typing import Optional

import pytest

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection

from .conftest import ODOO_SERVER_AVAILABLE


@pytest.fixture(autouse=True)
def _rate_limit_delay():
    """Small delay between tests to avoid overwhelming Odoo with connections."""
    yield
    time.sleep(0.1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Sentinel values — resolved to env vars at test time via AuthScenario.resolve().
# This avoids import-order issues with .env loading.
ENV_URL = "ENV_URL"
ENV_API_KEY = "ENV_API_KEY"
ENV_USER = "ENV_USER"
ENV_PASSWORD = "ENV_PASSWORD"
ENV_DB = "ENV_DB"

_ENV_RESOLVERS = {
    ENV_URL: lambda: os.getenv("ODOO_URL", "http://localhost:8069"),
    ENV_API_KEY: lambda: os.getenv("ODOO_API_KEY"),
    ENV_USER: lambda: os.getenv("ODOO_USER", "admin"),
    ENV_PASSWORD: lambda: os.getenv("ODOO_PASSWORD", "admin"),
    ENV_DB: lambda: os.getenv("ODOO_DB"),
}


def _resolve(value: Optional[str]) -> Optional[str]:
    """Resolve sentinel to actual env value, or pass through None."""
    if value in _ENV_RESOLVERS:
        return _ENV_RESOLVERS[value]()
    return value


@dataclass
class AuthScenario:
    """A single auth configuration to test.

    Fields use sentinel values (ENV_API_KEY etc.) that are resolved
    to actual env vars at test time, after .env is loaded by conftest.
    """

    id: str
    api_key: Optional[str]
    username: Optional[str]
    password: Optional[str]
    database: Optional[str]
    yolo_mode: str = "off"

    def make_config(self) -> OdooConfig:
        return OdooConfig(
            url=_resolve(ENV_URL),
            api_key=_resolve(self.api_key),
            username=_resolve(self.username),
            password=_resolve(self.password),
            database=_resolve(self.database),
            yolo_mode=self.yolo_mode,
        )

    def skip_if_missing_creds(self):
        """Skip test if required env vars are missing."""
        if self.api_key == ENV_API_KEY and not _resolve(ENV_API_KEY):
            pytest.skip("ODOO_API_KEY not set")
        if self.database == ENV_DB and not _resolve(ENV_DB):
            pytest.skip("ODOO_DB not set")


def _connect_and_auth(config: OdooConfig) -> OdooConnection:
    """Create connection, connect, authenticate. Caller must disconnect."""
    conn = OdooConnection(config)
    conn.connect()
    conn.authenticate()
    return conn


def _verify_read(conn: OdooConnection):
    """Verify read works — search res.partner."""
    results = conn.search_read("res.partner", [], ["name"], limit=1)
    assert len(results) >= 1
    assert "name" in results[0]


def _verify_db_autodetect(scenario: AuthScenario, conn: OdooConnection):
    """Verify DB was auto-detected when not explicitly provided."""
    if scenario.database is None:
        assert conn.database, "DB should be auto-detected when not provided"
    else:
        assert conn.database is not None


def _verify_write_cycle(conn: OdooConnection):
    """Create and delete a res.company record."""
    from uuid import uuid4

    unique_name = f"Auth Matrix Test {uuid4().hex[:8]}"
    record_id = conn.create("res.company", {"name": unique_name})
    assert isinstance(record_id, int)
    assert record_id > 0
    conn.unlink("res.company", [record_id])


def _verify_access_control_read(config: OdooConfig, database: str):
    """Verify AccessController can list models (standard mode)."""
    controller = AccessController(config, database=database)
    models = controller.get_enabled_models()
    assert isinstance(models, list)
    assert len(models) > 0


# ---------------------------------------------------------------------------
# Config validation tests (no server needed)
# ---------------------------------------------------------------------------


class TestAuthConfigValidation:
    """S7, Y6, F6: Missing credentials should fail at config creation."""

    def test_s7_standard_no_creds_fails(self):
        """Standard mode with no auth at all → ValueError."""
        with pytest.raises(ValueError, match="Authentication required"):
            OdooConfig(url="http://localhost:8069", yolo_mode="off")

    def test_y6_yolo_read_no_creds_fails(self):
        """YOLO read with no creds → ValueError."""
        with pytest.raises(ValueError, match="YOLO mode requires"):
            OdooConfig(url="http://localhost:8069", yolo_mode="read")

    def test_f6_yolo_full_no_creds_fails(self):
        """YOLO full with no creds → ValueError."""
        with pytest.raises(ValueError, match="YOLO mode requires"):
            OdooConfig(url="http://localhost:8069", yolo_mode="true")

    def test_standard_api_key_only_valid(self):
        """API key without user/pass is valid in standard mode."""
        config = OdooConfig(url="http://localhost:8069", api_key="some_key", yolo_mode="off")
        assert config.uses_api_key
        assert not config.uses_credentials

    def test_standard_user_pass_only_valid(self):
        """User/pass without API key is valid in standard mode."""
        config = OdooConfig(
            url="http://localhost:8069", username="admin", password="admin", yolo_mode="off"
        )
        assert not config.uses_api_key
        assert config.uses_credentials

    def test_yolo_requires_username_with_api_key(self):
        """YOLO mode needs username even with API key."""
        with pytest.raises(ValueError, match="YOLO mode requires"):
            OdooConfig(url="http://localhost:8069", api_key="some_key", yolo_mode="read")

    def test_yolo_api_key_plus_username_valid(self):
        """YOLO mode with API key + username (no password) is valid."""
        config = OdooConfig(
            url="http://localhost:8069", api_key="some_key", username="admin", yolo_mode="read"
        )
        assert config.uses_api_key
        assert config.username == "admin"


# ---------------------------------------------------------------------------
# Standard mode integration tests
# ---------------------------------------------------------------------------

STANDARD_SCENARIOS = [
    pytest.param(
        AuthScenario("S1", api_key=ENV_API_KEY, username=None, password=None, database=None),
        id="S1-apikey-nodb",
    ),
    pytest.param(
        AuthScenario("S2", api_key=ENV_API_KEY, username=None, password=None, database=ENV_DB),
        id="S2-apikey-db",
    ),
    pytest.param(
        AuthScenario(
            "S3", api_key=ENV_API_KEY, username=ENV_USER, password=ENV_PASSWORD, database=None
        ),
        id="S3-apikey-userpass-nodb",
    ),
    pytest.param(
        AuthScenario(
            "S4",
            api_key=ENV_API_KEY,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=ENV_DB,
        ),
        id="S4-apikey-userpass-db",
    ),
    pytest.param(
        AuthScenario("S5", api_key=None, username=ENV_USER, password=ENV_PASSWORD, database=None),
        id="S5-userpass-nodb",
    ),
    pytest.param(
        AuthScenario("S6", api_key=None, username=ENV_USER, password=ENV_PASSWORD, database=ENV_DB),
        id="S6-userpass-db",
    ),
]


@pytest.mark.skipif(not ODOO_SERVER_AVAILABLE, reason="Odoo server not available")
@pytest.mark.mcp
class TestStandardAuthMatrix:
    """S1–S6: Standard mode auth scenarios against live Odoo + MCP module."""

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS)
    def test_connect_and_read(self, scenario: AuthScenario):
        """Connect, authenticate, and read res.partner."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            assert conn.is_authenticated
            _verify_db_autodetect(scenario, conn)
            _verify_read(conn)
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS)
    def test_access_control(self, scenario: AuthScenario):
        """Verify AccessController can list models."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            _verify_access_control_read(config, conn.database)
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS)
    def test_write_cycle(self, scenario: AuthScenario):
        """Create + delete res.company — all S1–S6 should support writes."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            _verify_write_cycle(conn)
        finally:
            conn.disconnect()


# ---------------------------------------------------------------------------
# Standard mode — restricted operations
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ODOO_SERVER_AVAILABLE, reason="Odoo server not available")
@pytest.mark.mcp
class TestStandardRestrictedOps:
    """Standard mode: verify MCP module denies restricted operations.

    Tests the exact restricted ops from docs/auth-test-matrix.md:
    - create on res.partner → denied (create=false in MCP config)
    - unlink on res.country → denied (unlink=false in MCP config)
    - read on res.users → denied (read=false / model not enabled)
    """

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS)
    def test_create_denied_on_res_partner(self, scenario: AuthScenario):
        """create_record(res.partner) should be denied."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            controller = AccessController(config, database=conn.database)
            allowed, msg = controller.check_operation_allowed("res.partner", "create")
            assert allowed is False, f"Expected create on res.partner denied, got allowed: {msg}"
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS)
    def test_unlink_denied_on_res_country(self, scenario: AuthScenario):
        """delete_record(res.country) should be denied."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            controller = AccessController(config, database=conn.database)
            allowed, msg = controller.check_operation_allowed("res.country", "unlink")
            assert allowed is False, f"Expected unlink on res.country denied, got allowed: {msg}"
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS)
    def test_read_denied_on_res_users(self, scenario: AuthScenario):
        """search_records(res.users) should be denied."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            controller = AccessController(config, database=conn.database)
            allowed, msg = controller.check_operation_allowed("res.users", "read")
            assert allowed is False, f"Expected read on res.users denied, got allowed: {msg}"
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", STANDARD_SCENARIOS)
    def test_validate_raises_on_denied_operation(self, scenario: AuthScenario):
        """validate_model_access() should raise AccessControlError on denied ops."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            controller = AccessController(config, database=conn.database)
            with pytest.raises(AccessControlError):
                controller.validate_model_access("res.partner", "create")
        finally:
            conn.disconnect()


# ---------------------------------------------------------------------------
# YOLO read mode integration tests
# ---------------------------------------------------------------------------

YOLO_READ_SCENARIOS = [
    pytest.param(
        AuthScenario(
            "Y1",
            api_key=None,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=None,
            yolo_mode="read",
        ),
        id="Y1-userpass-nodb",
    ),
    pytest.param(
        AuthScenario(
            "Y2",
            api_key=None,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=ENV_DB,
            yolo_mode="read",
        ),
        id="Y2-userpass-db",
    ),
    pytest.param(
        AuthScenario(
            "Y3",
            api_key=ENV_API_KEY,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=None,
            yolo_mode="read",
        ),
        id="Y3-apikey-userpass-nodb",
    ),
    pytest.param(
        AuthScenario(
            "Y4",
            api_key=ENV_API_KEY,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=ENV_DB,
            yolo_mode="read",
        ),
        id="Y4-apikey-userpass-db",
    ),
    pytest.param(
        AuthScenario(
            "Y5",
            api_key=ENV_API_KEY,
            username=ENV_USER,
            password=None,
            database=None,
            yolo_mode="read",
        ),
        id="Y5-apikey-user-nodb",
    ),
]


@pytest.mark.skipif(not ODOO_SERVER_AVAILABLE, reason="Odoo server not available")
@pytest.mark.yolo
class TestYoloReadAuthMatrix:
    """Y1–Y5: YOLO read mode auth scenarios."""

    @pytest.mark.parametrize("scenario", YOLO_READ_SCENARIOS)
    def test_connect_and_read(self, scenario: AuthScenario):
        """Connect, authenticate, and read."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            assert conn.is_authenticated
            _verify_db_autodetect(scenario, conn)
            _verify_read(conn)
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", YOLO_READ_SCENARIOS)
    def test_access_control_bypassed(self, scenario: AuthScenario):
        """In YOLO mode, all models should be enabled."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        controller = AccessController(config)
        assert controller.is_model_enabled("res.partner")
        assert controller.is_model_enabled("any.random.model")

    @pytest.mark.parametrize("scenario", YOLO_READ_SCENARIOS)
    def test_writes_blocked(self, scenario: AuthScenario):
        """Write operations should be blocked in read-only YOLO mode.

        Connects to live Odoo to prove the scenario works for reads,
        then verifies writes are denied by the access control layer.
        Every credential combination is tested because each may take
        a different auth path (API-key-as-password vs password).
        """
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            # Reads work
            _verify_read(conn)

            # Writes blocked by access control
            controller = AccessController(config)
            for op in ("create", "write", "unlink"):
                allowed, msg = controller.check_operation_allowed("res.company", op)
                assert allowed is False, f"Expected {op} blocked in read mode"

            # validate_model_access raises on denied writes
            with pytest.raises(AccessControlError, match="not allowed"):
                controller.validate_model_access("res.company", "create")

            # Read operations are still allowed
            assert controller.check_operation_allowed("res.company", "read")[0] is True
        finally:
            conn.disconnect()


# ---------------------------------------------------------------------------
# YOLO full mode integration tests
# ---------------------------------------------------------------------------

YOLO_FULL_SCENARIOS = [
    pytest.param(
        AuthScenario(
            "F1",
            api_key=None,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=None,
            yolo_mode="true",
        ),
        id="F1-userpass-nodb",
    ),
    pytest.param(
        AuthScenario(
            "F2",
            api_key=None,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=ENV_DB,
            yolo_mode="true",
        ),
        id="F2-userpass-db",
    ),
    pytest.param(
        AuthScenario(
            "F3",
            api_key=ENV_API_KEY,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=None,
            yolo_mode="true",
        ),
        id="F3-apikey-userpass-nodb",
    ),
    pytest.param(
        AuthScenario(
            "F4",
            api_key=ENV_API_KEY,
            username=ENV_USER,
            password=ENV_PASSWORD,
            database=ENV_DB,
            yolo_mode="true",
        ),
        id="F4-apikey-userpass-db",
    ),
    pytest.param(
        AuthScenario(
            "F5",
            api_key=ENV_API_KEY,
            username=ENV_USER,
            password=None,
            database=None,
            yolo_mode="true",
        ),
        id="F5-apikey-user-nodb",
    ),
]


@pytest.mark.skipif(not ODOO_SERVER_AVAILABLE, reason="Odoo server not available")
@pytest.mark.yolo
class TestYoloFullAuthMatrix:
    """F1–F5: YOLO full mode auth scenarios."""

    @pytest.mark.parametrize("scenario", YOLO_FULL_SCENARIOS)
    def test_connect_and_read(self, scenario: AuthScenario):
        """Connect, authenticate, and read."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            assert conn.is_authenticated
            _verify_db_autodetect(scenario, conn)
            _verify_read(conn)
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", YOLO_FULL_SCENARIOS)
    def test_write_cycle(self, scenario: AuthScenario):
        """Create + delete res.company — writes should be allowed."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            _verify_write_cycle(conn)
        finally:
            conn.disconnect()

    @pytest.mark.parametrize("scenario", YOLO_FULL_SCENARIOS)
    def test_access_control_allows_writes(self, scenario: AuthScenario):
        """In YOLO full mode, all operations should be allowed."""
        scenario.skip_if_missing_creds()
        config = scenario.make_config()
        conn = _connect_and_auth(config)
        try:
            controller = AccessController(config)
            for op in ("read", "create", "write", "unlink"):
                allowed, msg = controller.check_operation_allowed("res.company", op)
                assert allowed is True, f"Expected {op} allowed in full mode, got: {msg}"

            # validate_model_access should not raise
            controller.validate_model_access("res.company", "create")
            controller.validate_model_access("res.company", "unlink")
        finally:
            conn.disconnect()
