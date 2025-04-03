"""Common test fixtures for mcp-server-odoo."""

import os
from unittest.mock import MagicMock

import pytest

from mcp import Resource


@pytest.fixture
def mock_odoo_connection():
    """Mock OdooConnection for testing."""
    connection = MagicMock()

    # Set up some default behaviors
    connection.uid = 1
    connection.available_models = ["res.partner", "product.template", "sale.order"]

    # Mock test_connection to succeed
    connection.test_connection.return_value = True

    # Mock model_fields_cache
    connection.model_fields_cache = {}

    return connection


@pytest.fixture
def mock_resource_handlers_registry():
    """Mock ResourceHandlerRegistry for testing."""
    registry = MagicMock()
    return registry


@pytest.fixture
def mock_mcp_server():
    """Mock MCP Server for testing."""
    server = MagicMock()
    return server


@pytest.fixture
def sample_record_resource():
    """Create a sample record resource for testing."""
    return Resource(uri="odoo://res.partner/record/1")


@pytest.fixture
def sample_search_resource():
    """Create a sample search resource for testing."""
    return Resource(uri="odoo://res.partner/search?domain=[]&limit=10")


@pytest.fixture
def sample_browse_resource():
    """Create a sample browse resource for testing."""
    return Resource(uri="odoo://res.partner/browse?ids=1,2,3")


@pytest.fixture
def sample_count_resource():
    """Create a sample count resource for testing."""
    return Resource(uri="odoo://res.partner/count?domain=[('is_company','=',true)]")


@pytest.fixture
def sample_fields_resource():
    """Create a sample fields resource for testing."""
    return Resource(uri="odoo://res.partner/fields")


@pytest.fixture
def env_vars_cleanup():
    """Clean up test environment variables."""
    old_environ = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(old_environ)
