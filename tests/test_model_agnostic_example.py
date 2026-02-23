"""Example of model-agnostic testing approach.

This module demonstrates how to write tests that adapt to
whatever models are currently enabled in the MCP configuration.
"""

import pytest

from mcp_server_odoo.odoo_connection import OdooConnection


@pytest.mark.integration
class TestModelAgnosticApproach:
    """Examples of model-agnostic test patterns."""

    def test_with_any_readable_model(self, real_config, readable_model):
        """Test that works with any model that has read permission."""
        # The readable_model fixture provides a model guaranteed to be readable
        print(f"Testing with model: {readable_model.model}")

        conn = OdooConnection(real_config)
        conn.connect()
        conn.authenticate()

        # Search for records in the discovered model
        records = conn.search_read(readable_model.model, domain=[], fields=["id"], limit=1)

        # We can't assume specific data exists, but we can verify the operation works
        assert isinstance(records, list)
        print(f"Found {len(records)} records in {readable_model.model}")

    def test_with_writable_model(self, real_config, writable_model):
        """Test that requires a model with write permission."""
        # This test will be skipped if no writable models are available
        print(f"Testing write operations with: {writable_model.model}")

        # Your write operation tests here
        assert writable_model.can_write is True

    def test_error_handling_with_disabled_model(self, real_config, disabled_model):
        """Test error handling with a model that should not be accessible."""
        print(f"Testing access denial with: {disabled_model}")

        conn = OdooConnection(real_config)
        conn.connect()
        conn.authenticate()

        # This should fail with appropriate error
        from mcp_server_odoo.odoo_connection import OdooConnectionError

        with pytest.raises(OdooConnectionError):
            conn.search_read(disabled_model, [], ["id"])

    def test_adapting_to_available_models(self, real_config, model_discovery):
        """Test that discovers and uses whatever models are available."""
        # Get list of common models that might be enabled
        common_models = model_discovery.get_common_models()

        if not common_models:
            pytest.skip("No common models available")

        print(f"Found {len(common_models)} common models")

        # Use the first available model
        test_model = common_models[0]
        print(f"Using {test_model.model} for testing")

        conn = OdooConnection(real_config)
        conn.connect()
        conn.authenticate()

        # Perform operations based on what permissions are available
        if test_model.can_read:
            count = conn.search_count(test_model.model, [])
            print(f"{test_model.model} has {count} records")

    def test_conditional_based_on_permissions(self, real_config, readable_model):
        """Test that adapts behavior based on model permissions."""
        conn = OdooConnection(real_config)
        conn.connect()
        conn.authenticate()

        # Always test read (guaranteed by readable_model fixture)
        records = conn.search_read(readable_model.model, [], ["id"], limit=1)
        assert isinstance(records, list)

        # Conditionally test write operations
        if readable_model.can_write and records:
            # Only attempt write if permission exists and we have a record
            print(f"Model {readable_model.model} is writable, could test updates")
        else:
            print(f"Model {readable_model.model} is read-only, skipping write tests")

        # Conditionally test create operations
        if readable_model.can_create:
            print(f"Model {readable_model.model} allows creation")
        else:
            print(f"Model {readable_model.model} does not allow creation")


# Integration test fixture
@pytest.fixture
def real_config():
    """Create real configuration from environment."""
    import os

    from mcp_server_odoo.config import OdooConfig

    return OdooConfig(
        url=os.getenv("ODOO_URL"),
        api_key=os.getenv("ODOO_API_KEY"),
        database=os.getenv("ODOO_DB"),
        username=os.getenv("ODOO_USER"),
        password=os.getenv("ODOO_PASSWORD"),
    )
