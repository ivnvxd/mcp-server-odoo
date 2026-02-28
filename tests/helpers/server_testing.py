"""Helper utilities for MCP server testing."""

import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import requests

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.server import OdooMCPServer

logger = logging.getLogger(__name__)


class MCPTestServer:
    """Test harness for MCP server lifecycle management."""

    def __init__(self, config: Optional[OdooConfig] = None):
        self.config = config or OdooConfig.from_env()
        self.server_process: Optional[subprocess.Popen] = None
        self.server: Optional[OdooMCPServer] = None
        self.odoo_connection: Optional[OdooConnection] = None

    async def start(self) -> None:
        """Start the MCP server for testing."""
        # Create server instance
        self.server = OdooMCPServer(self.config)

        # Establish connection
        self.server._ensure_connection()

        # Register resources
        self.server._register_resources()

        # Store connection reference
        self.odoo_connection = self.server.connection

    async def stop(self) -> None:
        """Stop the MCP server."""
        if self.server:
            self.server._cleanup_connection()
            self.server = None

        self.odoo_connection = None

    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()

    def start_subprocess(self) -> subprocess.Popen:
        """Start MCP server as a subprocess for external testing."""
        env = os.environ.copy()
        env.update(
            {
                "ODOO_URL": self.config.url,
                "ODOO_DATABASE": self.config.database,
                "PYTHONPATH": str(Path(__file__).parent.parent.parent),
            }
        )

        if self.config.api_key:
            env["ODOO_API_KEY"] = self.config.api_key
        else:
            if self.config.username:
                env["ODOO_USERNAME"] = self.config.username
            if self.config.password:
                env["ODOO_PASSWORD"] = self.config.password

        # Start server subprocess
        self.server_process = subprocess.Popen(
            [sys.executable, "-m", "mcp_server_odoo"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )

        # Wait for server to initialize (stdio — no port to poll)
        time.sleep(0.5)

        if self.server_process.poll() is not None:
            stdout, stderr = self.server_process.communicate()
            raise RuntimeError(f"Server crashed on startup:\nstdout: {stdout}\nstderr: {stderr}")

        return self.server_process

    def stop_subprocess(self) -> None:
        """Stop the server subprocess."""
        if self.server_process:
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server_process.kill()
            self.server_process = None


@contextlib.contextmanager
def mcp_test_server(config: Optional[OdooConfig] = None) -> Generator[MCPTestServer, None, None]:
    """Context manager for MCP test server lifecycle."""
    server = MCPTestServer(config)
    try:
        yield server
    finally:
        if server.server_process:
            server.stop_subprocess()


class OdooTestData:
    """Helper class for managing test data in Odoo."""

    def __init__(self, connection: OdooConnection):
        self.connection = connection
        self.created_records = []

    def create_test_partner(self, name: str = "Test Partner") -> int:
        """Create a test partner record."""
        partner_id = self.connection.execute_kw(
            "res.partner",
            "create",
            [
                {
                    "name": name,
                    "email": f"{name.lower().replace(' ', '.')}@test.com",
                    "is_company": False,
                }
            ],
            {},  # Empty kwargs
        )
        self.created_records.append(("res.partner", partner_id))
        return partner_id

    def create_test_product(self, name: str = "Test Product") -> int:
        """Create a test product record."""
        product_id = self.connection.execute_kw(
            "product.product",
            "create",
            [{"name": name, "type": "service", "list_price": 100.0}],
            {},  # Empty kwargs
        )
        self.created_records.append(("product.product", product_id))
        return product_id

    def cleanup(self) -> None:
        """Clean up all created test records."""
        for model, record_id in reversed(self.created_records):
            try:
                self.connection.execute_kw(model, "unlink", [[record_id]], {})  # Empty kwargs
            except Exception as e:
                logger.warning(f"Failed to cleanup {model} record {record_id}: {e}")

        self.created_records.clear()


def validate_mcp_response(response: Dict[str, Any]) -> bool:
    """Validate that a response follows MCP protocol format."""
    # Check for required fields based on response type
    if "error" in response:
        return all(key in response["error"] for key in ["code", "message"])

    if "result" in response:
        return True

    return False


def check_odoo_health(base_url: str, api_key: str, database: str | None = None) -> bool:
    """Check if Odoo MCP endpoints are healthy.

    Args:
        base_url: Odoo server URL
        api_key: API key to validate
        database: Database name for X-Odoo-Database header (needed for multi-DB)
    """
    try:
        db_headers: dict[str, str] = {}
        if database:
            db_headers["X-Odoo-Database"] = database

        # Check health endpoint
        response = requests.get(f"{base_url}/mcp/health", headers=db_headers, timeout=5)
        if response.status_code != 200:
            return False

        # Check auth endpoint (it's a GET endpoint)
        headers = {"X-API-Key": api_key, **db_headers}
        response = requests.get(f"{base_url}/mcp/auth/validate", headers=headers, timeout=5)

        if response.status_code == 200:
            data = response.json()
            return data.get("success", False) and data.get("data", {}).get("valid", False)

        return False

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return False


class PerformanceTimer:
    """Context manager for timing operations."""

    def __init__(self, name: str):
        self.name = name
        self.start_time = None
        self.end_time = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.time()
        self.duration = self.end_time - self.start_time
        logger.info(f"{self.name} took {self.duration:.3f} seconds")

    @property
    def elapsed(self) -> float:
        """Get elapsed time in seconds."""
        if self.end_time:
            return self.duration
        elif self.start_time:
            return time.time() - self.start_time
        else:
            return 0.0


def assert_performance(operation: str, duration: float, max_duration: float) -> None:
    """Assert that an operation completed within acceptable time."""
    if duration > max_duration:
        raise AssertionError(
            f"{operation} took {duration:.3f}s, exceeding limit of {max_duration}s"
        )


def create_test_env_file(test_dir: Path) -> Path:
    """Create a test .env file with server configuration."""
    import os

    env_file = test_dir / ".env"

    # Require environment variables to be set
    if not os.getenv("ODOO_URL"):
        raise ValueError("ODOO_URL environment variable not set. Please configure .env file.")

    if not os.getenv("ODOO_API_KEY") and not os.getenv("ODOO_PASSWORD"):
        raise ValueError("Neither ODOO_API_KEY nor ODOO_PASSWORD set. Please configure .env file.")

    lines = [
        f"ODOO_URL={os.getenv('ODOO_URL')}",
        f"ODOO_DATABASE={os.getenv('ODOO_DB', '')}",
        f"ODOO_MCP_LOG_LEVEL={os.getenv('ODOO_MCP_LOG_LEVEL', 'INFO')}",
    ]
    if os.getenv("ODOO_API_KEY"):
        lines.append(f"ODOO_API_KEY={os.getenv('ODOO_API_KEY')}")
    if os.getenv("ODOO_USER"):
        lines.append(f"ODOO_USER={os.getenv('ODOO_USER')}")
    if os.getenv("ODOO_PASSWORD"):
        lines.append(f"ODOO_PASSWORD={os.getenv('ODOO_PASSWORD')}")
    env_content = "\n".join(lines)

    env_file.write_text(env_content.strip())
    return env_file
