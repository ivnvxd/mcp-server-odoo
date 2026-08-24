# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - Unreleased

### Added
- **`get_current_context` tool**: returns the connected user, timezone, company scope and UTC guidance as structured data.
- **Personalized `initialize` instructions**: the same context block is sent at handshake (UTC-only fallback when the user read fails).
- **`get_fields` tool**: schema discovery with curated attributes by default; honors `field_names` and `attributes`.
- **Binary & attachment resources**: `odoo://{model}/record/{id}/{field}` and `odoo://attachment/{id}`, served with per-read mimeTypes.
- **No inlined base64**: populated binary fields are returned as fetchable resource URIs instead (reads use `bin_size`).
- **x2many previews**: `get_record` returns display-name summaries for small collections in `related_summaries`.
- **Inverse-field drilldown hints**: the record resource's one2many "view all" filters on the inverse field (generic `res_id` inverses and domain-restricted one2manys keep the id-in domain).
- **Offset depth cap**: offsets are capped at 1000 pages of `limit`, floored at 10,000 rows.
- **`aggregate_records` pagination**: reports `has_more` and `next_hint`.
- **`aggregate_records` overall totals**: an omitted or empty `groupby` returns a single overall row.
- **`post_message` subject**: optional `subject` argument.
- **Archived records**: the record resource serves them (`active_test=False`), matching what `get_record` already returned.
- **`all_fields_fallback`**: new `field_selection_method` value, reported when smart selection is unavailable and every field was read.

### Security
- **Attachment reads gate on the attached-to model**: `res_model` is checked alongside `ir.attachment`, so enabling that one model no longer exposes every attachment body on the database.
- **Attachment metadata is gated too**: the `search_records`/`get_record`/`aggregate_records` tools and the record/search/count resources scope `ir.attachment` to `res_model`s the caller may read — a row carries `url` and `index_content` (the extracted document text). An allowlist the module cannot report fails closed, as a retryable "could not verify access".
- **Attachment writes are gated too**: `create_record`/`update_record`/`delete_record` and `post_message`'s `attachment_ids` check the attached-to model. An ungated `update_record` could repoint an excluded model's attachment at an allowed one and then read it back, bypassing the read gate entirely.
- **`run_http()` host/port removed**: it reassigned `app.settings.host` after FastMCP had already chosen transport security from `config.host`, so an embedder could bind loopback with DNS-rebinding protection left off; the bind now always follows `config.host`/`config.port`.
- **Internal endpoints in error text**: hostnames, private IPs, ports and URLs are scrubbed from sanitized messages — a connection or DNS failure quotes the endpoint it tried, and the `ERROR_MAPPINGS` branch returned before the removal patterns ran.
- **`ODOO_MCP_ALLOWED_HOSTS` Origin ports**: an entry that pins a port now pins it for `Origin` too; the wildcard `:*` origin trusted a page served from any other port on the same host.
- **`ODOO_MCP_MAX_BINARY_SIZE`** (default 50 MB): bounds a single binary/attachment read, checked before the payload is fetched.
- **Sensitive-field filtering**: credential-named fields are withheld from bulk reads with an explanatory note; explicitly named fields are still returned. Covers `*password`/`*_pass`/`*secret`/`*_token`, the `*_api_key`/`*_hmac_key`/`*_signature_key` compounds, OAuth refresh tokens, passkeys, salts, OTP secrets and PINs, including trailing-underscore spellings (`pass_`, `api_key_`).
- **Credential-shaped log keys**: write-payload logging now uses the same detector as the read paths, so `smtp_pass`/`webhook_secret` are redacted instead of logged in cleartext.
- **`call_model_method` alias gaps**: `copy_data`, `copy_multi`, `update`, `get_view` and `get_views` are refused alongside the primitives they alias.
- **Sanitized error mappings**: captured fault text is scrubbed before interpolation — file paths, host:port pairs and memory addresses no longer reach the client through `ERROR_MAPPINGS`.
- **`call_model_method` hardening**: rejects `ir.actions.*`/`ir.cron`, the `web_*` family, and ORM CRUD primitives; list results truncate at 100 items.

