"""Tests for FastMCP server foundation and lifecycle.

This module tests the basic server structure, initialization,
lifecycle management, and connection to Odoo.
"""

import asyncio
import contextlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnectionError
from mcp_server_odoo.server import SERVER_VERSION, OdooMCPServer


class TestServerFoundation:
    """Test the basic FastMCP server foundation."""

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
        """Create server with mocked connection."""
        with patch("mcp_server_odoo.server.OdooConnection") as mock_conn_class:
            # Mock the connection class
            mock_connection = Mock()
            mock_connection.connect = Mock()
            mock_connection.authenticate = Mock()
            mock_connection.disconnect = Mock()
            mock_conn_class.return_value = mock_connection

            server = OdooMCPServer(valid_config)
            server._mock_connection_class = mock_conn_class
            server._mock_connection = mock_connection

            yield server

    def test_server_initialization(self, valid_config):
        """Test basic server initialization."""
        server = OdooMCPServer(valid_config)

        assert server.config == valid_config
        assert server.connection is None  # Not connected until run
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

    def test_ensure_connection_success(self, server_with_mock_connection):
        """Test successful connection establishment."""
        server = server_with_mock_connection

        # Ensure connection
        server._ensure_connection()

        # Verify connection was created with performance manager
        assert server._mock_connection_class.call_count == 1
        call_args = server._mock_connection_class.call_args
        assert call_args[0][0] == server.config
        assert "performance_manager" in call_args[1]
        server._mock_connection.connect.assert_called_once()
        server._mock_connection.authenticate.assert_called_once()

        # Verify connection is stored
        assert server.connection == server._mock_connection
        assert server.access_controller is not None

    def test_ensure_connection_failure(self, server_with_mock_connection):
        """Test connection establishment failure."""
        server = server_with_mock_connection

        # Make connection fail
        server._mock_connection.connect.side_effect = OdooConnectionError("Connection failed")

        # Ensure connection should raise an error
        with pytest.raises(OdooConnectionError, match="Connection failed"):
            server._ensure_connection()

    def test_cleanup_connection(self, server_with_mock_connection):
        """Test connection cleanup."""
        server = server_with_mock_connection

        # First establish connection
        server._ensure_connection()
        assert server.connection is not None

        # Clean up
        server._cleanup_connection()

        # Verify connection was closed
        server._mock_connection.disconnect.assert_called_once()
        assert server.connection is None
        assert server.access_controller is None
        assert server.resource_handler is None

    def test_cleanup_connection_without_connection(self, server_with_mock_connection):
        """Test cleanup when no connection exists."""
        server = server_with_mock_connection

        # Should not raise an error
        server._cleanup_connection()

        # Connection disconnect should not be called
        server._mock_connection.disconnect.assert_not_called()

    def test_cleanup_connection_with_error(self, server_with_mock_connection):
        """Test cleanup when disconnect raises an error."""
        server = server_with_mock_connection

        # Establish connection first
        server._ensure_connection()

        # Make disconnect raise an error
        server._mock_connection.disconnect.side_effect = Exception("Disconnect failed")

        # Should not raise an error (error is logged)
        server._cleanup_connection()

        # Verify disconnect was attempted
        server._mock_connection.disconnect.assert_called_once()
        # Connection should still be cleared
        assert server.connection is None
        assert server.access_controller is None
        assert server.resource_handler is None

    @pytest.mark.asyncio
    async def test_run_stdio_success(self, server_with_mock_connection):
        """Test successful run_stdio execution via lifespan."""
        server = server_with_mock_connection

        # Make run_stdio_async invoke the lifespan like real FastMCP does
        async def mock_run_with_lifespan():
            async with server._odoo_lifespan(server.app):
                pass

        with patch("mcp_server_odoo.server.AccessController"):
            with patch("mcp_server_odoo.server.register_resources", return_value=Mock()):
                with patch("mcp_server_odoo.server.register_tools", return_value=Mock()):
                    server.app.run_stdio_async = mock_run_with_lifespan
                    await server.run_stdio()

        # Verify connection lifecycle was executed
        server._mock_connection.connect.assert_called_once()
        server._mock_connection.authenticate.assert_called_once()
        server._mock_connection.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_stdio_connection_failure(self, server_with_mock_connection):
        """Test run_stdio with connection failure — cleanup still runs."""
        server = server_with_mock_connection

        # Make connection fail. The eager dynamic-instructions connect
        # swallows the first failure; make sure the lifespan's reauth
        # attempt on the half-built connection fails too.
        server._mock_connection.connect.side_effect = OdooConnectionError("Failed to connect")
        server._mock_connection.is_authenticated = False
        server._mock_connection.is_connected = False

        # Make run_stdio_async invoke the lifespan (which will fail on connect)
        async def mock_run_that_invokes_lifespan():
            async with server._odoo_lifespan(server.app):
                pass

        server.app.run_stdio_async = mock_run_that_invokes_lifespan

        # Should raise since lifespan will fail on _ensure_connection
        with pytest.raises(OdooConnectionError, match="Failed to connect"):
            await server.run_stdio()

        # Cleanup should still run even when setup fails
        server._mock_connection.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_stdio_keyboard_interrupt(self, server_with_mock_connection):
        """An interrupt while serving still tears the connection down."""
        server = server_with_mock_connection

        # Real FastMCP raises from inside the lifespan (interrupt happens
        # while serving), so its finally-cleanup must run.
        async def mock_run_interrupted():
            async with server._odoo_lifespan(server.app):
                raise KeyboardInterrupt

        with patch("mcp_server_odoo.server.AccessController"):
            with patch("mcp_server_odoo.server.register_resources", return_value=Mock()):
                with patch("mcp_server_odoo.server.register_tools", return_value=Mock()):
                    server.app.run_stdio_async = mock_run_interrupted
                    # Should not raise (handled gracefully)
                    await server.run_stdio()

        # Lifespan teardown disconnected and cleared the connection
        server._mock_connection.disconnect.assert_called_once()
        assert server.connection is None

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
        """Test that lifespan context manager handles setup and teardown."""
        server = server_with_mock_connection

        # Mock AccessController
        with patch("mcp_server_odoo.server.AccessController") as mock_access_ctrl:
            with patch("mcp_server_odoo.server.register_resources") as mock_register_res:
                with patch("mcp_server_odoo.server.register_tools") as mock_register_tools:
                    mock_register_res.return_value = Mock()
                    mock_register_tools.return_value = Mock()

                    # Use the lifespan context manager
                    async with server._odoo_lifespan(server.app) as state:
                        # Verify setup was called
                        server._mock_connection.connect.assert_called_once()
                        server._mock_connection.authenticate.assert_called_once()
                        mock_access_ctrl.assert_called_once()
                        mock_register_res.assert_called_once()
                        mock_register_tools.assert_called_once()

                        # State should be an empty dict
                        assert state == {}

                    # After exiting, verify cleanup was called
                    server._mock_connection.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_cleanup_on_setup_failure(self, server_with_mock_connection):
        """Test that lifespan cleans up if setup fails after connection is created."""
        server = server_with_mock_connection

        # Connection succeeds but authenticate fails
        server._mock_connection.authenticate.side_effect = OdooConnectionError("Auth failed")

        with pytest.raises(OdooConnectionError, match="Auth failed"):
            async with server._odoo_lifespan(server.app):
                pass

        # Cleanup should still run
        server._mock_connection.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_teardown_exception_swallowed(self, server_with_mock_connection):
        """Test that lifespan teardown exceptions are swallowed gracefully."""
        server = server_with_mock_connection

        with patch("mcp_server_odoo.server.AccessController"):
            with patch("mcp_server_odoo.server.register_resources", return_value=Mock()):
                with patch("mcp_server_odoo.server.register_tools", return_value=Mock()):
                    server._mock_connection.disconnect.side_effect = RuntimeError("cleanup boom")
                    # Should not raise — _cleanup_connection swallows the error
                    async with server._odoo_lifespan(server.app):
                        pass
                    # Connection reference should still be cleared
                    assert server.connection is None

    def test_get_model_names_returns_list(self, server_with_mock_connection):
        """Test _get_model_names returns model name strings."""
        server = server_with_mock_connection

        # Set up connection and access controller
        with patch("mcp_server_odoo.server.AccessController") as mock_access_ctrl:
            mock_ac = Mock()
            mock_ac.get_enabled_models.return_value = [
                {"model": "res.partner", "name": "Contact"},
                {"model": "sale.order", "name": "Sales Order"},
            ]
            mock_access_ctrl.return_value = mock_ac

            server._ensure_connection()

            # Get model names
            names = server._get_model_names()
            assert names == ["res.partner", "sale.order"]

    def test_get_model_names_no_access_controller(self, valid_config):
        """Test _get_model_names returns empty list when no access controller."""
        server = OdooMCPServer(valid_config)

        # No connection/access controller set up
        names = server._get_model_names()
        assert names == []

    def test_get_model_names_exception_returns_empty(self, server_with_mock_connection):
        """Test _get_model_names returns empty list on exception."""
        server = server_with_mock_connection

        with patch("mcp_server_odoo.server.AccessController") as mock_access_ctrl:
            mock_ac = Mock()
            mock_ac.get_enabled_models.side_effect = RuntimeError("boom")
            mock_access_ctrl.return_value = mock_ac
            server._ensure_connection()

            names = server._get_model_names()
            assert names == []

    def test_get_model_names_yolo_mode_fallback(self, server_with_mock_connection):
        """Test _get_model_names queries ir.model when get_enabled_models returns []."""
        server = server_with_mock_connection

        with patch("mcp_server_odoo.server.AccessController") as mock_access_ctrl:
            mock_ac = Mock()
            mock_ac.get_enabled_models.return_value = []  # YOLO mode returns []
            mock_access_ctrl.return_value = mock_ac
            server._ensure_connection()

            # Mock the connection's search_read for ir.model fallback
            server._mock_connection.is_authenticated = True
            server._mock_connection.search_read.return_value = [
                {"model": "res.partner"},
                {"model": "sale.order"},
            ]

            names = server._get_model_names()
            assert names == ["res.partner", "sale.order"]
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

        # Build a real CompleteRequest and invoke the registered handler
        handler = server.app._mcp_server.request_handlers[types.CompleteRequest]
        req = types.CompleteRequest(
            method="completion/complete",
            params=types.CompleteRequestParams(
                ref=types.PromptReference(type="ref/prompt", name="test"),
                argument=types.CompletionArgument(name="model", value="res."),
            ),
        )

        result = await handler(req)
        values = result.root.completion.values
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

        handler = server.app._mcp_server.request_handlers[types.CompleteRequest]
        req = types.CompleteRequest(
            method="completion/complete",
            params=types.CompleteRequestParams(
                ref=types.PromptReference(type="ref/prompt", name="test"),
                argument=types.CompletionArgument(name="model", value=""),
            ),
        )

        result = await handler(req)
        values = result.root.completion.values
        assert len(values) == 20


