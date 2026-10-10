"""Tests for MCPServer server foundation and lifecycle.

This module tests the basic server structure, initialization,
lifecycle management, and connection to Odoo.
"""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mcp import Client
from starlette.testclient import TestClient

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnectionError, OdooUnreachableError
from mcp_server_odoo.server import SERVER_VERSION, OdooMCPServer


class _FakeConnection:
    """Stands in for OdooConnection: ``outcome`` decides what connect() does."""

    def __init__(self, config=None, performance_manager=None):
        self.outcome = None  # None connects; an exception instance is raised
        self.auth_outcome = None
        self.companies_outcome = None
        self.disconnect_outcome = None
        self.is_connected = False
        self.is_authenticated = False
        self.database = "test_db"
        self.auth_method = "api_key"
        self.rpc_transport = "xmlrpc"
        self.uid = 2
        self.connect_calls = 0
        self.authenticate_calls = 0
        self.disconnect_calls = 0
        self.read = Mock(return_value=[])
        self.search_read = Mock(return_value=[])

    def connect(self):
        self.connect_calls += 1
        if self.outcome is not None:
            raise self.outcome
        self.is_connected = True

    def authenticate(self, database=None):
        self.authenticate_calls += 1
        if self.auth_outcome is not None:
            raise self.auth_outcome
        self.is_authenticated = True

    def check_allowed_companies(self):
        if self.companies_outcome is not None:
            raise self.companies_outcome

    def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_outcome is not None:
            raise self.disconnect_outcome
        self.is_connected = self.is_authenticated = False


