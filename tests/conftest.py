"""Pytest configuration and fixtures for Odoo MCP Server tests."""

import os
import socket
import xmlrpc.client

import pytest
from dotenv import load_dotenv

from mcp_server_odoo.config import OdooConfig

# Load .env file for tests
load_dotenv()

# Import model discovery helper
try:
    from tests.helpers.model_discovery import ModelDiscovery

    MODEL_DISCOVERY_AVAILABLE = True
except ImportError:
    MODEL_DISCOVERY_AVAILABLE = False


def is_odoo_server_available(host: str = "localhost", port: int = 8069) -> bool:
    """Check if Odoo server is available at the given host and port."""
    try:
        # Try to connect to the server
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()

        if result != 0:
            return False

        # Try to access the XML-RPC endpoint
        try:
            proxy = xmlrpc.client.ServerProxy(f"http://{host}:{port}/xmlrpc/2/common")
            proxy.version()
            return True
        except Exception:
            return False

    except Exception:
        return False


# Global flag for Odoo server availability — derive host/port from ODOO_URL
def _parse_odoo_host_port() -> tuple[str, int]:
    from urllib.parse import urlparse

    url = os.getenv("ODOO_URL", "http://localhost:8069")
    parsed = urlparse(url)
    return parsed.hostname or "localhost", parsed.port or 8069


_host, _port = _parse_odoo_host_port()
ODOO_SERVER_AVAILABLE = is_odoo_server_available(_host, _port)


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "odoo_required: mark test as requiring a running Odoo server"
    )
    config.addinivalue_line("markers", "e2e: mark test as end-to-end requiring a running Odoo server")


def pytest_collection_modifyitems(config, items):
    """Modify test collection to skip tests that require Odoo when it's not available."""
    if ODOO_SERVER_AVAILABLE:
        # Server is available, don't skip anything
        return

    skip_odoo = pytest.mark.skip(reason=f"Odoo server not available at {_host}:{_port}")

    for item in items:
        # Skip tests marked with 'integration' or 'e2e' when server is not available
        if "integration" in item.keywords or "e2e" in item.keywords:
            item.add_marker(skip_odoo)

        # Skip tests marked with 'odoo_required' when server is not available
        if "odoo_required" in item.keywords:
            item.add_marker(skip_odoo)

        # Also check for specific test names that indicate they need a real server
        test_name = item.name.lower()
        if any(keyword in test_name for keyword in ["real_server", "integration"]):
            item.add_marker(skip_odoo)


@pytest.fixture(autouse=True)
def rate_limit_delay(request):
    """Add a delay between tests to avoid rate limiting (only when needed)."""
    # Add delay BEFORE integration tests that hit the real server
    test_name = request.node.name.lower() if hasattr(request.node, "name") else ""
    class_name = request.cls.__name__ if request.cls else ""

    # Check if this is an integration test that needs rate limit protection
    if (
        "integration" in request.keywords
        or "e2e" in request.keywords
        or "Integration" in class_name
        or "E2E" in class_name
        or "integration" in test_name
        or "real_" in test_name
    ):
        import time

        time.sleep(0.5)  # Brief delay before integration tests to avoid rate limiting

    yield


@pytest.fixture
def odoo_server_required():
    """Fixture that skips test if Odoo server is not available."""
    if not ODOO_SERVER_AVAILABLE:
        pytest.skip(f"Odoo server not available at {_host}:{_port}")


@pytest.fixture
def handle_rate_limit():
    """Fixture that handles rate limiting errors gracefully."""
    import urllib.error

    try:
        yield
    except Exception as e:
        # Check if this is a rate limit error
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
            pytest.skip("Skipping due to rate limiting")
        elif "429" in str(e) or "TOO MANY REQUESTS" in str(e):
            pytest.skip("Skipping due to rate limiting")
        else:
            raise


@pytest.fixture
def test_config_with_server_check(odoo_server_required) -> OdooConfig:
    """Create test configuration, but skip if server not available."""
    # Require environment variables to be set
    if not os.getenv("ODOO_URL"):
        pytest.skip("ODOO_URL environment variable not set. Please configure .env file.")

    if not os.getenv("ODOO_API_KEY"):
        pytest.skip("ODOO_API_KEY environment variable not set. Please configure .env file.")

    return OdooConfig(
        url=os.getenv("ODOO_URL"),
        api_key=os.getenv("ODOO_API_KEY"),
        database=os.getenv("ODOO_DB"),  # DB can be auto-detected
        log_level=os.getenv("ODOO_MCP_LOG_LEVEL", "INFO"),
        default_limit=int(os.getenv("ODOO_MCP_DEFAULT_LIMIT", "10")),
        max_limit=int(os.getenv("ODOO_MCP_MAX_LIMIT", "100")),
    )


# MCP Model Discovery Fixtures
# These fixtures help make tests model-agnostic by discovering
# and adapting to whatever models are currently available


@pytest.fixture
def model_discovery():
    """Create a model discovery helper.

    Creates a fresh discovery instance for each test.
    """
    if not MODEL_DISCOVERY_AVAILABLE:
        pytest.skip("Model Discovery not available")

    if not ODOO_SERVER_AVAILABLE:
        pytest.skip("Odoo server not available")

    # Create config for discovery
    config = OdooConfig(
        url=os.getenv("ODOO_URL"),
        api_key=os.getenv("ODOO_API_KEY"),
        database=os.getenv("ODOO_DB"),
    )

    discovery = ModelDiscovery(config)
    return discovery


@pytest.fixture
def readable_model(model_discovery):
    """Get a model with read permission.

    Skips test if no readable models are available.
    """
    return model_discovery.require_readable_model()


@pytest.fixture
def writable_model(model_discovery):
    """Get a model with write permission.

    Skips test if no writable models are available.
    """
    return model_discovery.require_writable_model()


@pytest.fixture
def disabled_model(model_discovery):
    """Get a model name that is NOT enabled.

    Returns a model that should fail access checks.
    """
    return model_discovery.get_disabled_model()


@pytest.fixture
def test_models(model_discovery):
    """Get commonly available models for testing.

    Returns a list of models that are commonly enabled,
    or skips if none are available.
    """
    models = model_discovery.get_common_models()
    if not models:
        models = [model_discovery.require_readable_model()]
    return models
