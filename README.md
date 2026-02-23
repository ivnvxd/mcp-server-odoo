# MCP Server for Odoo

[![CI](https://github.com/ivnvxd/mcp-server-odoo/actions/workflows/ci.yml/badge.svg)](https://github.com/ivnvxd/mcp-server-odoo/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/ivnvxd/mcp-server-odoo/branch/main/graph/badge.svg)](https://codecov.io/gh/ivnvxd/mcp-server-odoo)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with ty](https://img.shields.io/badge/checked%20with-ty-blue?labelColor=orange)](https://github.com/astral-sh/ty)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MPL 2.0](https://img.shields.io/badge/License-MPL_2.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)

An MCP server that enables AI assistants like Claude to interact with Odoo ERP systems. Access business data, search records, create new entries, update existing data, and manage your Odoo instance through natural language.

**Works with any Odoo instance!** Use [YOLO mode](#yolo-mode-developmenttesting-only-) for quick testing and demos with any standard Odoo installation. For enterprise security, access controls, and production use, install the [Odoo MCP module](https://apps.odoo.com/apps/modules/19.0/mcp_server).

## Features

- 🔍 **Search and retrieve** any Odoo record (customers, products, invoices, etc.)
- ✨ **Create new records** with field validation and permission checks
- ✏️ **Update existing data** with smart field handling
- 🗑️ **Delete records** respecting model-level permissions
- 🔢 **Count records** matching specific criteria
- 📋 **Inspect model fields** to understand data structure
- 🔐 **Secure access** with API key or username/password authentication
- 🎯 **Smart pagination** for large datasets
- 🧠 **Smart field selection** — automatically picks the most relevant fields per model
- 💬 **LLM-optimized output** with hierarchical text formatting
- 🌍 **Multi-language support** — get responses in your preferred language
- 🚀 **YOLO Mode** for quick access with any Odoo instance (no module required)

## Installation

### Prerequisites

- Python 3.10 or higher
- Access to an Odoo instance:
  - **Standard mode** (production): Version 16.0+ with the [Odoo MCP module](https://apps.odoo.com/apps/modules/19.0/mcp_server) installed
  - **YOLO mode** (testing/demos): Any Odoo version with XML-RPC enabled (no module required)

### Install UV First

The MCP server runs on your **local computer** (where Claude Desktop is installed), not on your Odoo server. You need to install UV on your local machine:

<details>
<summary>macOS/Linux</summary>

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
</details>

<details>
<summary>Windows</summary>

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```
</details>

After installation, restart your terminal to ensure UV is in your PATH.

### Installing via MCP Settings (Recommended)

Add this configuration to your MCP settings:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

<details>
<summary>Claude Desktop</summary>

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_API_KEY": "your-api-key-here",
        "ODOO_DB": "your-database-name"
      }
    }
  }
}
```
</details>

<details>
<summary>Claude Code</summary>

Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_API_KEY": "your-api-key-here",
        "ODOO_DB": "your-database-name"
      }
    }
  }
}
```

Or use the CLI:

```bash
claude mcp add odoo \
  --env ODOO_URL=https://your-odoo-instance.com \
  --env ODOO_API_KEY=your-api-key-here \
  --env ODOO_DB=your-database-name \
  -- uvx mcp-server-odoo
```
</details>

<details>
<summary>Cursor</summary>

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_API_KEY": "your-api-key-here",
        "ODOO_DB": "your-database-name"
      }
    }
  }
}
```
</details>

<details>
<summary>VS Code (with GitHub Copilot)</summary>

Add to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "odoo": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_API_KEY": "your-api-key-here",
        "ODOO_DB": "your-database-name"
      }
    }
  }
}
```

> **Note:** VS Code uses `"servers"` as the root key, not `"mcpServers"`.
</details>

<details>
<summary>Windsurf</summary>

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_API_KEY": "your-api-key-here",
        "ODOO_DB": "your-database-name"
      }
    }
  }
}
```
</details>

<details>
<summary>Zed</summary>

Add to `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "odoo": {
      "command": {
        "path": "uvx",
        "args": ["mcp-server-odoo"],
        "env": {
          "ODOO_URL": "https://your-odoo-instance.com",
          "ODOO_API_KEY": "your-api-key-here",
          "ODOO_DB": "your-database-name"
        }
      }
    }
  }
}
```
</details>