class TestDynamicInstructions:
    """Dynamic initialize.instructions applied via eager connect.

    The SDK freezes instructions before the lifespan runs, so run_stdio/
    run_http must connect eagerly and personalize them first — and startup
    must never fail on it.
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
        """Server whose mocked connection serves the res.users context read."""
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key_12345",
            database="test_db",
        )
        with (
            patch("mcp_server_odoo.server.OdooConnection") as conn_cls,
            patch("mcp_server_odoo.server.AccessController"),
        ):
            mock_connection = Mock()
            mock_connection.is_authenticated = True
            mock_connection.uid = 2
            mock_connection.read.return_value = [dict(self.ADMIN_USER)]
            conn_cls.return_value = mock_connection

            server = OdooMCPServer(config)
            server._mock_connection = mock_connection
            yield server

    @pytest.mark.asyncio
    async def test_run_stdio_applies_personalized_instructions(self, server_with_user_read):
        server = server_with_user_read
        static = server.app.instructions
        server.app.run_stdio_async = AsyncMock()

        await server.run_stdio()

        instructions = server.app._mcp_server.instructions
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

        instructions = server.app._mcp_server.instructions
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

        instructions = server.app._mcp_server.instructions
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

        instructions = server.app._mcp_server.instructions
        assert instructions.startswith(static)
        assert "Datetime handling:" in instructions
        assert "You are connected to Odoo via MCP as:" not in instructions

    @pytest.mark.asyncio
    async def test_apply_dynamic_instructions_idempotent(self, server_with_user_read):
        """Repeated application rebuilds from the static base — the context
        block must never compound when one instance runs more than once."""
        server = server_with_user_read
        static = server.app.instructions

        await server._apply_dynamic_instructions()
        await server._apply_dynamic_instructions()

        instructions = server.app._mcp_server.instructions
        assert instructions.startswith(static)
        assert instructions.count("You are connected to Odoo via MCP as:") == 1
        assert instructions.count("Datetime handling:") == 1

    @pytest.mark.asyncio
    async def test_connect_failure_keeps_static_instructions(self, server_with_user_read):
        """Eager connect failure never aborts startup; instructions stay static."""
        server = server_with_user_read
        static = server.app.instructions
        server._mock_connection.connect.side_effect = OdooConnectionError("Odoo down")
        server._mock_connection.is_authenticated = False
        server.app.run_stdio_async = AsyncMock()

        await server.run_stdio()  # must not raise

        assert server.app._mcp_server.instructions == static


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
            server._ensure_connection()

            # If we get here, connection was successful
            assert server.connection is not None

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
    """Test the FastMCP app configuration."""

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
        """Test that FastMCP app is properly created."""
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

    def test_health_status_healthy_when_connected(self, valid_config):
        """Test health returns healthy when connected."""
        with patch("mcp_server_odoo.server.OdooConnection") as mock_conn_class:
            mock_conn = Mock()
            mock_conn.is_authenticated = True
            mock_conn.database = "test_db"
            mock_conn_class.return_value = mock_conn

            server = OdooMCPServer(valid_config)
            with patch("mcp_server_odoo.server.AccessController"):
                server._ensure_connection()

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


class TestConnectionPersistsAcrossHttpSessions:
    """Issue #70: streamable-http enters/exits the lifespan PER SESSION.

    The Odoo connection and the registered handlers must survive session
    teardown — previously every call after the first failed with
    'Not authenticated with Odoo'.
    """

    def _make_server(self, transport):
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key_12345",
            database="test_db",
            transport=transport,
        )
        with (
            patch("mcp_server_odoo.server.OdooConnection") as conn_cls,
            patch("mcp_server_odoo.server.AccessController"),
            patch("mcp_server_odoo.server.register_resources") as reg_res,
            patch("mcp_server_odoo.server.register_tools") as reg_tools,
        ):
            mock_connection = Mock()
            mock_connection.is_authenticated = True
            conn_cls.return_value = mock_connection
            server = OdooMCPServer(config)
            # run_http() sets this; these tests drive the lifespan directly.
            server._http_transport_active = transport == "streamable-http"
            yield server, conn_cls, mock_connection, reg_res, reg_tools

    @pytest.mark.asyncio
    async def test_http_sessions_reuse_connection_and_registrations(self):
        gen = self._make_server("streamable-http")
        server, conn_cls, mock_connection, reg_res, reg_tools = next(gen)

        # Session 1
        async with server._odoo_lifespan(server.app):
            pass
        # Session 2 (e.g. after a DELETE /mcp) — must NOT disconnect or rebuild
        async with server._odoo_lifespan(server.app):
            pass

        mock_connection.disconnect.assert_not_called()
        assert conn_cls.call_count == 1, "connection must be created exactly once"
        assert reg_res.call_count == 1, "resources must be registered exactly once"
        assert reg_tools.call_count == 1, "tools must be registered exactly once"
        assert server.connection is mock_connection, "connection survives session teardown"
        assert server.access_controller is not None

    @pytest.mark.asyncio
    async def test_stdio_still_cleans_up_on_exit(self):
        """stdio has one session per process — cleanup on exit stays correct."""
        gen = self._make_server("stdio")
        server, conn_cls, mock_connection, reg_res, reg_tools = next(gen)

        async with server._odoo_lifespan(server.app):
            assert server.connection is mock_connection

        mock_connection.disconnect.assert_called_once()
        assert server.connection is None

    @pytest.mark.asyncio
    async def test_stale_connection_reauthenticated_in_place(self):
        """A connection that lost authentication is reconnected IN PLACE —
        registered handlers hold references to it, so it must never be
        replaced with a new instance."""
        gen = self._make_server("streamable-http")
        server, conn_cls, mock_connection, reg_res, reg_tools = next(gen)

        async with server._odoo_lifespan(server.app):
            pass

        # Simulate auth loss between sessions
        mock_connection.is_authenticated = False
        mock_connection.is_connected = True

        async with server._odoo_lifespan(server.app):
            pass

        assert conn_cls.call_count == 1, "must not build a new connection object"
        mock_connection.authenticate.assert_called()
        assert server.connection is mock_connection
        # Reauth re-runs the api-key→password fallback chain — the controller
        # must track the connection's EFFECTIVE auth method
        assert server.access_controller.auth_method == mock_connection.auth_method

    @pytest.mark.asyncio
    async def test_recovery_after_failed_first_startup_registers_handlers(self):
        """If the first startup fails after self.connection was assigned but
        before auth succeeded, the next session's reauth must still create
        the AccessController — without it, handler registration silently
        skips and the recovered server serves zero tools while /health
        reports healthy."""
        gen = self._make_server("streamable-http")
        server, conn_cls, mock_connection, reg_res, reg_tools = next(gen)

        # Session 1: Odoo rejects auth — startup fails, half-built connection survives
        mock_connection.is_authenticated = False
        mock_connection.authenticate.side_effect = OdooConnectionError("auth failed")
        with pytest.raises(OdooConnectionError):
            async with server._odoo_lifespan(server.app):
                pass
        assert server.connection is mock_connection
        assert server.access_controller is None

        # Session 2: Odoo is back — reauth must recover a FULLY working server
        mock_connection.authenticate.side_effect = None
        mock_connection.is_connected = True
        async with server._odoo_lifespan(server.app):
            pass

        assert server.access_controller is not None, (
            "reauth recovery must create the access controller"
        )
        assert reg_res.call_count == 1, "resources must register after recovery"
        assert reg_tools.call_count == 1, "tools must register after recovery"


class TestTransportSecurity:
    """Test transport security configuration for DNS rebinding protection."""

    def test_loopback_bind_auto_enables_protection_when_allowed_hosts_is_empty(self):
        """Empty allowed_hosts does NOT mean "no host validation".

        _build_transport_security returns None, which hands the decision to
        the SDK — and FastMCP auto-enables its loopback allowlist for a
        127.0.0.1/localhost/::1 bind. This is the claim `.env.example`, the
        README and _build_transport_security's own docstring all make, so it
        needs an executable copy: the previous version of this test asserted
        the opposite in its name, docstring and comment.
        """
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            allowed_hosts=[],
        )
        server = OdooMCPServer(config)

        # The constructor forwards config.host rather than letting FastMCP
        # fall back to its own 127.0.0.1 default.
        assert server.app.settings.host == "localhost"

        settings = server.app.settings.transport_security
        assert settings is not None
        assert settings.enable_dns_rebinding_protection is True
        assert "localhost:*" in settings.allowed_hosts

    @pytest.mark.parametrize("host", ["localhost", "0.0.0.0"])
    @pytest.mark.asyncio
    async def test_run_http_binds_config_port_and_leaves_host_alone(self, host):
        """run_http() takes no host/port, and the last hop config -> settings
        is otherwise unasserted.

        settings.port is the ONLY place the port reaches FastMCP — the
        constructor passes host= and transport_security= but no port — so
        losing that line silently binds 8000 whatever --port said. And host
        must stay exactly as the constructor left it: reassigning it here
        moves the bind without moving the transport-security decision FastMCP
        already made from it, which is how a 0.0.0.0 config ends up on a
        loopback bind with protection off. Both hosts are exercised because a
        reintroduced ``host: str = "localhost"`` default is invisible when
        config.host is already localhost — the 0.0.0.0 case is what tells them
        apart.
        """
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            host=host,
            port=9000,
        )
        server = OdooMCPServer(config)
        constructed_host = server.app.settings.host
        assert constructed_host == host
        constructed_security = server.app.settings.transport_security

        server.app.run_streamable_http_async = AsyncMock()
        with patch.object(server, "_apply_dynamic_instructions", new=AsyncMock()):
            await server.run_http()

        assert server.app.settings.port == 9000
        assert server.app.settings.host == constructed_host
        assert server.app.settings.transport_security is constructed_security

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

        assert server.app.settings.transport_security is None

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

        with patch("mcp_server_odoo.server.FastMCP") as mock_fastmcp:
            mock_fastmcp.return_value = Mock()
            OdooMCPServer(config)

            # Verify FastMCP was called with transport_security=None
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


class TestSessionIdleTimeoutPreseed:
    """Test the session-manager pre-seed that applies the idle timeout."""

    def test_no_preseed_without_timeout(self):
        """Without the setting, the session manager is left for FastMCP to create."""
        server = OdooMCPServer(OdooConfig(url="http://localhost:8069", api_key="k"))

        server._preseed_session_manager()

        assert server.app._session_manager is None

    def test_preseed_applies_timeout_and_security(self):
        """The pre-seeded manager carries the timeout and mirrors FastMCP's settings."""
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        server = OdooMCPServer(
            OdooConfig(
                url="http://localhost:8069",
                api_key="k",
                allowed_hosts=["localhost"],
                session_idle_timeout=600,
            )
        )

        server._preseed_session_manager()

        manager = server.app._session_manager
        assert isinstance(manager, StreamableHTTPSessionManager)
        assert manager.session_idle_timeout == 600
        assert manager.security_settings is server.app.settings.transport_security
        assert manager.stateless is server.app.settings.stateless_http

    def test_fastmcp_reuses_preseeded_manager(self):
        """streamable_http_app() must use the pre-seeded manager, not build its own.

        This is the load-bearing assumption of the workaround; if a FastMCP
        upgrade changes the lazy initialization, this test fails loudly."""
        server = OdooMCPServer(
            OdooConfig(url="http://localhost:8069", api_key="k", session_idle_timeout=30)
        )

        server._preseed_session_manager()
        preseeded = server.app._session_manager
        server.app.streamable_http_app()

        assert server.app._session_manager is preseeded


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


