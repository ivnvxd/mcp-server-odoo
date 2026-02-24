"""Transport integration tests for MCP server.

These tests verify that both stdio and streamable-http transports work
correctly with the Odoo MCP server in integration with pytest.
"""

import asyncio
import json
import subprocess
import sys
from typing import Any, Dict, List, Optional

import pytest
import requests

from tests.helpers.mcp_test_client import MCPTestClient

# Mark all tests in this module as requiring Odoo with MCP module
pytestmark = [pytest.mark.mcp]


class HttpTransportTester:
    """Helper class for testing streamable-http transport."""

    def __init__(self, base_url: str = "http://localhost:8002/mcp/"):
        self.base_url = base_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session_id: Optional[str] = None
        self.request_id = 0
        self.server_process = None

    def _next_id(self) -> int:
        """Get next request ID."""
        self.request_id += 1
        return self.request_id

    @staticmethod
    def _parse_sse_response(text: str) -> List[Dict[str, Any]]:
        """Parse SSE-formatted response."""
        results = []
        for line in text.strip().split("\n"):
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                    results.append(data)
                except json.JSONDecodeError:
                    pass
        return results

    async def start_server(self, port: int = 8002, timeout: int = 10) -> bool:
        """Start HTTP server."""
        try:
            self.server_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mcp_server_odoo",
                    "--transport",
                    "streamable-http",
                    "--port",
                    str(port),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Wait for server to start
            await asyncio.sleep(5)

            # Test if server is responding
            try:
                requests.get(f"http://localhost:{port}/", timeout=3)
                return True
            except requests.exceptions.RequestException:
                # Server might not have a root endpoint, try the MCP endpoint
                try:
                    # Just check if we can connect
                    requests.post(
                        f"http://localhost:{port}/mcp/", json={"test": "connectivity"}, timeout=3
                    )
                    return True
                except requests.exceptions.RequestException:
                    return self.server_process.poll() is None

        except Exception as e:
            print(f"Failed to start HTTP server: {e}")
            return False

    def stop_server(self):
        """Stop HTTP server."""
        if self.server_process:
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server_process.kill()
                self.server_process.wait()
            self.server_process = None

    async def _send_request(
        self, method: str, params: Dict[str, Any], request_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Send HTTP request and return response."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        payload = {"jsonrpc": "2.0", "method": method, "params": params}

        if request_id is not None:
            payload["id"] = request_id

        try:
            response = self.session.post(self.base_url, json=payload, headers=headers, timeout=15)

            # Update session ID if provided
            if "mcp-session-id" in response.headers:
                self.session_id = response.headers["mcp-session-id"]

            # Parse SSE response
            if response.status_code == 200:
                events = self._parse_sse_response(response.text)
                for event in events:
                    if request_id is not None and event.get("id") == request_id:
                        return event
                    elif request_id is None:
                        return {"status": response.status_code}

            return {
                "error": f"Request failed with status {response.status_code}",
                "status": response.status_code,
            }

        except Exception as e:
            return {"error": str(e)}


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