class TestServerFoundation:
    """Test the basic MCPServer server foundation."""

    @pytest.fixture
    def valid_config(self):
        """Create a valid test configuration."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="test_api_key_12345",
            database="test_db",
            log_level="INFO",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def server_with_mock_connection(self, valid_config):
        """Create a server whose OdooConnection is a _FakeConnection."""
        fake = _FakeConnection()
        with patch("mcp_server_odoo.server.OdooConnection", return_value=fake) as conn_cls:
            server = OdooMCPServer(valid_config)
            server._mock_connection_class = conn_cls
            server._mock_connection = fake
            yield server

    def test_server_initialization(self, valid_config):
        """Test basic server initialization."""
        server = OdooMCPServer(valid_config)

        assert server.config == valid_config
        # The connection object exists from the start; it connects on startup
        # or on the first request
        assert server.connection is not None
        assert not server.connection.is_connected
        assert server.app is not None
        assert server.app.name == "odoo-mcp-server"

    def test_server_initialization_with_env_config(self, monkeypatch, tmp_path):
        """Test server initialization loading config from environment."""
        # Reset config singleton first
        from mcp_server_odoo.config import reset_config

        reset_config()

        # Set up environment variables
        monkeypatch.setenv("ODOO_URL", "http://test.odoo.com")
        monkeypatch.setenv("ODOO_API_KEY", "env_test_key")
        monkeypatch.setenv("ODOO_DB", "env_test_db")

        try:
            # Create server without explicit config
            server = OdooMCPServer()

            assert server.config.url == "http://test.odoo.com"
            assert server.config.api_key == "env_test_key"
            assert server.config.database == "env_test_db"
        finally:
            # Reset config for other tests
            reset_config()

    def test_server_version(self):
        """Test server version is a valid semver string."""
        parts = SERVER_VERSION.split(".")
        assert len(parts) == 3, f"Expected semver format x.y.z, got {SERVER_VERSION}"
        assert all(p.isdigit() for p in parts), (
            f"Expected numeric semver parts, got {SERVER_VERSION}"
        )

    @pytest.mark.asyncio
    async def test_ensure_connected_success(self, server_with_mock_connection):
        """ensure_connected connects once and updates the access controller."""
        server = server_with_mock_connection
        fake = server._mock_connection
        fake.database = "resolved_db"
        fake.auth_method = "password"

        await server.ensure_connected()

        assert server._mock_connection_class.call_count == 1
        assert "performance_manager" in server._mock_connection_class.call_args[1]
        assert fake.connect_calls == 1
        assert server.connection.is_authenticated
        # The controller follows the EFFECTIVE database and auth method (the
        # API key may have fallen back to the password)
        assert server.access_controller.database == "resolved_db"
        assert server.access_controller.auth_method == "password"

        await server.ensure_connected()
        assert fake.connect_calls == 1, "an authenticated connection is reused"

    @pytest.mark.asyncio
    async def test_ensure_connected_failure(self, server_with_mock_connection):
        """A configuration error propagates."""
        server = server_with_mock_connection
        server._mock_connection.outcome = OdooConnectionError("Connection failed")

        with pytest.raises(OdooConnectionError, match="Connection failed"):
            await server.ensure_connected()

    @pytest.mark.asyncio
    async def test_cleanup_connection(self, server_with_mock_connection):
        """Cleanup disconnects but keeps the objects the handlers hold."""
        server = server_with_mock_connection
        await server.ensure_connected()

        server._cleanup_connection()

        assert server._mock_connection.disconnect_calls == 1
        assert server.connection is server._mock_connection
        assert server.access_controller is not None

    def test_cleanup_connection_without_connection(self, server_with_mock_connection):
        """Test cleanup when the connection never connected."""
        server = server_with_mock_connection

        server._cleanup_connection()

        assert server._mock_connection.disconnect_calls == 0

    @pytest.mark.asyncio
    async def test_cleanup_connection_with_error(self, server_with_mock_connection):
        """Test cleanup when disconnect raises an error (it is logged)."""
        server = server_with_mock_connection
        await server.ensure_connected()
        server._mock_connection.disconnect_outcome = Exception("Disconnect failed")

        server._cleanup_connection()

        assert server._mock_connection.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_run_stdio_success(self, server_with_mock_connection):
        """run_stdio connects before the transport and disconnects at the end."""
        server = server_with_mock_connection

        # Make run_stdio_async invoke the lifespan like the real MCPServer does
        async def mock_run_with_lifespan():
            async with server._odoo_lifespan(server.app):
                pass

        server.app.run_stdio_async = mock_run_with_lifespan
        await server.run_stdio()

        assert server._mock_connection.connect_calls == 1
        assert server._mock_connection.authenticate_calls == 1
        assert server._mock_connection.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_run_stdio_configuration_error_stops_startup(self, server_with_mock_connection):
        """A configuration error fails before the transport starts."""
        server = server_with_mock_connection
        server._mock_connection.outcome = OdooConnectionError("Failed to connect")
        server.app.run_stdio_async = AsyncMock()

        with pytest.raises(OdooConnectionError, match="Failed to connect"):
            await server.run_stdio()

        server.app.run_stdio_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_stdio_serves_while_odoo_is_unreachable(self, server_with_mock_connection):
        server = server_with_mock_connection
        server._mock_connection.outcome = OdooUnreachableError("Cannot reach Odoo")
        server.app.run_stdio_async = AsyncMock()

        await server.run_stdio()  # must not raise

        server.app.run_stdio_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_stdio_keyboard_interrupt(self, server_with_mock_connection):
        """An interrupt while serving still tears the connection down."""
        server = server_with_mock_connection

        # Real MCPServer raises from inside the lifespan (interrupt happens
        # while serving), so its finally-cleanup must run.
        async def mock_run_interrupted():
            async with server._odoo_lifespan(server.app):
                raise KeyboardInterrupt

        server.app.run_stdio_async = mock_run_interrupted
        # Should not raise (handled gracefully)
        await server.run_stdio()

        assert server._mock_connection.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_run_stdio_keyboard_interrupt_before_lifespan(self, server_with_mock_connection):
        """An interrupt before the lifespan is entered exits gracefully; the
        eagerly-connected connection stays set (process exits right after)."""
        server = server_with_mock_connection
        server.app.run_stdio_async = AsyncMock(side_effect=KeyboardInterrupt)

        await server.run_stdio()  # must not raise

        assert server.connection is server._mock_connection

    @pytest.mark.asyncio
    async def test_lifespan_setup_and_teardown(self, server_with_mock_connection):
        """The lifespan connects on entry and disconnects on exit."""
        server = server_with_mock_connection

        async with server._odoo_lifespan(server.app) as state:
            assert server._mock_connection.is_authenticated
            assert state == {}

        assert server._mock_connection.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_lifespan_cleanup_on_setup_failure(self, server_with_mock_connection):
        """A refused login stops startup, and the opened connection is closed."""
        server = server_with_mock_connection
        server._mock_connection.auth_outcome = OdooConnectionError("Auth failed")

        with pytest.raises(OdooConnectionError, match="Auth failed"):
            async with server._odoo_lifespan(server.app):
                pass

        assert server._mock_connection.disconnect_calls == 1

    @pytest.mark.asyncio
    async def test_lifespan_teardown_exception_swallowed(self, server_with_mock_connection):
        """Test that lifespan teardown exceptions are swallowed gracefully."""
        server = server_with_mock_connection
        server._mock_connection.disconnect_outcome = RuntimeError("cleanup boom")

        # Should not raise — _cleanup_connection swallows the error
        async with server._odoo_lifespan(server.app):
            pass

        assert server._mock_connection.disconnect_calls == 1

    def test_get_model_names_returns_list(self, server_with_mock_connection):
        """Test _get_model_names returns model name strings."""
        server = server_with_mock_connection
        server.access_controller = Mock()
        server.access_controller.get_enabled_models.return_value = [
            {"model": "res.partner", "name": "Contact"},
            {"model": "sale.order", "name": "Sales Order"},
        ]

        assert server._get_model_names() == ["res.partner", "sale.order"]

    def test_get_model_names_exception_returns_empty(self, server_with_mock_connection):
        """Test _get_model_names returns empty list on exception."""
        server = server_with_mock_connection
        server.access_controller = Mock()
        server.access_controller.get_enabled_models.side_effect = RuntimeError("boom")

        assert server._get_model_names() == []

    def test_get_model_names_yolo_mode_fallback(self, server_with_mock_connection):
        """Test _get_model_names queries ir.model when get_enabled_models returns []."""
        server = server_with_mock_connection
        server.access_controller = Mock()
        server.access_controller.get_enabled_models.return_value = []  # YOLO mode returns []
        server._mock_connection.is_authenticated = True
        server._mock_connection.search_read.return_value = [
            {"model": "res.partner"},
            {"model": "sale.order"},
        ]

        assert server._get_model_names() == ["res.partner", "sale.order"]
        server._mock_connection.search_read.assert_called_once_with(
            "ir.model", [], ["model"], limit=200
        )

    @pytest.mark.asyncio
    async def test_completion_handler_partial_match(self, valid_config):
        """Test that the registered completion handler filters by partial match."""
        import mcp.types as types

        server = OdooMCPServer(valid_config)
        server.access_controller = Mock()
        server.access_controller.get_enabled_models.return_value = [
            {"model": "res.partner"},
            {"model": "res.users"},
            {"model": "sale.order"},
        ]

        # Ask through the in-memory MCP client; the lifespan must not dial Odoo
        with patch.object(OdooMCPServer, "ensure_connected", new=AsyncMock()):
            async with Client(server.app, mode="legacy") as client:
                result = await client.complete(
                    ref=types.PromptReference(type="ref/prompt", name="test"),
                    argument={"name": "model", "value": "res."},
                )
        values = result.completion.values
        assert set(values) == {"res.partner", "res.users"}
        assert "sale.order" not in values

    @pytest.mark.asyncio
    async def test_completion_handler_cap_at_20(self, valid_config):
        """Test that the registered completion handler caps results at 20."""
        import mcp.types as types

        server = OdooMCPServer(valid_config)
        server.access_controller = Mock()
        server.access_controller.get_enabled_models.return_value = [
            {"model": f"model.{i}"} for i in range(25)
        ]

        with patch.object(OdooMCPServer, "ensure_connected", new=AsyncMock()):
            async with Client(server.app, mode="legacy") as client:
                result = await client.complete(
                    ref=types.PromptReference(type="ref/prompt", name="test"),
                    argument={"name": "model", "value": ""},
                )
        values = result.completion.values
        assert len(values) == 20


class TestDynamicInstructions:
    """Dynamic initialize.instructions applied after a connect.

    stdio freezes the instructions when the transport starts, so run_stdio
    connects before that; a later connect refreshes them for new sessions.
    Startup never fails on personalization.
    """

    ADMIN_USER = {
        "id": 2,
        "name": "Mitchell Admin",
        "login": "admin",
        "tz": "Europe/Brussels",
        "company_id": [1, "My Company"],
        "company_ids": [1],
    }

    @pytest.fixture
    def server_with_user_read(self):
        """Server whose fake connection serves the res.users context read."""
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key_12345",
            database="test_db",
        )
        fake = _FakeConnection()
        fake.read.return_value = [dict(self.ADMIN_USER)]
        with patch("mcp_server_odoo.server.OdooConnection", return_value=fake):
            server = OdooMCPServer(config)
            server._mock_connection = fake
            yield server

    @pytest.mark.asyncio
    async def test_run_stdio_applies_personalized_instructions(self, server_with_user_read):
        server = server_with_user_read
        static = server.app.instructions
        server.app.run_stdio_async = AsyncMock()

        await server.run_stdio()

        instructions = server.app.instructions
        assert instructions.startswith(static), "static description retained as first line"
        assert "You are connected to Odoo via MCP as:" in instructions
        assert "- User: Mitchell Admin (login: admin)" in instructions
        assert "- Timezone: Europe/Brussels" in instructions
        assert "- Active company: My Company (ID: 1)" in instructions
        assert "Datetime handling:" in instructions

    @pytest.mark.asyncio
    async def test_run_http_applies_personalized_instructions(self, server_with_user_read):
        server = server_with_user_read
        server.app.run_streamable_http_async = AsyncMock()

        await server.run_http()

        instructions = server.app.instructions
        assert "- User: Mitchell Admin (login: admin)" in instructions

    @pytest.mark.asyncio
    async def test_run_http_users_read_failure_falls_back_to_utc_guidance(
        self, server_with_user_read
    ):
        """The HTTP transport shares the stdio degradation behavior."""
        server = server_with_user_read
        static = server.app.instructions
        server._mock_connection.read.side_effect = Exception("res.users not MCP-enabled")
        server.app.run_streamable_http_async = AsyncMock()

        await server.run_http()

        instructions = server.app.instructions
        assert instructions.startswith(static)
        assert "Datetime handling:" in instructions
        assert "You are connected to Odoo via MCP as:" not in instructions

    @pytest.mark.asyncio
    async def test_users_read_failure_falls_back_to_utc_guidance(self, server_with_user_read):
        """Standard mode may gate res.users — instructions degrade, startup proceeds."""
        server = server_with_user_read
        static = server.app.instructions
        server._mock_connection.read.side_effect = Exception("res.users not MCP-enabled")
        server.app.run_stdio_async = AsyncMock()

        await server.run_stdio()

        instructions = server.app.instructions
        assert instructions.startswith(static)
        assert "Datetime handling:" in instructions
        assert "You are connected to Odoo via MCP as:" not in instructions

    @pytest.mark.asyncio
    async def test_apply_dynamic_instructions_idempotent(self, server_with_user_read):
        """Repeated application rebuilds from the static base — the context
        block must never compound when one instance runs more than once."""
        server = server_with_user_read
        static = server.app.instructions
        await server.ensure_connected()

        await server._apply_dynamic_instructions()
        await server._apply_dynamic_instructions()

        instructions = server.app.instructions
        assert instructions.startswith(static)
        assert instructions.count("You are connected to Odoo via MCP as:") == 1
        assert instructions.count("Datetime handling:") == 1

    @pytest.mark.asyncio
    async def test_connect_failure_keeps_static_instructions(self, server_with_user_read):
        """An unreachable Odoo never aborts startup; instructions stay static."""
        server = server_with_user_read
        static = server.app.instructions
        server._mock_connection.outcome = OdooUnreachableError("Odoo down")
        server.app.run_stdio_async = AsyncMock()

        await server.run_stdio()  # must not raise

        assert server.app.instructions == static


class TestUsageInstructions:
    """The usage block is part of the static instructions, from the registered tools."""

    def _server(self, **config_kwargs):
        config = OdooConfig(url="http://localhost:8069", database="test_db", **config_kwargs)
        with patch("mcp_server_odoo.server.OdooConnection", return_value=_FakeConnection()):
            return OdooMCPServer(config)

    def test_static_instructions_carry_the_usage_block(self):
        server = self._server(api_key="test_api_key_12345")

        instructions = server.app.instructions
        assert instructions == server._static_instructions
        assert instructions.startswith("MCP server for accessing and managing Odoo ERP data")
        assert "Usage guidance:" in instructions
        assert '[["id", "in", ids]]' in instructions
        assert "call_model_method" not in instructions

    def test_opt_in_method_calls_are_named(self):
        server = self._server(
            username="admin", password="admin", yolo_mode="true", enable_method_calls=True
        )

        assert "call_model_method" in server.app.instructions


class TestServerIntegration:
    """Integration tests with real .env configuration."""

    @pytest.mark.mcp
    def test_server_with_env_file(self, tmp_path):
        """Test server initialization with .env file in isolated environment."""
        # Import modules we need
        from mcp_server_odoo.config import load_config, reset_config

        # Store original working directory
        original_cwd = os.getcwd()

        # Create a test .env file in tmp directory
        env_file = tmp_path / ".env"
        env_file.write_text("""
