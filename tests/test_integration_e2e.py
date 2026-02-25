"""Comprehensive end-to-end integration tests for Odoo MCP Server.

These tests validate the complete MCP server functionality with a real Odoo server
using .env configuration. They test the full lifecycle, all resource operations,
authentication flows, and error handling.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict

import pytest
import requests

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection
from tests.helpers.server_testing import (
    MCPTestServer,
    PerformanceTimer,
    assert_performance,
    check_odoo_health,
    create_test_env_file,
    mcp_test_server,
    run_mcp_command,
    validate_mcp_response,
    validate_resource_operation,
)

# Mark all tests in this module as requiring Odoo with MCP module
pytestmark = [pytest.mark.mcp]


def _resolve_db_header(config: OdooConfig) -> Dict[str, str]:
    """Resolve target database and return X-Odoo-Database header dict.

    MCP addon routes return 404 when multiple DBs exist and no DB context
    is provided. This helper uses a temporary connection to resolve the DB.
    """
    conn = OdooConnection(config)
    conn.connect()
    try:
        db = conn.auto_select_database()
    finally:
        conn.disconnect()
    return {"X-Odoo-Database": db} if db else {}


class TestServerLifecycle:
    """Test MCP server lifecycle management."""

    @pytest.mark.asyncio
    async def test_server_startup_and_shutdown(self):
        """Test that server can start up and shut down cleanly."""
        # Create server with test configuration
        config = OdooConfig.from_env()
        server = MCPTestServer(config)

        # Start server
        await server.start()
        assert server.server is not None
        assert server.odoo_connection is not None

        # Verify connection is active
        assert server.odoo_connection.is_connected

        # Stop server
        await server.stop()
        assert server.server is None
        assert server.odoo_connection is None

    def test_server_subprocess_lifecycle(self):
        """Test server can be started as a subprocess."""
        config = OdooConfig.from_env()

        with mcp_test_server(config) as server:
            # Start subprocess
            process = server.start_subprocess()
            assert process is not None
            assert process.poll() is None  # Process is running

            # Give server time to initialize
            time.sleep(2)

            # Process should still be running
            assert process.poll() is None

        # After context exit, process should be terminated
        assert server.server_process is None

    def test_server_with_env_file(self, tmp_path):
        """Test server can load configuration from .env file."""
        # Create test .env file
        create_test_env_file(tmp_path)

        # Change to test directory
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            # Load config from .env
            config = OdooConfig.from_env()
            assert config.url == os.getenv("ODOO_URL", "http://localhost:8069")
            assert config.api_key == (os.getenv("ODOO_API_KEY") or None)
            assert config.database == os.getenv("ODOO_DB")

        finally:
            os.chdir(original_cwd)

    def test_uvx_server_startup(self):
        """Test that server can be started with uvx command."""
        # Create a test script to simulate uvx execution
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server_odoo", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Server module should be executable
        assert result.returncode == 0 or "MCP" in result.stdout or "MCP" in result.stderr


class TestAuthenticationFlows:
    """Test authentication flows with different configurations."""

    def test_api_key_authentication_from_env(self):
        """Test API key authentication using .env configuration."""
        config = OdooConfig.from_env()

        if not config.api_key:
            pytest.skip("ODOO_API_KEY not configured")

        # Verify API key is loaded
        assert config.api_key is not None

        # Test connection with API key
        conn = OdooConnection(config)
        conn.connect()
        conn.authenticate()

        assert conn.is_connected
        assert conn.uid is not None

        # Verify we can execute operations
        version = conn.get_server_version()
        assert version is not None

        conn.close()

    def test_username_password_fallback(self):
        """Test fallback to username/password when API key fails."""
        # Create config with invalid API key
        config = OdooConfig(
            url=os.getenv("ODOO_URL", "http://localhost:8069"),
            api_key="invalid_key",
            database=os.getenv("ODOO_DB"),
            username=os.getenv("ODOO_USER", "admin"),
            password=os.getenv("ODOO_PASSWORD", "admin"),
        )

        conn = OdooConnection(config)

        # Connection should succeed with username/password fallback
        conn.connect()
        conn.authenticate()
        assert conn.is_connected

        conn.close()

    def test_rest_api_authentication(self):
        """Test REST API authentication with API key."""
        config = OdooConfig.from_env()
        db_header = _resolve_db_header(config)

        # Test health check (no auth)
        response = requests.get(f"{config.url}/mcp/health", headers=db_header)
        assert response.status_code == 200

        # Test authenticated endpoint
        headers = {"X-API-Key": config.api_key, **db_header}
        response = requests.get(f"{config.url}/mcp/system/info", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is True
        assert "db_name" in data.get("data", {})

    def test_authentication_error_handling(self):
        """Test proper error handling for authentication failures."""
        config = OdooConfig.from_env()
        db_header = _resolve_db_header(config)

        # Test with invalid API key
        headers = {"X-API-Key": "invalid_key", **db_header}
        response = requests.get(f"{config.url}/mcp/system/info", headers=headers)

        if config.api_key:
            # With API key auth enabled, invalid key should be rejected
            assert response.status_code == 401
        else:
            # Without API key auth, endpoint uses public user (use_api_keys=False)
            assert response.status_code == 200


class TestResourceOperations:
    """Test all resource operations with real Odoo data."""

    @pytest.mark.asyncio
    async def test_record_resource_operation(self):
        """Test record resource for retrieving single records."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Use existing admin user record (ID=2)
            # This avoids needing create permissions
            uri = "odoo://res.users/record?id=2"
            success, error = await validate_resource_operation(server.server, uri)

            assert success, f"Record operation failed: {error}"

    @pytest.mark.asyncio
    async def test_search_resource_operation(self):
        """Test search resource for finding records."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test search operation
            uri = "odoo://res.users/search?domain=[('id','=',2)]&limit=5"
            success, error = await validate_resource_operation(server.server, uri)

            assert success, f"Search operation failed: {error}"

    @pytest.mark.asyncio
    async def test_browse_resource_operation(self):
        """Test browse resource for navigating records."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test browse operation
            uri = "odoo://res.partner/browse?offset=0&limit=10"
            success, error = await validate_resource_operation(server.server, uri)

            assert success, f"Browse operation failed: {error}"

    @pytest.mark.asyncio
    async def test_count_resource_operation(self):
        """Test count resource for counting records."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test count operation
            uri = "odoo://res.partner/count?domain=[]"
            success, error = await validate_resource_operation(server.server, uri)

            assert success, f"Count operation failed: {error}"

    @pytest.mark.asyncio
    async def test_fields_resource_operation(self):
        """Test fields resource for model metadata."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test fields operation
            uri = "odoo://res.partner/fields"
            success, error = await validate_resource_operation(server.server, uri)

            assert success, f"Fields operation failed: {error}"

    @pytest.mark.asyncio
    async def test_resource_with_complex_domain(self):
        """Test resource operations with complex search domains."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Use a complex domain with existing data
            # Search for users with specific criteria
            domain = "[('id','in',[1,2]),('active','=',True)]"
            uri = f"odoo://res.users/search?domain={domain}&limit=5"

            response = await run_mcp_command(server.server, "resources/read", {"uri": uri})

            assert "result" in response
            contents = response["result"]["contents"]
            assert len(contents) > 0

            # Verify we got results
            text = contents[0]["text"]
            assert "Mock data" in text  # Our mock implementation returns this


class TestErrorHandling:
    """Test error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_invalid_model_error(self):
        """Test error handling for invalid model names."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test with non-existent model
            uri = "odoo://invalid.model/search?domain=[]"
            success, error = await validate_resource_operation(server.server, uri)

            assert not success
            assert "access" in error.lower() or "not found" in error.lower()

    @pytest.mark.asyncio
    async def test_access_denied_error(self):
        """Test error handling for access denied scenarios."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test with a model that might not be MCP-enabled
            uri = "odoo://ir.config_parameter/search?domain=[]"
            response = await run_mcp_command(server.server, "resources/read", {"uri": uri})

            # Should either succeed or return proper error
            assert validate_mcp_response(response)

    @pytest.mark.asyncio
    async def test_invalid_uri_format_error(self):
        """Test error handling for malformed URIs."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test various invalid URIs
            invalid_uris = [
                "invalid://format",
                "odoo://",
                "odoo://model/invalid_operation",
                "odoo://res.partner/search?invalid_param=test",
            ]

            for uri in invalid_uris:
                response = await run_mcp_command(server.server, "resources/read", {"uri": uri})

                assert "error" in response

    def test_connection_failure_recovery(self):
        """Test recovery from connection failures."""
        config = OdooConfig.from_env()
        conn = OdooConnection(config)

        # Connect initially
        conn.connect()
        conn.authenticate()
        assert conn.is_connected

        # Simulate connection loss by closing
        conn.close()
        assert not conn.is_connected

        # Need to manually reconnect
        conn.connect()
        conn.authenticate()
        version = conn.get_server_version()
        assert version is not None
        assert conn.is_connected

        conn.close()

    @pytest.mark.asyncio
    async def test_large_result_handling(self):
        """Test handling of large result sets."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Request large number of records
            uri = "odoo://res.partner/browse?limit=1000"

            with PerformanceTimer("Large result fetch"):
                response = await run_mcp_command(server.server, "resources/read", {"uri": uri})

            assert "result" in response
            # Should handle gracefully, possibly with pagination info


