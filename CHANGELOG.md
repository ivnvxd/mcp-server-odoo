# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - Unreleased

### Added
- **Lifespan hooks**: Consolidated duplicated setup/teardown from `run_stdio()` and `run_http()` into a single `_odoo_lifespan()` async context manager passed to FastMCP — transport methods are now thin wrappers
- **Context injection**: All 7 tool functions and 4 resource functions accept `ctx: Context` for client-visible observability — sends info/warning/progress notifications to MCP clients in real time
- **Health endpoint**: `GET /health` HTTP route via FastMCP `custom_route` — returns connection status and version as JSON for load balancers and monitoring
- **Model autocomplete**: FastMCP completion handler returns matching model names when clients request autocomplete for `model` parameters in resource URIs

### Changed
- **Health endpoint**: Simplified response to `status`, `version`, and `connected` only — removed `url`, `database`, `error_metrics`, `recent_errors`, and `performance` fields
- **Tests**: Overhaul test suite — replace fake MCP protocol simulators with real handler calls, mock only at the XML-RPC network boundary, add CRUD/tool/lifespan/completion coverage, remove example-only and mock-asserting tests

### Fixed
- **Boolean formatting**: `False` values displayed as "Not set" instead of "No" in formatted output due to branch precedence bug in `_format_simple_value()`
- **Completion handler**: Returned raw list instead of `Completion(values=...)`, breaking MCP protocol contract
- **Lifespan cleanup**: Connection cleanup didn't run when setup failed because setup was outside the `try` block

## [0.4.5] - 2026-02-27

### Changed
- **Documentation**: Synchronized `--help`, `.env.example`, and README with all current environment variables and test commands
- **CI**: Run lint and unit tests in parallel; gate integration tests to PRs and main/dev branches; add concurrency groups and uv caching
- **Project config**: Clean up `.gitignore`, trim redundant dependencies, consolidate dev tooling config

## [0.4.4] - 2026-02-25

### Changed
- **Connection startup**: `OdooConnection.connect()` resolves the target database *before* creating MCP proxies in standard mode, setting the `X-Odoo-Database` header for `_test_connection()` and all subsequent calls
- **`AccessController` auth**: Supports session-cookie authentication as fallback when no API key is configured — authenticates via `/web/session/authenticate` and retries once on session expiry

### Fixed
- **All standard-mode auth combinations now work**: API-key-only (S1/S2), API-key + user/pass (S3/S4), and user/pass-only (S5/S6) — with or without explicit `ODOO_DB` — all connect, authenticate, and pass access control. Previously S1–S6 could fail with 404 on multi-DB servers
- **Multi-database routing**: Standard mode MCP endpoints (`/mcp/xmlrpc/*`, `/mcp/models`, `/mcp/health`, etc.) returned 404 when multiple databases existed in PostgreSQL because Odoo couldn't determine which DB to route to. Now sends `X-Odoo-Database` header on all MCP requests (XML-RPC and REST)
- **DB endpoint routing**: Database listing now always uses the server-wide `/xmlrpc/db` endpoint instead of `/mcp/xmlrpc/db`, since the latter requires a DB context that isn't available yet

## [0.4.3] - 2026-02-24

### Changed
- **CI**: Reduce Python matrix to 3.10 + 3.13; add YOLO and MCP integration test jobs with Odoo 18 + PostgreSQL 17 Docker containers
- **Test markers**: Rename `integration` → `mcp`, `e2e` → `yolo`, drop `odoo_required`; add markers to 30 previously-invisible tests; remove `RUN_MCP_TESTS` gate
- **CI coverage**: Collect coverage from unit, YOLO and MCP jobs; merge and upload to Codecov

### Fixed
- **Standard mode auth**: `AccessController` no longer crashes at startup when only username/password is configured (without `ODOO_API_KEY`). Now logs a warning and defers auth failure to REST call time with actionable error messages suggesting `ODOO_API_KEY` or YOLO mode

## [0.4.2] - 2026-02-23

### Added
- **Docker support**: Multi-stage Dockerfile with `uv` for fast builds, and GitHub Actions workflow for multi-platform images (amd64 + arm64) published to both GHCR and Docker Hub

## [0.4.1] - 2026-02-22

### Added
- **Multi-language support**: New `ODOO_LOCALE` environment variable to get Odoo responses in any installed language (e.g. `es_ES`, `fr_FR`, `de_DE`). Validates locale at startup and falls back to default language if not installed. (#23)

### Fixed
- **Models without `name` field**: `create_record` and `update_record` failed on models like `mail.activity` that lack a `name` field due to hardcoded field list in post-operation read-back
- **Version-aware record URLs**: `create_record` and `update_record` now generate `/odoo/{model}/{id}` URLs for Odoo 18+ instead of the legacy `/web#id=...` format (which is still used for Odoo ≤ 17)

## [0.4.0] - 2026-02-22

### Added
- **Structured output**: All tools return typed Pydantic models with auto-generated JSON schemas for MCP clients (`SearchResult`, `RecordResult`, `ModelsResult`, `CreateResult`, `UpdateResult`, `DeleteResult`)
- **Tool annotations**: All tools declare `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` via MCP `ToolAnnotations`
- **Resource annotations**: All resources declare `audience` and `priority` via MCP `Annotations`
- **Human-readable titles**: All tools and resources include `title` for better display in MCP clients

### Changed
- **MCP SDK**: Upgraded from `>=1.9.4` to `>=1.26.0,<2`
- **`get_record` structured output**: Returns `RecordResult` with separate `record` and `metadata` fields instead of injecting `_metadata` into record data
- **Tooling**: Replace black/mypy with ruff format/ty for formatting and type checking

### Fixed
- **VertexAI compatibility**: Simplified `search_records` `domain`/`fields` type hints from `Union` to `Optional[Any]` to avoid `anyOf` JSON schemas rejected by VertexAI/Google ADK (#27)
- **Stale record data**: Removed record-level caching from `read()` to prevent returning stale field values (e.g. `active`) when records change in Odoo between calls (#28)
- **Tests**: Integration tests now use `ODOO_URL` for server detection, deduplicated server checks, fixed async test handling, updated assertions for structured output types, halved suite runtime

### Removed
- Legacy error type aliases (`ToolError`, `ResourceError`, `ResourceNotFoundError`, `ResourcePermissionError`) — use `ValidationError`, `NotFoundError`, `PermissionError` directly
- Unused `_setup_handlers()` method from `OdooMCPServer`

## [0.3.1] - 2026-02-21

### Fixed
- **Authentication bypass**: Add missing `@property` on `is_authenticated` — was always truthy as a method reference, bypassing auth guards

### Changed
- Update CI dependencies (black 26.1.0, GitHub Actions v6/v7)
- Server version test validates semver format instead of hardcoded value

## [0.3.0] - 2025-09-14

### Added
- **YOLO Mode**: Development mode for testing without MCP module installation
  - Read-Only: Safe demo mode with read-only access to all models
  - Full Access: Unrestricted access for development (never use in production)
  - Works with any standard Odoo instance via native XML-RPC endpoints

## [0.2.2] - 2025-08-04

### Added
- **Direct Record URLs**: Added `url` field to `create_record` and `update_record` responses for direct access to records in Odoo

### Changed
- **Minimal Response Fields**: Reduced `create_record` and `update_record` tool responses to return only essential fields (id, name, display_name) to minimize LLM context usage
- **Smart Field Optimization**: Implemented dynamic field importance scoring to reduce smart default fields to most essential across all models, with configurable limit via `ODOO_MCP_MAX_SMART_FIELDS`

## [0.2.1] - 2025-06-28

### Changed
- **Resource Templates**: Updated `list_resource_templates` tool to clarify that query parameters are not supported in FastMCP resources

## [0.2.0] - 2025-06-19

### Added
- **Write Operations**: Enabled full CRUD functionality with `create_record`, `update_record`, and `delete_record` tools (#5)

### Changed
- **Resource Simplification**: Removed query parameters from resource URIs due to FastMCP limitations - use tools for advanced queries (#4)

### Fixed
- **Domain Parameter Parsing**: Fixed `search_records` tool to accept both JSON strings and Python-style domain strings, supporting various format variations

## [0.1.2] - 2025-06-19

### Added
- **Resource Discovery**: Added `list_resource_templates` tool to provide resource URI template information
- **HTTP Transport**: Added streamable-http transport support for web and remote access

## [0.1.1] - 2025-06-16

### Fixed
- **HTTPS Connection**: Fixed SSL/TLS support by using `SafeTransport` for HTTPS URLs instead of regular `Transport`
- **Database Validation**: Skip database existence check when database is explicitly configured, as listing may be restricted for security

## [0.1.0] - 2025-06-08

### Added

#### Core Features
- **MCP Server**: Full Model Context Protocol implementation using FastMCP with stdio transport
- **Dual Authentication**: API key and username/password authentication
- **Resource System**: Complete `odoo://` URI schema with 5 operations (record, search, browse, count, fields)
- **Tools**: `search_records`, `get_record`, `list_models` with smart field selection
- **Auto-Discovery**: Automatic database detection and connection management

#### Data & Performance
- **LLM-Optimized Output**: Hierarchical text formatting for AI consumption
- **Connection Pooling**: Efficient connection reuse with health checks
- **Pagination**: Smart handling of large datasets
- **Caching**: Performance optimization for frequently accessed data
- **Error Handling**: Comprehensive error sanitization and user-friendly messages

#### Security & Access Control
- **Multi-layered Security**: Odoo permissions + MCP-specific access controls
- **Session Management**: Automatic credential injection and session handling
- **Audit Logging**: Complete operation logging for security

## Limitations
- **No Prompts**: Guided workflows not available
- **Alpha Status**: API may change before 1.0.0

**Note**: This alpha release provides production-ready data access for Odoo via AI assistants.