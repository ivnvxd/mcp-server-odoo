"""Helper classes for testing MCP transports."""

import json
import select
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import requests


class TransportTestBase:
    """Base class for transport testing."""

    @staticmethod
    def parse_sse_response(text: str) -> List[Dict[str, Any]]:
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


class StdioTransportTester(TransportTestBase):
    """Test stdio transport."""

    def __init__(self):
        self.process = None

    def start_server(self, timeout: int = 5) -> bool:
        """Start stdio server."""
        try:
            self.process = subprocess.Popen(
                ["python", "-m", "mcp_server_odoo"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0,
            )
            time.sleep(0.5)  # Stdio server — no port to poll
            return self.process.poll() is None
        except Exception as e:
            print(f"Failed to start stdio server: {e}")
            return False

    def stop_server(self):
        """Stop stdio server."""
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None

    def send_message(self, message: Dict[str, Any], timeout: int = 5) -> Optional[Dict[str, Any]]:
        """Send message and get response."""
        if not self.process or not self.process.stdin:
            return None

        try:
            # Send message
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

            # Read response with timeout
            if sys.platform == "win32":
                # Windows doesn't support select on files
                time.sleep(0.5)
                if self.process.stdout:
                    response = self.process.stdout.readline()
                    if response:
                        return json.loads(response)
            else:
                # Unix-like systems
                ready, _, _ = select.select([self.process.stdout], [], [], timeout)
                if ready and self.process.stdout:
                    response = self.process.stdout.readline()
                    if response:
                        return json.loads(response)
        except Exception as e:
            print(f"Error sending message: {e}")

        return None

    def test_basic_flow(self) -> bool:
        """Test basic stdio flow."""
        # Initialize
        init_msg = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
            "id": 1,
        }

        response = self.send_message(init_msg)
        if not response or "result" not in response:
            return False

        # Send initialized notification
        initialized_msg = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        self.send_message(initialized_msg)

        # Test tools/list
        tools_msg = {"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2}

        response = self.send_message(tools_msg)
        return response is not None and "result" in response


class HttpTransportTester(TransportTestBase):
    """Test streamable-http transport with async methods."""

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

    async def start_server(self, port: int = 8002, timeout: int = 10) -> bool:
        """Start HTTP server, polling until it accepts connections."""
        import asyncio

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

            # Poll until the server accepts connections instead of a fixed sleep
            import socket

            for _ in range(timeout * 10):
                if self.server_process.poll() is not None:
                    return False
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.connect(("localhost", port))
                    sock.close()
                    return True
                except OSError:
                    sock.close()
                    await asyncio.sleep(0.1)

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

            if "mcp-session-id" in response.headers:
                self.session_id = response.headers["mcp-session-id"]

            if response.status_code == 200:
                events = self.parse_sse_response(response.text)
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