### Fixed
- **Smart field selection**: the non-stored score cap gated on a `compute` key `fields_get()` never returns; it now gates on `store=False` (related fields exempt).
- **Oversized record ids**: ids outside the XML-RPC 32-bit range are rejected with a clean validation error before any RPC.
- **Oversized ints in domains, values and method arguments**: domains, `create_record`/`update_record` values and `call_model_method` arguments are all range-checked, instead of an `OverflowError` surfacing as `Connection error: Operation failed`.
- **YOLO `list_models` cap**: the silent 200-model cap is gone — listings truncate at 500 with an explicit note and the real total.
- **YOLO `list_models` filter**: the system-model exclusion is prefix-anchored (`!` + `=like`); plain `like` matched substrings and hid every model merely containing `ir.`/`base.` (`repair.*`, ...).
- **Fault classification**: business faults route on Odoo's `faultCode` (2 = UserError/ValidationError, 4 = AccessError), the only signal `/xmlrpc/2/*` sends — previously every business error read as `Connection error: Operation failed:`.
- **Business error line structure**: multi-line messages keep their newlines, and trailing `CONTEXT:`/`HINT:` lines are no longer stripped from business exceptions (psycopg2 diagnostics still are).
- **Traceback frame detection**: a frame is matched by shape, so a business message quoting a filename at line start is no longer mistaken for one and truncated.
- **Unsanitized error paths**: the record, search, count and fields resource handlers scrub the exception before surfacing it.
- **Non-loopback bind warning**: the HTTP startup warning now also states that DNS-rebinding protection is off unless `ODOO_MCP_ALLOWED_HOSTS` is set.
- **`ODOO_MCP_ALLOWED_HOSTS` port-less authority**: a port-less entry now also allows the bare `Host`/`Origin` a proxy sends on 80/443, which previously matched nothing.
- **`ODOO_MCP_ALLOWED_HOSTS` IPv6**: bracketed and bare literals are parsed and normalized instead of producing junk patterns that locked the deployment out.
- **HTTP session teardown**: keys off the transport that actually started, so `run_http()` under a default stdio config no longer strands tool handlers on a dead connection (#70).
- **`aggregate_records` groupby collision**: aggregating a field that is also a groupby key is refused on the pre-19 path instead of returning corrupted groups.
- **`aggregate_records` duplicate aggregates**: two aggregates over one field are refused on the pre-19 path, where `read_group` dropped one and mislabeled the other.
- **`__extra_domain` drilldown contract**: documented as the group's own condition, to be ANDed with the caller's `domain`.
- **`list_resource_templates`**: unreadable models are filtered out when `/mcp/models` reports per-model `operations` (best-effort — older modules omit the block and everything stays listed).
- **`id:` aggregates on Odoo 15/16**: refused with an explanation — `read_group` deletes the `id` key before returning, so the value silently never arrived.
- **`__extra_domain` on overall totals**: emitted as `[]` for `groupby=[]` on every version; Odoo 15/16 omit the key entirely.
- **Unbalanced domains**: a domain whose `&`/`|`/`!` operators lack operands is rejected with a clear message before any RPC, instead of reaching Odoo as a server-side "syntactically not correct".
- **Deeply nested `domain`/`fields` strings**: reported as an invalid parameter instead of an unexpected failure — `json.loads` recurses per nesting level, so a 2 KB `[[[[...]]]]` exhausted the stack.
- **Mutated caller context dicts**: `search()`, `read()` and `search_read()` copy the `context` they are given — `execute_kw` injects the locale into it, so a caller reusing one dict accumulated our keys.
- **Caller-supplied invalid `lang`**: no longer blamed on `ODOO_MCP_LOCALE` — a bad context language used to null the configured locale for every later request in the process.
- **`ModelInfo.operations` description**: was "Allowed operations (standard mode only)"; now states it is null in YOLO mode, where the flags are global and reported once under `yolo_mode.operations`.
- **Session cookie race**: the REST session id is read under the lock that creates it, so a concurrent 401 handler can no longer produce `Cookie: session_id=None`.

### Changed
- **`list_models` counts**: `total` is the number of models returned and `total_available` the database count; both are emitted in every mode.
- **YOLO `list_models` rows**: no longer repeat the global `operations` dict on every row — it is reported once under `yolo_mode.operations` (46.6 KB to 28.5 KB for 319 models).
- **Stored binaries in resource reads**: the record/search resources previously skipped every binary field; stored ones are now read under `bin_size` and rendered as fetchable URIs. Non-stored ones stay out — `bin_size` cannot short-circuit a compute (`sale.order.tax_totals` made a 10-row read 3.3x slower).
- **Default search resource reads**: `odoo://{model}/search` reads only the fields its one-line summary renders, instead of every safe field of every record and then discarding them.
- **`mcp` floor raised to 1.27**: `session_idle_timeout` is passed to `StreamableHTTPSessionManager`, which does not accept it before 1.27.

### Removed
- **`build_search_uri`**: unused public helper removed from `uri_schema`.
- **`Cache.invalidate_pattern`**: orphaned when the record cache was removed.

## [0.7.1] - 2026-06-12

### Added
- **`ODOO_MCP_ALLOWED_HOSTS`**: comma-separated `Host` headers to accept for the `streamable-http` transport (DNS-rebinding protection). Needed when running behind a reverse proxy that forwards an external host; unset preserves the prior behavior (the SDK auto-enables protection for a loopback bind only) (#45, @Miriup).
- **`ODOO_MCP_SESSION_IDLE_TIMEOUT`**: seconds of inactivity before a `streamable-http` session is closed and its server-side state freed; unset preserves the prior behavior (sessions never expire).

### Changed
- **Dependencies**: refreshed the lockfile — `mcp` 1.26.0 → 1.27.2 (streamable-http idle timeout, mid-request transport-close handling, pydantic 2.13 compat), `pydantic` 2.11 → 2.13, `starlette` 0.47 → 1.3, plus dev tooling (ruff, ty, pytest 9). Full suite green against the new stack.

## [0.7.0] - 2026-06-11

### Fixed
- **`delete_record` on records without a display name** (e.g. `mail.message`): the unlink succeeded but the result then failed schema validation (`deleted_name` received Odoo's `False`), so clients believed the delete had failed — now falls back to an ID label.
- **Timeouts**: Socket timeouts now actually apply to XML-RPC connections — a hung Odoo server fails fast instead of blocking forever.
- **Half-open keepalive recovery** (#68): a timeout on a reused keepalive socket (LB dropped the upstream without FIN/RST) transparently retries once on a fresh connection — idempotent calls only: read-only model methods plus login/version/db-list probes (a timed-out write is never re-sent, it could be double-executed by a slow server); timeouts on fresh connections are NOT retried.
- **streamable-http sessions** (#70): the Odoo connection and registered handlers now persist across HTTP sessions — previously every tool call after the first failed with "Not authenticated with Odoo" because session teardown disconnected Odoo. stdio still cleans up on exit.
- **Event loop**: Blocking XML-RPC/HTTP calls offloaded to worker threads; concurrent tool calls and pings stay responsive during slow operations.
- **YOLO `list_models`**: System-model exclusion was a no-op (tautological domain) — results were dominated by `ir.*`/`base.*` models.
- **Domain parsing**: String domains no longer corrupt values containing `True`/`False` substrings (e.g. `'True North'`).
- **Binary fields**: `search_records`/`get_record` results with binary/datetime fields serialize cleanly instead of erroring.
- **`fields=[]`**: Treated as smart defaults instead of silently fetching ALL fields.
- **`list_resource_templates`**: YOLO mode reports "all models available" instead of `total_models: 0`.
- **Search resource**: `odoo://{model}/search` skips binary/html fields like the record path; pagination hints now reference the `search_records` tool instead of emitting unroutable query-param URIs.
- **Formatters**: Float precision honors `digits` metadata; unset names render `Record {id}` instead of `False`; long values truncated with a marker; x2many "View all" hints carry real record ids.
- **Cache**: Memory budget actually enforced (looped eviction); replacing a key no longer evicts an unrelated entry.
- **Error sanitizer**: Server tracebacks reduced to the final error message (multi-line business errors kept intact) — no more leaked source lines, SQL constraint names, or row values; no more literal `{}` placeholders in error messages.
- **Error classification**: Builtin `PermissionError`/`ConnectionError` no longer misclassified as critical system errors (custom classes shadowed the builtins); access-control infrastructure failures report as connection errors, not "Access denied".
- **Config precedence**: `ODOO_MCP_TRANSPORT`/`HOST`/`PORT`/`LOG_LEVEL` set only in `.env` now take effect; invalid `ODOO_MCP_PORT` or `ODOO_MCP_SLOW_OPERATION_THRESHOLD_MS` no longer crash startup/import with raw tracebacks.
- **Auth fallback**: After a rejected API key falls back to password auth, permission checks use session auth instead of resending the dead key (which 401'd every tool call).
- **Logging**: Sensitive keys (passwords, API keys, tokens) redacted in debug-log write payloads; database names moved to DEBUG level.
- **Version**: `SERVER_VERSION` derives from the package version; a test pins it to pyproject.toml (v0.5.2 shipped reporting 0.5.1).

### Changed
- **TLS**: HTTPS transports share one `ssl.SSLContext` to amortize handshake cost across the per-proxy sockets introduced in 0.6.0.
- **HTTP transport**: Loud startup warning when binding a non-loopback host (the transport has no built-in authentication); README security notes added.
- **Dead code removed** (~550 lines): never-wired record/permission caches, `RequestOptimizer`, the browse resource chain, and the unused error metrics/conversion API; `ConnectionPool` simplified to the proxy factory it actually was.
- **CI**: Push trigger limited to `main`/`dev` (PRs cover feature branches; kills duplicate runs); Dependabot switched to the `uv` ecosystem so `uv.lock` gets update/security PRs; publish workflow verifies the built version against the release tag (manual dispatch requires typed version confirmation); Python 3.13 classifier added; MCP integration job skips Dependabot PRs — repository secrets aren't available to them, so the private-module checkout failed with "Input required and not supplied: token"; Dependabot version-update PRs now target `dev` directly (manual retargeting desynced Dependabot's base-branch metadata); codecov-action bumped to v7 (supersedes the stuck Dependabot PR).
- **`--help`**: documents `ODOO_MCP_ENABLE_METHOD_CALLS`.
- **Tests**: integration suite hardened — MCP-module detection works on multi-database instances (health probe sends `X-Odoo-Database`); an env-var leak that wedged spawned transport servers under DEBUG logging fixed; vacuous `if records:` guards replaced with real fixtures and assertions; MCP test client `read_resource` returned `""` (matched the wrong content type).

## [0.6.0] - 2026-05-02

### Added
- **`aggregate_records` tool**: Server-side aggregation via Odoo's grouping methods (#64). Dispatches to `formatted_read_group` on Odoo 19+ and falls back to `read_group` with response normalization on older versions.
- **`call_model_method` tool** (YOLO-only, opt-in via `ODOO_MCP_ENABLE_METHOD_CALLS`): generic XML-RPC `execute_kw` escape hatch for workflow actions not covered by CRUD.

### Fixed
- **Concurrent tool calls**: Per-proxy XML-RPC transport prevents wire-state corruption when multiple tools run in parallel (#44).
- **CI**: Disable MCP rate limiting in integration tests — the 300/min ceiling tripped mid-suite under the shared admin API key. Production keeps the default.

## [0.5.2] - 2026-04-30

### Added
- **`post_message` tool**: Post to an Odoo record's chatter via `mail.thread.message_post()` (#50).

### Fixed
- **Stdio transport**: Drop terminal `notifications/progress` from `search_records` and `list_models` — strict MCP clients treated the post-response notification as a fatal protocol error and tore down the transport.

## [0.5.1] - 2026-04-23

### Fixed
- **CI**: Skip MCP integration tests for fork PRs where repository secrets are unavailable
- **Search**: Cap over-limit to `max_limit` instead of resetting to `default_limit` (#57, @litnimax)
- **Search**: `ODOO_MCP_DEFAULT_LIMIT` now applies when clients omit `limit` — previously hardcoded, bypassing the env var in the common case (#61)

## [0.5.0] - 2026-02-28

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