ODOO_URL=http://localhost:8069
ODOO_API_KEY=test_integration_key
ODOO_DB=test_integration_db
ODOO_MCP_LOG_LEVEL=DEBUG
""")

        # patch.dict snapshots os.environ and rolls back everything on exit,
        # including keys that load_dotenv adds (monkeypatch.delenv on an
        # absent key registers nothing to restore, so those would leak)
        try:
            with patch.dict(os.environ):
                # Change to temp directory to isolate from project .env
                os.chdir(tmp_path)

                # Clear all environment variables that might interfere
                for key in [
                    "ODOO_URL",
                    "ODOO_API_KEY",
                    "ODOO_DB",
                    "ODOO_MCP_LOG_LEVEL",
                    "ODOO_USER",
                    "ODOO_PASSWORD",
                    "ODOO_YOLO",
                ]:
                    os.environ.pop(key, None)

                # Reset config singleton
                reset_config()

                # Load config explicitly from our test .env file
                # This ensures we're loading from the tmp directory's .env
                config = load_config(env_file)

                # Create server with the loaded config
                server = OdooMCPServer(config)

                assert server.config.url == "http://localhost:8069"
                assert server.config.api_key == "test_integration_key"
                assert server.config.database == "test_integration_db"
                assert server.config.log_level == "DEBUG"

        finally:
            os.chdir(original_cwd)
            reset_config()  # Reset again for other tests

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_real_odoo_connection(self):
        """Test with real Odoo connection using .env credentials.

        This test requires a running Odoo server with valid credentials
        in the .env file.
        """
        # Skip if no .env file exists
        if not Path(".env").exists():
            pytest.skip("No .env file found for integration test")

        # Import and reset config to ensure clean state
        from mcp_server_odoo.config import reset_config

        reset_config()

        # Load environment
        from dotenv import load_dotenv

        load_dotenv()

        # Check if required env vars are set
        if not os.getenv("ODOO_URL"):
            pytest.skip("ODOO_URL not set in environment")

        server = None
        try:
            # Create server with real config
            server = OdooMCPServer()

            # Test connection
            await server.ensure_connected()

            # If we get here, connection was successful
            assert server.connection.is_authenticated

            # Clean up
            server._cleanup_connection()

        except OdooConnectionError as e:
            # Connection errors are expected if Odoo is not running
            pytest.skip(f"Integration test skipped (Odoo not available): {e}")
        finally:
            # Always reset config for other tests
            reset_config()


class TestMainEntry:
    """Test the __main__ entry point."""

    def test_help_flag(self, capsys):
        """Test --help flag."""
        from mcp_server_odoo.__main__ import main

        # argparse raises SystemExit for --help
        try:
            exit_code = main(["--help"])
            assert exit_code == 0
        except SystemExit as e:
            assert e.code == 0

        captured = capsys.readouterr()
        # Help output goes to stdout by default from argparse
        help_output = captured.out or captured.err
        assert "Odoo MCP Server" in help_output
        assert "ODOO_URL" in help_output

    def test_version_flag(self, capsys):
        """Test --version flag."""
        from mcp_server_odoo.__main__ import main

        # argparse raises SystemExit for --version
        try:
            exit_code = main(["--version"])
            assert exit_code == 0
        except SystemExit as e:
            assert e.code == 0

        captured = capsys.readouterr()
        # Version output goes to stdout by default from argparse
        version_output = captured.out or captured.err
        assert f"odoo-mcp-server v{SERVER_VERSION}" in version_output

    def test_main_with_invalid_config(self, capsys, monkeypatch):
        """Test main with invalid configuration."""
        from mcp_server_odoo.__main__ import main

        # Set invalid config
        monkeypatch.setenv("ODOO_URL", "")  # Empty URL

        exit_code = main([])

        assert exit_code == 1

        captured = capsys.readouterr()
        assert "Configuration error" in captured.err

    def test_main_with_valid_config(self, monkeypatch):
        """Test main with valid configuration."""
        from mcp_server_odoo.__main__ import main

        # Set valid config
        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test_key")

        # Mock the server and its run_stdio method
        with patch("mcp_server_odoo.__main__.OdooMCPServer") as mock_server_class:
            mock_server = Mock()

            # Create a coroutine that completes immediately
            async def mock_run_stdio():
                pass

            mock_server.run_stdio = mock_run_stdio
            mock_server_class.return_value = mock_server

            # Mock asyncio.run to execute synchronously
            def mock_asyncio_run(coro):
                # Run the coroutine to completion
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            with patch("asyncio.run", side_effect=mock_asyncio_run):
                exit_code = main([])

                assert exit_code == 0
                mock_server_class.assert_called_once()

    def test_main_with_http_transport(self, monkeypatch):
        """Test main with streamable-http transport."""
        from mcp_server_odoo.__main__ import main

        monkeypatch.setenv("ODOO_URL", "http://localhost:8069")
        monkeypatch.setenv("ODOO_API_KEY", "test_key")
        # Pre-set so main()'s os.environ writes are captured by monkeypatch
        monkeypatch.setenv("ODOO_MCP_TRANSPORT", "stdio")
        monkeypatch.setenv("ODOO_MCP_HOST", "localhost")
        monkeypatch.setenv("ODOO_MCP_PORT", "8000")

        with patch("mcp_server_odoo.__main__.OdooMCPServer") as mock_server_class:
            mock_config = Mock()
            mock_config.transport = "streamable-http"
            mock_config.host = "localhost"
            mock_config.port = 8000

            mock_server = Mock()

            async def mock_run_http(**kwargs):
                pass

            mock_server.run_http = mock_run_http
            mock_server_class.return_value = mock_server

            def mock_asyncio_run(coro):
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(coro)
                finally:
                    loop.close()

            with (
                patch("mcp_server_odoo.__main__.load_config", return_value=mock_config),
                patch("asyncio.run", side_effect=mock_asyncio_run),
            ):
                exit_code = main(["--transport", "streamable-http"])
                assert exit_code == 0


class TestFastMCPApp:
    """Test the MCPServer app configuration."""

    @pytest.fixture
    def valid_config(self):
        """Create a valid test configuration."""
        return OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="test_api_key_12345",
            database="test_db",
            log_level="INFO",
            default_limit=10,
            max_limit=100,
        )

    def test_fastmcp_app_creation(self, valid_config):
        """Test that MCPServer app is properly created."""
        server = OdooMCPServer(valid_config)

        assert server.app is not None
        assert server.app.name == "odoo-mcp-server"
        assert "Odoo ERP data" in server.app.instructions

    def test_health_route_registered(self, valid_config):
        """Test that /health custom route is registered in Starlette routes."""
        server = OdooMCPServer(valid_config)

        # Inspect the actual Starlette route table via the streamable HTTP app
        starlette_app = server.app.streamable_http_app()
        route_paths = [r.path for r in starlette_app.routes if hasattr(r, "path")]
        assert "/health" in route_paths

    def test_health_status_unhealthy_when_disconnected(self, valid_config):
        """Test health returns unhealthy when not connected."""
        server = OdooMCPServer(valid_config)

        health = server.get_health_status()
        assert health["status"] == "unhealthy"
        assert health["version"] == SERVER_VERSION
        assert health["connection"]["connected"] is False

    @pytest.mark.asyncio
    async def test_health_status_healthy_when_connected(self, valid_config):
        """Test health returns healthy when connected."""
        with patch("mcp_server_odoo.server.OdooConnection", return_value=_FakeConnection()):
            server = OdooMCPServer(valid_config)
            await server.ensure_connected()

            health = server.get_health_status()
            assert health["status"] == "healthy"
            assert health["connection"]["connected"] is True


class TestHttpExposureWarning:
    """Non-loopback HTTP binds must produce a loud security warning."""

    def _make_server(self, **config_overrides):
        kwargs = {
            "url": "http://localhost:8069",
            "api_key": "test_api_key_12345",
            "database": "test_db",
        }
        kwargs.update(config_overrides)
        return OdooMCPServer(OdooConfig(**kwargs))

    def test_warns_on_non_loopback_host(self):
        server = self._make_server()
        with patch("mcp_server_odoo.server.logger.warning") as mock_warning:
            server._warn_if_exposed("0.0.0.0")
        mock_warning.assert_called_once()
        message = mock_warning.call_args[0][0]
        assert "NO built-in" in message
        assert "authentication" in message

    def test_no_warning_on_loopback(self):
        server = self._make_server()
        with patch("mcp_server_odoo.server.logger.warning") as mock_warning:
            server._warn_if_exposed("localhost")
            server._warn_if_exposed("127.0.0.1")
        mock_warning.assert_not_called()

    def test_warning_escalates_in_yolo_full_mode(self):
        server = self._make_server(
            api_key=None, username="admin", password="admin", yolo_mode="true"
        )
        with patch("mcp_server_odoo.server.logger.warning") as mock_warning:
            server._warn_if_exposed("0.0.0.0")
        message = mock_warning.call_args[0][0]
        assert "YOLO FULL-ACCESS MODE" in message


class TestTransportSecurity:
    """Test transport security configuration for DNS rebinding protection."""

    @staticmethod
    def _post_with_foreign_host(server):
        """POST /mcp with a Host header no allowlist accepts; returns the response."""
        app = server.app.streamable_http_app(
            transport_security=server._transport_security, host=server.config.host
        )
        # The HTTP lifespan connects to Odoo; keep it offline
        with (
            patch.object(OdooMCPServer, "ensure_connected", new=AsyncMock()),
            TestClient(app) as client,
        ):
            return client.post(
                "/mcp",
                headers={
                    "Host": "evil.example",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )

    def test_loopback_bind_auto_enables_protection_when_allowed_hosts_is_empty(self):
        """Empty allowed_hosts does NOT mean "no host validation".

        _build_transport_security returns None, which hands the decision to
        the SDK — and the SDK auto-enables its loopback allowlist for a
        127.0.0.1/localhost/::1 bind. This is the claim `.env.example`, the
        README and _build_transport_security's own docstring all make, so it
        needs an executable copy.
        """
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=[],
        )
        server = OdooMCPServer(config)

        assert server._transport_security is None
        response = self._post_with_foreign_host(server)
        assert response.status_code == 421
        assert response.text == "Invalid Host header"

    @pytest.mark.parametrize("host", ["localhost", "0.0.0.0"])
    @pytest.mark.asyncio
    async def test_run_http_binds_config_host_and_port(self, host):
        """run_http() takes no host/port: the bind and the transport-security
        decision both follow config. The SDK decides the loopback default
        from the same ``host`` argument it binds, so both must be passed
        through untouched. Both hosts are exercised because a reintroduced
        ``host: str = "localhost"`` default is invisible when config.host is
        already localhost — the 0.0.0.0 case is what tells them apart.
        """
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            host=host,
            port=9000,
        )
        server = OdooMCPServer(config)

        server.app.run_streamable_http_async = AsyncMock()
        with patch.object(server, "_apply_dynamic_instructions", new=AsyncMock()):
            await server.run_http()

        kwargs = server.app.run_streamable_http_async.call_args.kwargs
        assert kwargs["host"] == host
        assert kwargs["port"] == 9000
        assert kwargs["transport_security"] is server._transport_security

    def test_non_loopback_bind_leaves_protection_off_when_allowed_hosts_is_empty(self):
        """The other half, and the reason the docs tell 0.0.0.0 deployments to
        set ODOO_MCP_ALLOWED_HOSTS: no Host/Origin validation runs at all.
        """
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            host="0.0.0.0",
            allowed_hosts=[],
        )
        server = OdooMCPServer(config)

        assert server._transport_security is None
        assert self._post_with_foreign_host(server).status_code != 421

    def test_transport_security_with_single_host(self):
        """Test transport security is configured with a single allowed host."""
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=["localhost"],
        )
        server = OdooMCPServer(config)

        # Server should be created successfully with transport security
        assert server.app is not None
        assert server.config.allowed_hosts == ["localhost"]

    def test_transport_security_with_multiple_hosts(self):
        """Test transport security with multiple allowed hosts."""
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=["localhost", "example.com", "odoo.local"],
        )
        server = OdooMCPServer(config)

        assert server.app is not None
        assert len(server.config.allowed_hosts) == 3

    def test_transport_security_host_with_port_preserved(self):
        """Test that hosts with ports are preserved as-is."""
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=["localhost:8000", "example.com"],
        )
        server = OdooMCPServer(config)

        # The server should handle both formats
        assert "localhost:8000" in server.config.allowed_hosts
        assert "example.com" in server.config.allowed_hosts

    def test_transport_security_builds_allowed_origins(self):
        """Test that allowed_origins are built from allowed_hosts."""
        from mcp.server.transport_security import TransportSecuritySettings

        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=["example.com"],
        )

        # Capture the TransportSecuritySettings that would be created
        with patch.object(TransportSecuritySettings, "__init__", return_value=None) as mock_init:
            # Create server - this will call TransportSecuritySettings
            OdooMCPServer(config)

            # Verify TransportSecuritySettings was called with correct params
            mock_init.assert_called_once()
            call_kwargs = mock_init.call_args[1]

            assert call_kwargs["enable_dns_rebinding_protection"] is True
            assert "example.com:*" in call_kwargs["allowed_hosts"]
            assert "http://example.com:*" in call_kwargs["allowed_origins"]
            assert "https://example.com:*" in call_kwargs["allowed_origins"]

    def test_transport_security_host_with_port_pins_the_port(self):
        """An entry that pins a port pins it for origins too — a ":*" origin
        would trust a page served from any OTHER port on the same hostname,
        making the Origin allowlist looser than the Host one."""
        from mcp.server.transport_security import TransportSecuritySettings

        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=["example.com:8080"],
        )

        with patch.object(TransportSecuritySettings, "__init__", return_value=None) as mock_init:
            OdooMCPServer(config)

            call_kwargs = mock_init.call_args[1]

            # Host with port should be preserved as-is (already has port)
            assert "example.com:8080" in call_kwargs["allowed_hosts"]
            assert call_kwargs["allowed_origins"] == [
                "http://example.com:8080",
                "https://example.com:8080",
            ]
            assert "http://example.com:*" not in call_kwargs["allowed_origins"]

    def test_transport_security_not_configured_when_empty(self):
        """Test we pass None as transport_security when allowed_hosts is empty."""
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=[],
        )

        with patch("mcp_server_odoo.server.MCPServer") as mock_fastmcp:
            mock_fastmcp.return_value = Mock()
            mock_fastmcp.return_value._tool_manager.list_tools.return_value = []
            OdooMCPServer(config)

            # Verify MCPServer was called with transport_security=None
            call_kwargs = mock_fastmcp.call_args[1]
            assert call_kwargs.get("transport_security") is None

    def test_build_transport_security_returns_none_without_hosts(self):
        """Empty allowed_hosts → None, leaving the SDK default (protection off)."""
        server = OdooMCPServer(
            OdooConfig(url="http://localhost:8069", api_key="k", allowed_hosts=[])
        )
        assert server._build_transport_security() is None

    def test_build_transport_security_settings_shape(self):
        """Configured hosts produce wildcard-port hosts and http/https origins;
        a host that already carries a port keeps it verbatim."""
        server = OdooMCPServer(
            OdooConfig(
                url="http://localhost:8069",
                api_key="k",
                allowed_hosts=["odoo.example.com", "localhost:9000"],
            )
        )
        settings = server._build_transport_security()

        assert settings is not None
        assert settings.enable_dns_rebinding_protection is True
        # A port-less entry matches any port AND the bare authority a proxy
        # sends on 80/443; host:port is preserved as-is.
        assert settings.allowed_hosts == [
            "odoo.example.com:*",
            "odoo.example.com",
            "localhost:9000",
        ]
        # origins mirror each host entry on both schemes, port included
        assert settings.allowed_origins == [
            "http://odoo.example.com:*",
            "https://odoo.example.com:*",
            "http://odoo.example.com",
            "https://odoo.example.com",
            "http://localhost:9000",
            "https://localhost:9000",
        ]


class TestSessionIdleTimeout:
    """ODOO_MCP_SESSION_IDLE_TIMEOUT reaches the streamable-http transport."""

    @staticmethod
    async def _run_http_kwargs(config):
        server = OdooMCPServer(config)
        server.app.run_streamable_http_async = AsyncMock()
        with patch.object(server, "_apply_dynamic_instructions", new=AsyncMock()):
            await server.run_http()
        return server.app.run_streamable_http_async.call_args.kwargs

    @pytest.mark.asyncio
    async def test_unset_timeout_means_sessions_never_expire(self):
        """mcp 2.x evicts sessions idle for 30 minutes by default; an unset
        ODOO_MCP_SESSION_IDLE_TIMEOUT must keep the documented "never expire"."""
        kwargs = await self._run_http_kwargs(OdooConfig(url="http://localhost:8069", api_key="k"))

        assert "session_idle_timeout" in kwargs
        assert kwargs["session_idle_timeout"] is None

    @pytest.mark.asyncio
    async def test_configured_timeout_is_passed_through(self):
        kwargs = await self._run_http_kwargs(
            OdooConfig(url="http://localhost:8069", api_key="k", session_idle_timeout=600)
        )

        assert kwargs["session_idle_timeout"] == 600


class TestAllowedHostsIPv6:
    """A naive split(":") mangles IPv6 both ways, producing an allowlist that
    rejects the very host the operator allowlisted."""

    def _settings(self, hosts):
        server = OdooMCPServer(
            OdooConfig(url="http://localhost:8069", api_key="k", allowed_hosts=hosts)
        )
        return server._build_transport_security()

    def test_bracketed_ipv6_with_port(self):
        s = self._settings(["[::1]:8000"])
        assert s.allowed_hosts == ["[::1]:8000"]
        assert s.allowed_origins == ["http://[::1]:8000", "https://[::1]:8000"]

    def test_bracketed_ipv6_without_port_gets_wildcard(self):
        s = self._settings(["[::1]"])
        assert s.allowed_hosts == ["[::1]:*", "[::1]"]
        assert s.allowed_origins == [
            "http://[::1]:*",
            "https://[::1]:*",
            "http://[::1]",
            "https://[::1]",
        ]

    def test_bare_ipv6_normalized_to_bracket_form(self):
        """Host headers and URL authorities carry IPv6 bracketed."""
        s = self._settings(["::1"])
        assert s.allowed_hosts == ["[::1]:*", "[::1]"]
        assert s.allowed_origins == [
            "http://[::1]:*",
            "https://[::1]:*",
            "http://[::1]",
            "https://[::1]",
        ]

    def test_bare_global_ipv6_normalized(self):
        s = self._settings(["2001:db8::1"])
        assert s.allowed_hosts == ["[2001:db8::1]:*", "[2001:db8::1]"]
        assert "http://[2001:db8::1]:*" in s.allowed_origins

    def test_ipv6_origin_actually_matches_a_real_header(self):
        """The SDK matches with startswith(base + ':') — the old 'http://[:*'
        pattern could never match a real bracketed origin."""
        from mcp.server.transport_security import TransportSecurityMiddleware

        mw = TransportSecurityMiddleware(self._settings(["[::1]:8000"]))
        assert mw._validate_host("[::1]:8000") is True
        assert mw._validate_origin("http://[::1]:8000") is True

    def test_ipv4_and_dns_unchanged(self):
        s = self._settings(["odoo.example.com", "localhost:9000"])
        assert s.allowed_hosts == ["odoo.example.com:*", "odoo.example.com", "localhost:9000"]


class TestAllowedHostsPortlessAuthority:
    """A browser and a reverse proxy omit the default port from Host and
    Origin entirely, and the SDK matches ':*' with startswith(base + ':') —
    so a port-less entry has to allow the bare authority too, or the
    documented 'odoo.example.com behind TLS' deployment rejects everything.
    """

    def _middleware(self, hosts):
        from mcp.server.transport_security import TransportSecurityMiddleware

        server = OdooMCPServer(
            OdooConfig(url="http://localhost:8069", api_key="k", allowed_hosts=hosts)
        )
        return TransportSecurityMiddleware(server._build_transport_security())

    def test_portless_host_header_accepted(self):
        mw = self._middleware(["odoo.example.com"])
        assert mw._validate_host("odoo.example.com") is True
        assert mw._validate_host("odoo.example.com:443") is True
        assert mw._validate_host("odoo.example.com:8000") is True

    def test_portless_origin_accepted(self):
        mw = self._middleware(["odoo.example.com"])
        assert mw._validate_origin("https://odoo.example.com") is True
        assert mw._validate_origin("https://odoo.example.com:443") is True

    def test_portless_ipv6_authority_accepted(self):
        mw = self._middleware(["::1"])
        assert mw._validate_host("[::1]") is True
        assert mw._validate_host("[::1]:8000") is True

    def test_other_hosts_still_rejected(self):
        mw = self._middleware(["odoo.example.com"])
        assert mw._validate_host("evil.example.com") is False
        assert mw._validate_host("odoo.example.com.evil.com") is False
        assert mw._validate_origin("https://evil.example.com") is False

    def test_explicit_port_entry_stays_exact(self):
        """An operator who names a port means that port."""
        mw = self._middleware(["localhost:9000"])
        assert mw._validate_host("localhost:9000") is True
        assert mw._validate_host("localhost") is False
        assert mw._validate_host("localhost:9001") is False


class TestOdooLifecycle:
    """The server serves while Odoo is unreachable and connects on demand.

    mcp 2.x enters the lifespan once per process; a failing lifespan stops the
    HTTP server. So only configuration and authentication errors may fail it.
    """

    @pytest.fixture
    def server(self):
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="test_db")
        fake = _FakeConnection()
        with (
            patch("mcp_server_odoo.server.OdooConnection", return_value=fake),
            patch(
                "mcp_server_odoo.server.build_user_context",
                return_value="You are connected to Odoo via MCP as:\n- User: Test",
            ),
        ):
            server = OdooMCPServer(config)
            server._fake = fake
            yield server

    @staticmethod
    def _http_app(server):
        return server.app.streamable_http_app(
            transport_security=server._transport_security, host=server.config.host
        )

    @pytest.mark.asyncio
    async def test_tools_exist_before_odoo_answers(self, server):
        server._fake.outcome = OdooUnreachableError("Cannot reach Odoo")

        tools = await server.app.list_tools()

        assert {"search_records", "get_record"} <= {tool.name for tool in tools}

    async def test_a_request_while_odoo_is_down_is_a_plain_tool_error(self, server, caplog):
        """The text reaches the client, without a traceback logged per call."""
        from mcp import Client

        server._fake.outcome = OdooUnreachableError("Cannot reach Odoo at http://localhost:8069")

        async with Client(server.app, mode="legacy") as client:
            result = await client.call_tool("list_models", {})
            with pytest.raises(Exception, match="Cannot reach Odoo"):
                await client.read_resource("odoo://res.partner/count")

        assert result.is_error
        text = result.content[0].text
        assert "Cannot reach Odoo" in text
        # The URL and port stay out of what a (remote HTTP) client sees
        assert "localhost:8069" not in text
        assert "unexpected exception" not in caplog.text

    def test_unreachable_odoo_keeps_http_up(self, server):
        server._fake.outcome = OdooUnreachableError("Cannot reach Odoo at http://localhost:8069")

        with TestClient(self._http_app(server)) as client:
            health = client.get("/health").json()

        assert health["status"] == "unhealthy"
        assert server._fake.connect_calls >= 1

    @pytest.mark.asyncio
    async def test_request_after_odoo_returns_connects(self, server, monkeypatch):
        server._fake.outcome = OdooUnreachableError("Cannot reach Odoo")
        clock = [1000.0]
        monkeypatch.setattr("mcp_server_odoo.server.time.monotonic", lambda: clock[0])

        async with server._odoo_lifespan(server.app):
            # Odoo comes back, but the backoff holds the next attempt
            server._fake.outcome = None
            attempts = server._fake.connect_calls
            with pytest.raises(OdooUnreachableError):
                await server.ensure_connected()
            assert server._fake.connect_calls == attempts

            clock[0] += 120
            # The tool asks the module for its enabled models; keep it offline
            server.access_controller.get_enabled_models = Mock(return_value=[])
            await server.app.call_tool("list_resource_templates", {})

            assert server._fake.is_authenticated
            assert server.get_health_status()["status"] == "healthy"
            assert "- User: Test" in server.app.instructions

    @pytest.mark.parametrize(
        "connect_error,auth_error",
        [
            (OdooConnectionError("Cannot list databases on this server. Set ODOO_DB"), None),
            (None, OdooConnectionError("Authentication failed")),
        ],
    )
    @pytest.mark.asyncio
    async def test_configuration_and_auth_errors_stop_startup(
        self, server, connect_error, auth_error
    ):
        server._fake.outcome = connect_error
        server._fake.auth_outcome = auth_error

        with pytest.raises(OdooConnectionError):
            async with server._odoo_lifespan(server.app):
                pass

    async def test_foreign_company_fails_every_request(self, server):
        """A bad ODOO_ALLOWED_COMPANIES is a configuration error: never left connected."""
        server._fake.companies_outcome = OdooConnectionError(
            "ODOO_ALLOWED_COMPANIES names companies the user cannot access: [9]."
        )

        for _ in range(2):
            with pytest.raises(OdooConnectionError, match="cannot access"):
                await server.ensure_connected()

        assert server._fake.authenticate_calls == 2
        assert server._fake.is_authenticated is False

    def test_auth_error_stops_the_http_server(self, server):
        server._fake.auth_outcome = OdooConnectionError("Authentication failed")

        with pytest.raises(OdooConnectionError, match="Authentication failed"):
            with TestClient(self._http_app(server)):
                pass

    @pytest.mark.asyncio
    async def test_lost_authentication_is_restored_in_place(self, server):
        """Handlers hold the connection object, so it is reauthenticated, never replaced."""
        await server.ensure_connected()
        connection = server.connection
        server._fake.is_authenticated = False
        server._fake.auth_method = "password"

        await server.ensure_connected()

        assert server.connection is connection
        assert server._fake.connect_calls == 1, "still connected: authenticate only"
        assert server._fake.authenticate_calls == 2
        assert server.access_controller.auth_method == "password"

    @pytest.mark.asyncio
    async def test_recovery_after_refused_first_login(self, server):
        server._fake.auth_outcome = OdooConnectionError("Authentication failed")
        with pytest.raises(OdooConnectionError):
            await server.ensure_connected()

        server._fake.auth_outcome = None
        await server.ensure_connected()

        assert server.get_health_status()["status"] == "healthy"
        assert {"search_records"} <= {tool.name for tool in await server.app.list_tools()}

    def test_http_requests_share_one_connection(self, server):
        """Issue #70: the connection outlives every request and session."""
        with TestClient(self._http_app(server)) as client:
            client.get("/health")
            client.get("/health")
            assert server._fake.connect_calls == 1
            assert server._fake.disconnect_calls == 0

        assert server._fake.disconnect_calls == 1