class TestTeardownGateTracksRunningTransport:
    """The lifespan teardown must key off the transport that actually started,
    not off config.transport — run_http() is public and binds config.host /
    config.port, so it can run with ODOO_MCP_TRANSPORT unset."""

    @contextlib.contextmanager
    def _server(self, transport):
        """Patches must stay live for the whole test — a suspended generator
        keeps them only until GC, which made this order-dependent."""
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key_12345",
            database="test_db",
            transport=transport,
        )
        with (
            patch("mcp_server_odoo.server.OdooConnection") as conn_cls,
            patch("mcp_server_odoo.server.AccessController"),
            patch("mcp_server_odoo.server.register_resources"),
            patch("mcp_server_odoo.server.register_tools"),
        ):
            mock_connection = Mock()
            mock_connection.is_authenticated = True
            conn_cls.return_value = mock_connection
            yield OdooMCPServer(config), mock_connection

    def test_default_is_stdio_semantics(self):
        with self._server("stdio") as (server, _):
            assert server._http_transport_active is False

    @pytest.mark.asyncio
    async def test_run_http_under_default_stdio_config_keeps_connection(self):
        """The #70 failure: an HTTP session teardown must not disconnect and
        null the handlers just because config.transport was never set."""
        with self._server("stdio") as (server, mock_connection):
            with patch.object(server.app, "run_streamable_http_async", new=AsyncMock()):
                with patch.object(server, "_apply_dynamic_instructions", new=AsyncMock()):
                    await server.run_http()

            assert server._http_transport_active is True

            async with server._odoo_lifespan(server.app):
                pass

            mock_connection.disconnect.assert_not_called()
            assert server.connection is mock_connection
            assert server.tool_handler is not None


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