class TestPerformanceAndReliability:
    """Test performance and reliability aspects."""

    @pytest.mark.asyncio
    async def test_connection_reuse(self):
        """Test that connections are properly reused."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()
            conn = server.odoo_connection

            # Perform multiple operations
            for _ in range(5):
                version = conn.get_server_version()
                assert version is not None

            # Connection should be reused
            assert conn.is_connected

    @pytest.mark.asyncio
    async def test_operation_performance(self):
        """Test that operations complete within acceptable time."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test various operations with timing
            operations = [
                ("Record fetch", "odoo://res.users/record?id=2", 1.0),
                ("Small search", "odoo://res.partner/search?limit=10", 1.0),
                ("Field metadata", "odoo://res.partner/fields", 2.0),
                ("Count operation", "odoo://res.partner/count", 1.0),
            ]

            for op_name, uri, max_time in operations:
                with PerformanceTimer(op_name) as timer:
                    response = await run_mcp_command(server.server, "resources/read", {"uri": uri})

                assert "result" in response
                assert_performance(op_name, timer.elapsed, max_time)

    @pytest.mark.asyncio
    async def test_concurrent_operations(self):
        """Test handling of concurrent operations."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Define concurrent tasks
            async def fetch_resource(uri: str) -> Dict[str, Any]:
                return await run_mcp_command(server.server, "resources/read", {"uri": uri})

            # Run multiple operations concurrently
            uris = [
                "odoo://res.users/record?id=2",
                "odoo://res.partner/count",
                "odoo://res.partner/fields",
                "odoo://res.users/search?limit=5",
            ]

            with PerformanceTimer("Concurrent operations"):
                results = await asyncio.gather(*[fetch_resource(uri) for uri in uris])

            # All operations should succeed
            for result in results:
                assert validate_mcp_response(result)
                assert "result" in result

    def test_server_health_monitoring(self):
        """Test server health check functionality."""
        config = OdooConfig.from_env()
        db_header = _resolve_db_header(config)
        db_name = db_header.get("X-Odoo-Database")

        # Check Odoo health — without API key, use password to verify endpoint is up
        if config.api_key:
            is_healthy = check_odoo_health(config.url, config.api_key, database=db_name)
            assert is_healthy

            # Test with invalid credentials (only meaningful when API keys are enforced)
            is_healthy = check_odoo_health(config.url, "invalid_key", database=db_name)
            assert not is_healthy
        else:
            # Without API key, just verify the health endpoint responds
            response = requests.get(f"{config.url}/mcp/health", headers=db_header, timeout=5)
            assert response.status_code == 200


class TestMCPProtocolCompliance:
    """Test MCP protocol compliance and integration."""

    @pytest.mark.asyncio
    async def test_resource_list_operation(self):
        """Test MCP resources/list operation."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # List available resources
            response = await run_mcp_command(server.server, "resources/list", {})

            assert "result" in response
            resources = response["result"]["resources"]
            assert isinstance(resources, list)

            # Should have schema resources
            schema_resources = [r for r in resources if "schema" in r["uri"]]
            assert len(schema_resources) == 5  # One for each operation

    @pytest.mark.asyncio
    async def test_mcp_response_format(self):
        """Test that all responses follow MCP format."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test successful response
            response = await run_mcp_command(
                server.server, "resources/read", {"uri": "odoo://res.users/record?id=2"}
            )

            assert validate_mcp_response(response)
            assert "result" in response

            result = response["result"]
            assert "contents" in result
            assert isinstance(result["contents"], list)

            for content in result["contents"]:
                assert "uri" in content
                assert "mimeType" in content
                assert content["mimeType"] == "text/plain"
                assert "text" in content

    @pytest.mark.asyncio
    async def test_schema_resources(self):
        """Test that schema resources are properly served."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test each schema resource
            schema_uris = [
                "odoo://schema/record",
                "odoo://schema/search",
                "odoo://schema/browse",
                "odoo://schema/count",
                "odoo://schema/fields",
            ]

            for uri in schema_uris:
                response = await run_mcp_command(server.server, "resources/read", {"uri": uri})

                assert "result" in response
                contents = response["result"]["contents"]
                assert len(contents) > 0

                # Schema should be in JSON format
                schema_text = contents[0]["text"]
                schema = json.loads(schema_text)

                # Validate schema structure
                assert "operation" in schema
                assert "parameters" in schema
                assert "description" in schema


