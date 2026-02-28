"""Test package structure and basic functionality."""

import subprocess
import sys


class TestPackageStructure:
    """Test the package structure and configuration."""

    def test_package_imports(self):
        """Test that the package can be imported with expected exports."""
        import mcp_server_odoo

        assert hasattr(mcp_server_odoo, "__version__")
        assert hasattr(mcp_server_odoo, "OdooMCPServer")

    def test_cli_help(self):
        """Test CLI help output contains expected content."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server_odoo", "--help"], capture_output=True, text=True
        )

        assert result.returncode == 0
        # argparse sends --help to stdout
        assert "Odoo MCP Server" in result.stdout
        assert "ODOO_URL" in result.stdout
