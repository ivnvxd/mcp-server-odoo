"""Test package structure and basic functionality."""

import subprocess
import sys
from pathlib import Path


class TestPackageStructure:
    """Test the package structure and configuration."""

    def test_package_directory_exists(self):
        """Test that the package directory exists."""
        package_dir = Path(__file__).parent.parent / "mcp_server_odoo"
        assert package_dir.exists()
        assert package_dir.is_dir()

    def test_required_files_exist(self):
        """Test that all required files exist."""
        base_dir = Path(__file__).parent.parent
        required_files = [
            "pyproject.toml",
            "mcp_server_odoo/__init__.py",
            "mcp_server_odoo/__main__.py",
            "mcp_server_odoo/server.py",
            "tests/__init__.py",
        ]

        for file_path in required_files:
            full_path = base_dir / file_path
            assert full_path.exists(), f"Missing required file: {file_path}"

    def test_package_imports(self):
        """Test that the package can be imported."""
        import mcp_server_odoo

        # Check version
        assert hasattr(mcp_server_odoo, "__version__")
        assert mcp_server_odoo.__version__ == "0.4.0"

        # Check main class
        assert hasattr(mcp_server_odoo, "OdooMCPServer")

    def test_main_entry_point(self):
        """Test the main entry point."""
        from mcp_server_odoo.__main__ import main

        # Test help - argparse raises SystemExit for --help
        try:
            exit_code = main(["--help"])
            assert exit_code == 0
        except SystemExit as e:
            assert e.code == 0

    def test_cli_help(self):
        """Test CLI help output."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server_odoo", "--help"], capture_output=True, text=True
        )

        assert result.returncode == 0
        # Help output goes to stdout by default from argparse
        help_output = result.stdout or result.stderr
        assert "Odoo MCP Server" in help_output
        assert "ODOO_URL" in help_output
