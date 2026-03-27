# MCP Server for Odoo (Production Fork)

[![CI](https://github.com/ivnvxd/mcp-server-odoo/actions/workflows/ci.yml/badge.svg)](https://github.com/ivnvxd/mcp-server-odoo/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)

A production-oriented remote MCP server for Odoo ERP, designed to work with **ChatGPT Apps**, **Microsoft Copilot Studio**, and any MCP-compatible client over HTTPS.

This is a security-hardened fork of [ivnvxd/mcp-server-odoo](https://github.com/ivnvxd/mcp-server-odoo) (v0.5.0). It adds OAuth 2.0 authentication, config-driven safety guardrails, audit logging, and deployment-ready artifacts while preserving full backward compatibility with the upstream project.

## Why This Fork Exists

The upstream project is excellent for local use with Claude Desktop. This fork adds what's needed to expose the server **remotely over HTTPS** as a production service:

- **Client authentication** (OAuth 2.0, API keys) so not anyone can call your Odoo
- **Model allowlists** so you control exactly which Odoo models are exposed
- **Field-level filtering** to hide sensitive fields
- **Mutation controls** so the default mode is read-only
- **Audit logging** for compliance and debugging
- **CORS support** for browser-based MCP clients
- **Docker Compose** for easy deployment

**No Odoo module required.** This server uses YOLO mode (direct XML-RPC) because we cannot install modules in our customer's Odoo environment.

## Features

- **Remote MCP endpoint** over HTTP with CORS support
- **OAuth 2.0** (Entra ID, Auth0, Keycloak) and **API key** authentication
- **Config-driven safety**: model allowlists, field filtering, mutation controls
- **Audit logging** for all tool calls (structured JSON)
- **9 MCP tools**: search, get, list_models, list_fields, count, create, update, delete, list_resource_templates
- **Smart field selection** with automatic relevance scoring
- **Dry-run mode** for write operations
- **Confirmation tokens** for mutation safety
- **Health and readiness endpoints** (`/health`, `/ready`)
- **Docker and docker-compose** deployment
- **Backward compatible** with upstream stdio transport

## Quick Start

### Local Development (stdio)

```bash
# Install and run
uvx mcp-server-odoo

# Or with environment variables
ODOO_URL=http://localhost:8069 \
ODOO_USER=admin \
ODOO_PASSWORD=admin \
ODOO_YOLO=read \
uvx mcp-server-odoo
```

### Remote Server (HTTP)

```bash
ODOO_URL=http://your-odoo:8069 \
ODOO_USER=admin \
ODOO_PASSWORD=admin \
ODOO_YOLO=read \
ODOO_MCP_TRANSPORT=streamable-http \
ODOO_MCP_HOST=0.0.0.0 \
ODOO_MCP_AUTH_MODE=api_key \
ODOO_MCP_API_KEYS=your-secret-key \
ODOO_ALLOWED_MODELS=res.partner,res.company \
uv run mcp-server-odoo
```

### Docker Compose

```bash
cp .env.example .env
# Edit .env with your settings
docker compose up -d
```

## Deployment Modes

| Mode | Transport | Auth | Use Case |
|------|-----------|------|----------|
| **Local dev** | stdio | none | Claude Desktop, local testing |
| **Remote (API key)** | streamable-http | api_key | Internal tools, smoke tests |
| **Remote (OAuth)** | streamable-http | oauth2 | ChatGPT Apps, Copilot Studio |
| **Admin** | any | any | Trusted internal admin (dangerous) |

## Authentication

### No Auth (Local Only)

```env
ODOO_MCP_AUTH_MODE=none
```

Only appropriate for `stdio` transport. A warning is logged if used with HTTP.

### API Key Auth

```env
ODOO_MCP_AUTH_MODE=api_key
ODOO_MCP_API_KEYS=key1,key2
ODOO_ALLOWED_MODELS=res.partner,res.company
```

Clients send `X-API-Key: key1` header. Multiple keys supported for rotation.

### OAuth 2.0

```env
ODOO_MCP_AUTH_MODE=oauth2
ODOO_MCP_OAUTH2_ISSUER_URL=https://login.microsoftonline.com/{tenant-id}/v2.0
ODOO_MCP_OAUTH2_AUDIENCE=api://your-app-id
ODOO_MCP_OAUTH2_JWKS_URL=https://login.microsoftonline.com/{tenant-id}/discovery/v2.0/keys
ODOO_ALLOWED_MODELS=res.partner,res.company
```

Compatible with:
- **Microsoft Entra ID** (Azure AD)
- **Auth0**
- **Keycloak**
- Any OIDC provider with JWKS

Clients send `Authorization: Bearer <token>` header.

## Security Model

### Model Allowlist

```env
# Only these models are accessible
ODOO_ALLOWED_MODELS=res.partner,res.company,sale.order,project.task
```

**Required** when auth is enabled (fail-safe). Requests to non-allowed models are rejected.

### Per-Model Operation Control

```env
# JSON: restrict operations per model
ODOO_MODEL_OPERATION_MAP={"res.partner":["read","create"],"sale.order":["read"]}
```

### Field-Level Controls

```env
# Only expose these fields for res.partner
ODOO_FIELD_ALLOWLIST_RES_PARTNER=name,email,phone,company_id,street,city,country_id

# Hide sensitive fields
ODOO_FIELD_DENYLIST_RES_PARTNER=password_crypt,oauth_access_token
```

Model names use underscores: `res.partner` -> `RES_PARTNER`.

### Mutation Controls

```env
# Default: read-only (mutations disabled)
ODOO_ENABLE_MUTATIONS=false

# Enable writes (creates and updates)
ODOO_ENABLE_MUTATIONS=true

# Enable deletes (requires mutations enabled)
ODOO_ENABLE_DELETES=true

# Require confirmation token for writes
ODOO_REQUIRE_CONFIRMATION_FOR_MUTATIONS=true
```

### Record Limits

```env
ODOO_MAX_RECORDS_PER_QUERY=100
ODOO_MAX_BATCH_SIZE=50
```

### Admin Mode

```env
# DANGEROUS: bypasses ALL safety checks
ODOO_MCP_ADMIN_MODE=true
```

Admin mode disables model allowlists, field filtering, and mutation controls. Only use for trusted internal scenarios with enhanced audit logging.

## Available Tools

### Read Tools (always available)

| Tool | Description |
|------|-------------|
| `search_records` | Search with domain filters, pagination, smart field selection |
| `get_record` | Get a single record by ID |
| `list_models` | List all accessible models with permissions |
| `list_fields` | List field definitions for a model |
| `count_records` | Count records matching a domain |
| `list_resource_templates` | List available resource URI patterns |

### Write Tools (disabled by default)

| Tool | Description |
|------|-------------|
| `create_record` | Create a new record (supports dry-run) |
| `update_record` | Update an existing record (supports dry-run) |
| `delete_record` | Delete a record (requires `ODOO_ENABLE_DELETES=true`) |

Write tools support:
- **Dry-run mode**: `dry_run=true` validates without executing
- **Confirmation tokens**: When enabled, first call returns a token; second call with token executes

## Audit Logging

All tool calls are logged as structured JSON:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "correlation_id": "a1b2c3d4e5f6g7h8",
  "subject": "api_key_0",
  "auth_mode": "api_key",
  "tool_name": "search_records",
  "model": "res.partner",
  "operation": "read",
  "record_ids": null,
  "success": true,
  "duration_ms": 42.5
}
```

Sensitive field values are never logged. Disable with `ODOO_MCP_AUDIT_LOG=false`.

## Connecting from ChatGPT

1. Deploy the server with HTTPS (e.g., behind nginx/Cloudflare tunnel)
2. Configure OAuth 2.0 with your identity provider
3. In ChatGPT developer settings, add the MCP endpoint:
   - URL: `https://your-server.example.com/mcp`
   - Auth: OAuth 2.0 with your provider settings
4. The server exposes safe read tools by default

## Connecting from Microsoft Copilot Studio

1. Deploy the server with HTTPS
2. Configure OAuth 2.0 with Microsoft Entra ID:
   ```env
   ODOO_MCP_AUTH_MODE=oauth2
   ODOO_MCP_OAUTH2_ISSUER_URL=https://login.microsoftonline.com/{tenant}/v2.0
   ODOO_MCP_OAUTH2_AUDIENCE=api://your-app-registration-id
   ```
3. In Copilot Studio, add the MCP server as a custom connector
4. Configure the connection with your Entra ID app registration

## Odoo Configuration

### YOLO Mode (No Module Required)

```env
ODOO_URL=http://your-odoo:8069
ODOO_USER=admin
ODOO_PASSWORD=admin
ODOO_YOLO=read    # read-only (recommended)
# ODOO_YOLO=true  # full CRUD (use with mutation controls!)
```

### With Odoo MCP Module

```env
ODOO_URL=http://your-odoo:8069
ODOO_API_KEY=your-odoo-api-key
# ODOO_YOLO=off   # default, uses MCP module endpoints
```

## Docker

### Build and Run

```bash
docker build -t mcp-server-odoo .
docker run -p 8000:8000 --env-file .env mcp-server-odoo
```

### Docker Compose

```bash
docker compose up -d
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/mcp` | POST | Required | MCP protocol endpoint |
| `/health` | GET | None | Health check (connection status) |
| `/ready` | GET | None | Readiness check (fully initialized) |

## Configuration Reference

See [`.env.example`](.env.example) for the full list of environment variables with documentation.

### Key Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ODOO_URL` | (required) | Odoo server URL |
| `ODOO_MCP_AUTH_MODE` | `none` | `none`, `api_key`, or `oauth2` |
| `ODOO_ALLOWED_MODELS` | (none) | Comma-separated allowed models |
| `ODOO_ENABLE_MUTATIONS` | `false` | Enable write operations |
| `ODOO_ENABLE_DELETES` | `false` | Enable delete operations |
| `ODOO_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `ODOO_MCP_CORS_ORIGINS` | (none) | Allowed CORS origins |
| `ODOO_MCP_AUDIT_LOG` | `true` | Enable audit logging |
| `ODOO_MCP_ADMIN_MODE` | `false` | Bypass all safety checks (dangerous) |

## Secure Allowlist Example

A production-safe configuration for a CRM use case:

```env
# Odoo connection
ODOO_URL=https://your-odoo.example.com
ODOO_USER=mcp-service
ODOO_PASSWORD=strong-service-password
ODOO_YOLO=read

# Transport
ODOO_MCP_TRANSPORT=streamable-http
ODOO_MCP_HOST=0.0.0.0
ODOO_MCP_PORT=8000

# Authentication
ODOO_MCP_AUTH_MODE=api_key
ODOO_MCP_API_KEYS=your-secret-api-key-here

# Safety
ODOO_ALLOWED_MODELS=res.partner,res.company,crm.lead
ODOO_FIELD_ALLOWLIST_RES_PARTNER=name,email,phone,company_id,city,country_id
ODOO_FIELD_DENYLIST_RES_PARTNER=password_crypt,oauth_access_token
ODOO_MAX_RECORDS_PER_QUERY=50
ODOO_ENABLE_MUTATIONS=false
ODOO_ENABLE_DELETES=false

# CORS (if needed)
ODOO_MCP_CORS_ORIGINS=https://chat.openai.com

# Logging
ODOO_MCP_LOG_JSON=true
ODOO_MCP_AUDIT_LOG=true
```

## Migration from Upstream

This fork is fully backward compatible. Existing setups work without changes:

- All original environment variables are preserved
- stdio transport works identically
- YOLO mode works identically
- New safety features are opt-in (activate only when configured)

**Breaking changes when upgrading with new auth:**
- Setting `ODOO_MCP_AUTH_MODE=api_key` or `oauth2` requires `ODOO_ALLOWED_MODELS` to be set (fail-safe)
- Setting `ODOO_ENABLE_DELETES=true` requires `ODOO_ENABLE_MUTATIONS=true`

## Development

```bash
# Clone and install
git clone https://github.com/svenvanderwegen/mcp-server-odoo.git
cd mcp-server-odoo
uv sync --extra dev

# Run tests
uv run pytest -m "not yolo and not mcp" -v

# Lint
uv run ruff check .
uv run ruff format --check .
```

## License

MPL-2.0 (same as upstream)

## Acknowledgments

Based on [mcp-server-odoo](https://github.com/ivnvxd/mcp-server-odoo) by Andrey Ivanov.
