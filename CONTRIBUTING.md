# Contributing to MCP Server for Odoo

Thanks for your interest in contributing! Whether it's a bug report, new feature, documentation improvement, or test coverage — all contributions are welcome.

## Development Setup

### Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) package manager

### Getting Started

1. Fork and clone the repository:

   ```bash
   git clone https://github.com/<your-username>/mcp-server-odoo.git
   cd mcp-server-odoo
   ```

2. Install in development mode:

   ```bash
   uv pip install -e ".[dev]"
   ```

3. *(Optional)* Create a `.env` file for integration tests:

   ```env
   ODOO_URL=http://localhost:8069
   ODOO_USER=admin
   ODOO_PASSWORD=admin
   ODOO_DB=demo
   ODOO_YOLO=true
   ```

## Running Tests

The project has three test tiers. Unit tests require no external services — integration tests auto-skip when Odoo is unavailable.

```bash
# Unit tests (no Odoo needed)
uv run pytest -m "not yolo and not mcp" --cov

# YOLO integration tests (vanilla Odoo, no MCP module)
uv run pytest -m "yolo" -v

# MCP integration tests (Odoo + MCP module installed)
uv run pytest -m "mcp" -v

# All tests
uv run pytest --cov

# Single test by name
uv run pytest -k "test_search_records" -v
```

## Code Style

The project uses [Ruff](https://docs.astral.sh/ruff/) for formatting and linting, and [ty](https://github.com/astral-sh/ty) for type checking. Run all checks before submitting:

```bash
uv run ruff check . && uv run ruff format --check . && uv run ty check .
```

Key settings:

- **Line length**: 100
- **Lint rules**: E, F, I, N, W, B, Q
- **Python target**: 3.10

To auto-fix formatting: `uv run ruff format .`

## Commit Messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/). Use lowercase, imperative mood, no trailing period.

**Format**: `<type>: <description>`

| Type | Purpose |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `docs` | Documentation only |
| `ci` | CI/CD changes |
| `chore` | Dependency updates, tooling |

**Examples**:

```
feat: add multi-language support via ODOO_LOCALE
fix: handle models without name field in create_record
refactor: rename test markers — integration→mcp, e2e→yolo
```

For non-trivial changes, add a body explaining **why**:

```
fix: use password auth for MCP integration tests in CI

Remove fake ODOO_API_KEY from CI env vars — it caused Access Denied
errors because Odoo rejected the dummy key at the XML-RPC dispatch
level. Tests now use password-only auth which works with
use_api_keys=False.
```

## Pull Request Process

1. **Fork** the repository and create a branch from `dev`
2. **Keep it focused** — one logical change per PR
3. **Add tests** for new functionality
4. **Update** `CHANGELOG.md` under the `[Unreleased]` section using [Keep a Changelog](https://keepachangelog.com/) format
5. **Run checks** locally — CI runs lint, unit tests, and integration tests
6. **Open a PR** targeting the `dev` branch
7. **Describe** what changed and why in the PR description

## Reporting Issues

### Bug Reports

Please include:

- Steps to reproduce the issue
- Expected vs actual behavior
- Odoo version and mode (standard or YOLO)
- MCP server version (`python -m mcp_server_odoo --version`)
- Relevant error messages or logs

### Feature Requests

Describe the use case and motivation. Explain how the feature would benefit users of the MCP server.

## Project Architecture

A quick orientation for navigating the codebase:

```
__main__.py → OdooConfig → OdooMCPServer → OdooConnection → FastMCP
```

| Module | Responsibility |
|--------|---------------|
| `server.py` | Orchestrates startup, registers handlers on FastMCP |
| `config.py` | `OdooConfig` dataclass from env vars, singleton via `get_config()` |
| `odoo_connection.py` | XML-RPC proxies, auth, CRUD convenience methods, caching |
| `tools.py` | MCP tool handlers (search, get, create, update, delete, list) |
| `resources.py` | MCP resource handlers with URI patterns |
| `schemas.py` | Pydantic models for structured tool return types |
| `access_control.py` | Model permission checks |
| `formatters.py` | LLM-friendly hierarchical text output |
| `performance.py` | Connection pooling, record/field caching |
| `error_handling.py` | Centralized error types and handler |
| `error_sanitizer.py` | Strips sensitive data from error messages |

The server operates in two modes: **Standard** (requires the Odoo MCP module with `/mcp/` endpoints) and **YOLO** (connects directly to `/xmlrpc/` endpoints, no module needed).

## License

By contributing, you agree that your contributions will be licensed under the [Mozilla Public License 2.0](LICENSE).