### Alternative Installation Methods

<details>
<summary>Using Docker</summary>

Run with Docker — no Python installation required:

```json
{
  "mcpServers": {
    "odoo": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "ODOO_URL=http://host.docker.internal:8069",
        "-e", "ODOO_API_KEY=your-api-key-here",
        "ivnvxd/mcp-server-odoo"
      ]
    }
  }
}
```

> **Note:** Use `host.docker.internal` instead of `localhost` to connect to Odoo running on the host machine.

For HTTP transport:

```bash
docker run --rm -p 8000:8000 \
  -e ODOO_URL=http://host.docker.internal:8069 \
  -e ODOO_API_KEY=your-api-key-here \
  ivnvxd/mcp-server-odoo --transport streamable-http --host 0.0.0.0
```

The image is also available on GHCR: `ghcr.io/ivnvxd/mcp-server-odoo`
</details>

<details>
<summary>Using pip</summary>

```bash
# Install globally
pip install mcp-server-odoo

# Or use pipx for isolated environment
pipx install mcp-server-odoo
```

Then use `mcp-server-odoo` as the command in your MCP configuration.
</details>

<details>
<summary>From source</summary>

```bash
git clone https://github.com/ivnvxd/mcp-server-odoo.git
cd mcp-server-odoo
pip install -e .
```

Then use the full path to the package in your MCP configuration.
</details>

## Configuration

### Environment Variables

The server requires the following environment variables:

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `ODOO_URL` | Yes | Your Odoo instance URL | `https://mycompany.odoo.com` |
| `ODOO_API_KEY` | Yes* | API key for authentication | `0ef5b399e9ee9c11b053dfb6eeba8de473c29fcd` |
| `ODOO_USER` | Yes* | Username (if not using API key) | `admin` |
| `ODOO_PASSWORD` | Yes* | Password (if not using API key) | `admin` |
| `ODOO_DB` | No | Database name (auto-detected if not set) | `mycompany` |
| `ODOO_LOCALE` | No | Language/locale for Odoo responses | `es_ES`, `fr_FR`, `de_DE` |
| `ODOO_YOLO` | No | YOLO mode - bypasses MCP security (⚠️ DEV ONLY) | `off`, `read`, `true` |

*Either `ODOO_API_KEY` or both `ODOO_USER` and `ODOO_PASSWORD` are required.

**Notes:**
- If database listing is restricted on your server, you must specify `ODOO_DB`
- API key authentication is recommended for better security
- The server also loads environment variables from a `.env` file in the working directory

#### Advanced Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ODOO_MCP_DEFAULT_LIMIT` | `10` | Default number of records returned per search |
| `ODOO_MCP_MAX_LIMIT` | `100` | Maximum allowed record limit per request |
| `ODOO_MCP_MAX_SMART_FIELDS` | `15` | Maximum fields returned by smart field selection |
| `ODOO_MCP_LOG_LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `ODOO_MCP_LOG_JSON` | `false` | Enable structured JSON log output |
| `ODOO_MCP_LOG_FILE` | — | Path for rotating log file (10 MB, 5 backups) |
| `ODOO_MCP_TRANSPORT` | `stdio` | Transport type (`stdio`, `streamable-http`) |
| `ODOO_MCP_HOST` | `localhost` | Host to bind for HTTP transport |
| `ODOO_MCP_PORT` | `8000` | Port to bind for HTTP transport |

### Transport Options

The server supports multiple transport protocols for different use cases:

#### 1. **stdio** (Default)
Standard input/output transport - used by desktop AI applications like Claude Desktop.

```bash
# Default transport - no additional configuration needed
uvx mcp-server-odoo
```

#### 2. **streamable-http**
Standard HTTP transport for REST API-style access and remote connectivity.

```bash
# Run with HTTP transport
uvx mcp-server-odoo --transport streamable-http --host 0.0.0.0 --port 8000

# Or use environment variables
export ODOO_MCP_TRANSPORT=streamable-http
export ODOO_MCP_HOST=0.0.0.0
export ODOO_MCP_PORT=8000
uvx mcp-server-odoo
```

The HTTP endpoint will be available at: `http://localhost:8000/mcp/`