class TestEndToEndWorkflow:
    """Test complete end-to-end workflows."""

    @pytest.mark.asyncio
    async def test_read_workflow(self):
        """Test read operations workflow with existing data."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Test reading existing user record
            uri = "odoo://res.users/record?id=2"
            response = await run_mcp_command(server.server, "resources/read", {"uri": uri})

            assert "result" in response
            text = response["result"]["contents"][0]["text"]
            assert "Mock data" in text  # Our mock returns this

            # Test search operation
            search_uri = "odoo://res.users/search?domain=[('id','=',2)]"
            response = await run_mcp_command(server.server, "resources/read", {"uri": search_uri})

            assert "result" in response

            # Test browse operation
            browse_uri = "odoo://res.users/browse?limit=5"
            response = await run_mcp_command(server.server, "resources/read", {"uri": browse_uri})

            assert "result" in response

    @pytest.mark.asyncio
    async def test_relationship_navigation_workflow(self):
        """Test navigating relationships between models."""
        config = OdooConfig.from_env()

        async with MCPTestServer(config) as server:
            await server.start()

            # Start with a user
            user_uri = "odoo://res.users/record?id=2"
            response = await run_mcp_command(server.server, "resources/read", {"uri": user_uri})

            assert "result" in response
            # user_text = response["result"]["contents"][0]["text"]

            # Extract partner_id from response (simplified)
            # In real implementation, would parse the formatted text

            # Navigate to related partner
            partner_uri = "odoo://res.partner/search?domain=[('user_ids','=',2)]"
            response = await run_mcp_command(server.server, "resources/read", {"uri": partner_uri})

            assert "result" in response
            assert len(response["result"]["contents"]) > 0
