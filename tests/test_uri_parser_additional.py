"""Tests for URI parser with edge cases."""

import unittest

from mcp_server_odoo.utils import parse_uri


class TestUriParserWithEnabledModels(unittest.TestCase):
    """Test URI parser with enabled_models parameter."""

    def test_parse_uri_with_enabled_models(self):
        """Test parsing URI with enabled_models parameter."""
        uri = "odoo://res.partner/record/1"
        enabled_models = {"res.partner", "product.product"}
        model, operation, params = parse_uri(uri, enabled_models)
        self.assertEqual(model, "res.partner")
        self.assertEqual(operation, "record/1")
        self.assertEqual(params, {})

    def test_parse_uri_with_model_not_enabled(self):
        """Test parsing URI with a model that is not enabled."""
        uri = "odoo://res.partner/record/1"
        enabled_models = {"product.product", "product.template"}

        with self.assertRaises(ValueError) as context:
            parse_uri(uri, enabled_models)

        self.assertIn("Model not enabled for MCP access", str(context.exception))


if __name__ == "__main__":
    unittest.main()