> **Note**: SSE (Server-Sent Events) transport has been deprecated in MCP protocol version 2025-03-26. Use streamable-http transport instead for HTTP-based communication. Requires MCP library v1.9.4 or higher for proper session management.

<details>
<summary>Running streamable-http transport for remote access</summary>

```json
{
  "mcpServers": {
    "odoo-remote": {
      "command": "uvx",
      "args": ["mcp-server-odoo", "--transport", "streamable-http", "--port", "8080"],
      "env": {
        "ODOO_URL": "https://your-odoo-instance.com",
        "ODOO_API_KEY": "your-api-key-here",
        "ODOO_DB": "your-database-name"
      }
    }
  }
}
```
</details>

### Setting up Odoo

1. **Install the MCP module**:
   - Download the [mcp_server](https://apps.odoo.com/apps/modules/18.0/mcp_server) module
   - Install it in your Odoo instance
   - Navigate to Settings > MCP Server

2. **Enable models for MCP access**:
   - Go to Settings > MCP Server > Enabled Models
   - Add models you want to access (e.g., res.partner, product.product)
   - Configure permissions (read, write, create, delete) per model

3. **Generate an API key**:
   - Go to Settings > Users & Companies > Users
   - Select your user
   - Under the "API Keys" tab, create a new key
   - Copy the key for your MCP configuration

### YOLO Mode (Development/Testing Only) ⚠️

YOLO mode allows the MCP server to connect directly to any standard Odoo instance **without requiring the MCP module**. This mode bypasses all MCP security controls and is intended **ONLY for development, testing, and demos**.

**🚨 WARNING: Never use YOLO mode in production environments!**

#### YOLO Mode Levels

1. **Read-Only Mode** (`ODOO_YOLO=read`):
   - Allows all read operations (search, read, count)
   - Blocks all write operations (create, update, delete)
   - Safe for demos and testing
   - Shows "READ-ONLY" indicators in responses

2. **Full Access Mode** (`ODOO_YOLO=true`):
   - Allows ALL operations without restrictions
   - Full CRUD access to all models
   - **EXTREMELY DANGEROUS** - use only in isolated environments
   - Shows "FULL ACCESS" warnings in responses

#### YOLO Mode Configuration

<details>
<summary>Read-Only YOLO Mode (safer for demos)</summary>

```json
{
  "mcpServers": {
    "odoo-demo": {
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "http://localhost:8069",
        "ODOO_USER": "admin",
        "ODOO_PASSWORD": "admin",
        "ODOO_DB": "demo",
        "ODOO_YOLO": "read"
      }
    }
  }
}
```
</details>

<details>
<summary>Full Access YOLO Mode (⚠️ use with extreme caution)</summary>

```json
{
  "mcpServers": {
    "odoo-test": {
      "command": "uvx",
      "args": ["mcp-server-odoo"],
      "env": {
        "ODOO_URL": "http://localhost:8069",
        "ODOO_USER": "admin",
        "ODOO_PASSWORD": "admin",
        "ODOO_DB": "test",
        "ODOO_YOLO": "true"
      }
    }
  }
}
```
</details>

#### When to Use YOLO Mode

✅ **Appropriate Uses:**
- Local development with test data
- Quick demos with non-sensitive data
- Testing MCP clients before installing the MCP module
- Prototyping in isolated environments

❌ **Never Use For:**
- Production environments
- Instances with real customer data
- Shared development servers
- Any environment with sensitive information

#### YOLO Mode Security Notes

- Connects directly to Odoo's standard XML-RPC endpoints
- Bypasses all MCP access controls and model restrictions
- No rate limiting is applied
- All operations are logged but not restricted
- Model listing shows 200+ models instead of just enabled ones

## Usage Examples

Once configured, you can ask Claude:

**Search & Retrieve:**
- "Show me all customers from Spain"
- "Find products with stock below 10 units"
- "List today's sales orders over $1000"
- "Search for unpaid invoices from last month"
- "Count how many active employees we have"
- "Show me the contact information for Microsoft"

**Create & Manage:**
- "Create a new customer contact for Acme Corporation"
- "Add a new product called 'Premium Widget' with price $99.99"
- "Create a calendar event for tomorrow at 2 PM"
- "Update the phone number for customer John Doe to +1-555-0123"
- "Change the status of order SO/2024/001 to confirmed"
- "Delete the test contact we created earlier"

## Available Tools

### `search_records`
Search for records in any Odoo model with filters.

```json
{
  "model": "res.partner",
  "domain": [["is_company", "=", true], ["country_id.code", "=", "ES"]],
  "fields": ["name", "email", "phone"],
  "limit": 10
}
```

**Field Selection Options:**
- Omit `fields` or set to `null`: Returns smart selection of common fields
- Specify field list: Returns only those specific fields
- Use `["__all__"]`: Returns all fields (use with caution)

### `get_record`
Retrieve a specific record by ID.

```json
{
  "model": "res.partner",
  "record_id": 42,
  "fields": ["name", "email", "street", "city"]
}
```

**Field Selection Options:**
- Omit `fields` or set to `null`: Returns smart selection of common fields with metadata
- Specify field list: Returns only those specific fields
- Use `["__all__"]`: Returns all fields without metadata

### `list_models`
List all models enabled for MCP access.

```json
{}
```

### `list_resource_templates`
List available resource URI templates and their patterns.

```json
{}
```

### `create_record`
Create a new record in Odoo.

```json
{
  "model": "res.partner",
  "values": {
    "name": "New Customer",
    "email": "customer@example.com",
    "is_company": true
  }
}
```

### `update_record`
Update an existing record.

```json
{
  "model": "res.partner",
  "record_id": 42,
  "values": {
    "phone": "+1234567890",
    "website": "https://example.com"
  }
}
```

### `delete_record`
Delete a record from Odoo.

```json
{
  "model": "res.partner",
  "record_id": 42
}
```

### Smart Field Selection

When you omit the `fields` parameter (or set it to `null`), the server automatically selects the most relevant fields for each model using a scoring algorithm:

- **Essential fields** like `id`, `name`, `display_name`, and `active` are always included
- **Business-relevant fields** (state, amount, email, phone, partner, etc.) are prioritized
- **Technical fields** (message threads, activity tracking, website metadata) are excluded
- **Expensive fields** (binary, HTML, large text, computed non-stored) are skipped

The default limit is 15 fields per request. Responses include metadata showing which fields were returned and how many total fields are available. You can adjust the limit with `ODOO_MCP_MAX_SMART_FIELDS` or bypass it entirely with `fields: ["__all__"]`.

## Resources

The server also provides direct access to Odoo data through resource URIs:

| URI Pattern | Description |
|------------|-------------|
| `odoo://{model}/record/{id}` | Retrieve a specific record by ID |
| `odoo://{model}/search` | Search records with default settings (first 10 records) |
| `odoo://{model}/count` | Count all records in a model |
| `odoo://{model}/fields` | Get field definitions and metadata for a model |

**Examples:**
- `odoo://res.partner/record/1` — Get partner with ID 1
- `odoo://product.product/search` — List first 10 products
- `odoo://res.partner/count` — Count all partners
- `odoo://product.product/fields` — Show all fields for products

> **Note:** Resource URIs don't support query parameters (like `?domain=...`). For filtering, pagination, and field selection, use the `search_records` tool instead.

## How It Works

```
AI Assistant (Claude, Copilot, etc.)
        ↓ MCP Protocol (stdio or HTTP)
   mcp-server-odoo
        ↓ XML-RPC
   Odoo Instance
```

The server translates MCP tool calls into Odoo XML-RPC requests. It handles authentication, access control, field selection, data formatting, and error handling — presenting Odoo data in an LLM-friendly hierarchical text format.

## Security

- Always use HTTPS in production environments
- Keep your API keys secure and rotate them regularly
- Configure model access carefully - only enable necessary models
- The MCP module respects Odoo's built-in access rights and record rules
- Each API key is linked to a specific user with their permissions

## Troubleshooting

<details>
<summary>Connection Issues</summary>

If you're getting connection errors:
1. Verify your Odoo URL is correct and accessible
2. Check that the MCP module is installed: visit `https://your-odoo.com/mcp/health`
3. Ensure your firewall allows connections to Odoo
</details>

<details>
<summary>Authentication Errors</summary>

If authentication fails:
1. Verify your API key is active in Odoo
2. Check that the user has appropriate permissions
3. Try regenerating the API key
4. For username/password auth, ensure 2FA is not enabled
</details>

<details>
<summary>Model Access Errors</summary>

If you can't access certain models:
1. Go to Settings > MCP Server > Enabled Models in Odoo
2. Ensure the model is in the list and has appropriate permissions
3. Check that your user has access to that model in Odoo's security settings
</details>

<details>
<summary>"spawn uvx ENOENT" Error</summary>

This error means UV is not installed or not in your PATH:

**Solution 1: Install UV** (see Installation section above)

**Solution 2: macOS PATH Issue**
Claude Desktop on macOS doesn't inherit your shell's PATH. Try:
1. Quit Claude Desktop completely (Cmd+Q)
2. Open Terminal
3. Launch Claude from Terminal:
   ```bash
   open -a "Claude"
   ```

**Solution 3: Use Full Path**
Find UV location and use full path:
```bash
which uvx
# Example output: /Users/yourname/.local/bin/uvx
```

Then update your config:
```json
{
  "command": "/Users/yourname/.local/bin/uvx",
  "args": ["mcp-server-odoo"]
}
```
</details>

<details>
<summary>Database Configuration Issues</summary>

If you see "Access Denied" when listing databases:
- This is normal - some Odoo instances restrict database listing for security
- Make sure to specify `ODOO_DB` in your configuration
- The server will use your specified database without validation

Example configuration:
```json
{
  "env": {
    "ODOO_URL": "https://your-odoo.com",
    "ODOO_API_KEY": "your-key",
    "ODOO_DB": "your-database-name"
  }
}
```
Note: `ODOO_DB` is required if database listing is restricted on your server.
</details>

<details>
<summary>"SSL: CERTIFICATE_VERIFY_FAILED" Error</summary>

This error occurs when Python cannot verify SSL certificates, often on macOS or corporate networks.

**Solution**: Add SSL certificate path to your environment configuration:

```json
{
  "env": {
    "ODOO_URL": "https://your-odoo.com",
    "ODOO_API_KEY": "your-key",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem"
  }
}
```

This tells Python where to find the system's SSL certificate bundle for HTTPS connections. The path `/etc/ssl/cert.pem` is the standard location on most systems.
</details>

<details>
<summary>Debug Mode</summary>

Enable debug logging for more information:

```json
{
  "env": {
    "ODOO_URL": "https://your-odoo.com",
    "ODOO_API_KEY": "your-key",
    "ODOO_MCP_LOG_LEVEL": "DEBUG"
  }
}
```
</details>

## Development

<details>
<summary>Running from source</summary>

```bash
# Clone the repository
git clone https://github.com/ivnvxd/mcp-server-odoo.git
cd mcp-server-odoo

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest --cov

# Run the server
python -m mcp_server_odoo

# Check version
python -m mcp_server_odoo --version
```
</details>

<details>
<summary>Testing with MCP Inspector</summary>

```bash
# Using uvx
npx @modelcontextprotocol/inspector uvx mcp-server-odoo

# Using local installation
npx @modelcontextprotocol/inspector python -m mcp_server_odoo
```
</details>

## Testing

### Transport Tests

You can test both stdio and streamable-http transports to ensure they're working correctly:

```bash
# Run comprehensive transport tests
python tests/run_transport_tests.py
```

This will test:
- **stdio transport**: Basic initialization and communication
- **streamable-http transport**: HTTP endpoint, session management, and tool calls

### Unit Tests

For complete testing including unit and integration tests:

```bash
# Run all tests
uv run pytest --cov

# Run specific test categories
uv run pytest tests/test_tools.py -v
uv run pytest tests/test_server_foundation.py -v
```

## License

This project is licensed under the Mozilla Public License 2.0 (MPL-2.0) - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are very welcome! Please see the [CONTRIBUTING](CONTRIBUTING.md) guide for details.

## Support

Thank you for using this project! If you find it helpful and would like to support my work, kindly consider buying me a coffee. Your support is greatly appreciated!

<a href="https://www.buymeacoffee.com/ivnvxd" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

And do not forget to give the project a star if you like it! :star: