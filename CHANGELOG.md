# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - Unreleased

### Added
- **Structured output**: All tools return typed Pydantic models with auto-generated JSON schemas for MCP clients (`SearchResult`, `RecordResult`, `ModelsResult`, `CreateResult`, `UpdateResult`, `DeleteResult`)
- **Tool annotations**: All tools declare `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` via MCP `ToolAnnotations`
- **Resource annotations**: All resources declare `audience` and `priority` via MCP `Annotations`
- **Human-readable titles**: All tools and resources include `title` for better display in MCP clients

### Changed
- **MCP SDK**: Upgraded from `>=1.9.4` to `>=1.26.0,<2`
- **`get_record` structured output**: Returns `RecordResult` with separate `record` and `metadata` fields instead of injecting `_metadata` into record data
- **Tooling**: Replace black/mypy with ruff format/ty for formatting and type checking

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