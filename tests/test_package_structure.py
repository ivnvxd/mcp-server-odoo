"""Test package structure and basic functionality."""

import subprocess
import sys
from pathlib import Path

import pytest


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
        """Test that the package can be imported with expected exports."""
        import mcp_server_odoo

        # Check version exists and is a valid semver string
        assert hasattr(mcp_server_odoo, "__version__")
        parts = mcp_server_odoo.__version__.split(".")
        assert len(parts) == 3, f"Expected semver x.y.z, got {mcp_server_odoo.__version__}"
        assert all(p.isdigit() for p in parts)

        # Check main class
        assert hasattr(mcp_server_odoo, "OdooMCPServer")

    def test_main_entry_point(self):
        """Test the main entry point responds to --help."""
        from mcp_server_odoo.__main__ import main

        # argparse always raises SystemExit(0) for --help
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_cli_help(self):
        """Test CLI help output contains expected content."""
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server_odoo", "--help"], capture_output=True, text=True
        )

        assert result.returncode == 0
        # argparse sends --help to stdout
        assert "Odoo MCP Server" in result.stdout
        assert "ODOO_URL" in result.stdout
