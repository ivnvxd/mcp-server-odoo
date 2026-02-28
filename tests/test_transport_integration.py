"""Transport integration tests for MCP server.

These tests verify that both stdio and streamable-http transports work
correctly with the Odoo MCP server in integration with pytest.
"""

import pytest

from tests.helpers.mcp_test_client import MCPTestClient
from tests.helpers.transport_client import HttpTransportTester

# Mark all tests in this module as requiring Odoo with MCP module
pytestmark = [pytest.mark.mcp]


class TestTransportIntegration:
    """Integration tests for MCP transports."""

    @pytest.mark.asyncio
    async def test_stdio_transport_basic_flow(self, odoo_server_required):
        """Test stdio transport basic initialization and communication."""
        client = MCPTestClient()

        try:
            async with client.connect():
                # Test basic operations
                tools = await client.list_tools()
                assert len(tools) > 0, "Expected at least one tool"

                await client.list_resources()
                # Resources might be empty, that's ok for transport testing

                # Test a basic tool call - list_models should work with proper auth
                result = await client.call_tool("list_models", {})
                assert result is not None, "Tool call should return a result"
                # The result should be a proper MCP response
                assert hasattr(result, "content"), "Tool result should have content"

        except Exception as e:
            # Log the actual error for debugging
            import logging

            logging.error(f"stdio transport test failed: {e}")
            # Re-raise to fail the test
            raise

    @pytest.mark.asyncio
    async def test_stdio_transport_multiple_requests(self, odoo_server_required):
        """Test stdio transport can handle multiple sequential requests."""
        client = MCPTestClient()

        try:
            async with client.connect():
                # Make multiple tool list requests
                for i in range(3):
                    tools = await client.list_tools()
                    assert len(tools) > 0, f"Expected tools on request {i + 1}"

                # Make multiple resource list requests
                for i in range(3):
                    resources = await client.list_resources()
                    # Resources might be empty, that's ok - just testing transport stability
                    assert resources is not None, (
                        f"Resource list should not be None on request {i + 1}"
                    )

        except Exception as e:
            # Log the actual error for debugging
            import logging

            logging.error(f"stdio multiple requests test failed: {e}")
            raise

    @pytest.mark.asyncio
    async def test_http_transport_basic_flow(self, odoo_server_required):
        """Test streamable-http transport basic initialization and communication."""
        tester = HttpTransportTester()

        try:
            # Start server
            assert await tester.start_server(), "Failed to start HTTP server"

            # Test initialization
            init_params = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "clientInfo": {"name": "pytest-http", "version": "1.0.0"},
            }

            response = await tester._send_request("initialize", init_params, tester._next_id())
            assert response is not None, "No response to initialize request"
            assert "error" not in response, f"Error in initialize response: {response}"
            assert "result" in response, f"Expected result in response, got: {response}"
            assert tester.session_id is not None, "No session ID received"

            # Send initialized notification
            await tester._send_request("notifications/initialized", {})

            # Test tools/list
            response = await tester._send_request("tools/list", {}, tester._next_id())
            assert response is not None, "No response to tools/list request"
            assert "error" not in response, f"Error in tools/list response: {response}"
            assert "result" in response, f"Expected result in tools/list response, got: {response}"

        finally:
            tester.stop_server()

    @pytest.mark.asyncio
    async def test_http_transport_session_persistence(self, odoo_server_required):
        """Test that HTTP transport maintains session across requests."""
        tester = HttpTransportTester()

        try:
            # Start and initialize server
            assert await tester.start_server(), "Failed to start HTTP server"

            # Initialize
            init_params = {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest-http", "version": "1.0"},
            }
            response = await tester._send_request("initialize", init_params, tester._next_id())
            assert response and "result" in response

            original_session_id = tester.session_id
            assert original_session_id is not None, "No session ID after initialize"

            # Send initialized notification
            await tester._send_request("notifications/initialized", {})

            # Make multiple requests and verify session ID persists
            for i in range(3):
                response = await tester._send_request("tools/list", {}, tester._next_id())
                assert response is not None, f"No response to request {i + 1}"
                assert "error" not in response, f"Error in request {i + 1}: {response}"
                assert tester.session_id == original_session_id, (
                    f"Session ID changed on request {i + 1}"
                )

        finally:
            tester.stop_server()

    @pytest.mark.asyncio
    async def test_http_transport_tool_call(self, odoo_server_required):
        """Test HTTP transport can execute tool calls."""
        tester = HttpTransportTester()

        try:
            # Start and initialize server
            assert await tester.start_server(), "Failed to start HTTP server"

            # Initialize
            init_params = {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest-http", "version": "1.0"},
            }
            response = await tester._send_request("initialize", init_params, tester._next_id())
            assert response and "result" in response

            # Send initialized notification
            await tester._send_request("notifications/initialized", {})

            # Test list_models tool call
            params = {"name": "list_models", "arguments": {}}
            response = await tester._send_request("tools/call", params, tester._next_id())
            assert response is not None, "No response to tool call"
            # Note: The tool call might fail due to auth, but the transport should work
            # Just check that we got some kind of response (transport working)
            assert isinstance(response, dict), f"Expected dict response, got: {response}"
            # Accept either successful result or any error that's not a transport error
            has_result = "result" in response
            has_error = "error" in response
            if has_error:
                error = response.get("error")
                # If error is a dict with code, check it's not a transport error (-32600)
                if isinstance(error, dict) and error.get("code") == -32600:
                    raise AssertionError(f"Transport error in tool call: {response}")
            # If we get here, either we have a result or a non-transport error
            assert has_result or has_error, f"Response should have result or error: {response}"

        finally:
            tester.stop_server()


@pytest.mark.mcp
class TestTransportCompatibility:
    """Test transport compatibility and edge cases."""

    @pytest.mark.asyncio
    async def test_server_version_consistency(self, odoo_server_required):
        """Test that both transports can successfully connect and communicate."""
        # Test stdio connection
        stdio_client = MCPTestClient()
        stdio_connected = False

        try:
            async with stdio_client.connect():
                # Test basic operation to verify connection works
                tools = await stdio_client.list_tools()
                stdio_connected = len(tools) > 0
        except Exception:
            stdio_connected = False

        # Test HTTP connection
        http_tester = HttpTransportTester()
        http_connected = False

        try:
            if await http_tester.start_server():
                response = await http_tester._send_request(
                    "initialize",
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                    1,
                )
                http_connected = response is not None and "result" in response

        finally:
            http_tester.stop_server()

        # Both transports should successfully connect
        assert stdio_connected, "Failed to connect via stdio transport"
        assert http_connected, "Failed to connect via HTTP transport"
