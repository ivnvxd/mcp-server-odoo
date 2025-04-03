# MCP Server for Odoo

A Model Context Protocol (MCP) server implementation for Odoo. This package allows AI models to interact with Odoo data and functionality through a standardized MCP interface.

## Features

- Expose Odoo data as MCP resources
- Search functionality with domain filters
- Pagination and summarization for large datasets
- Easy configuration through environment variables or CLI arguments

## Installation

```bash
pip install mcp-server-odoo
# or
uv install mcp-server-odoo
```

## Configuration

Configure the MCP server using environment variables:

- `ODOO_URL`: URL of your Odoo instance
- `ODOO_DB`: Database name
- `ODOO_MCP_TOKEN`: Authentication token from the Odoo MCP module
- `ODOO_MCP_LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `ODOO_MCP_DEFAULT_LIMIT`: Default record limit (default: 20)
- `ODOO_MCP_MAX_LIMIT`: Maximum allowed record limit (default: 100)

Or use command-line arguments:

```bash
mcp-server-odoo --url https://example.odoo.com --db mydb --token abc123
```

## Resource URI Format

The Odoo MCP server uses the following URI format for resources:

```
odoo://{model}/{operation}?{parameters}
```

### Operations

- `record/{id}` - Fetch a specific record
- `search` - Search for records using domain
- `browse` - Retrieve multiple records by IDs
- `count` - Count matching records
- `fields` - Get field definitions

### Example URIs

```
odoo://res.partner/record/42
odoo://product.product/search?domain=[('type','=','product')]&limit=10
odoo://sale.order/browse?ids=1,2,3,4
odoo://res.partner/count?domain=[('country_id.code','=','US')]
odoo://product.template/fields
```

## Usage with Claude Desktop App

Add to your `mcp.conf`:

```json
{
  "odoo": {
    "command": "mcp-server-odoo",
    "args": []
  }
}
```

For more customization, you can use environment variables:

```json
{
  "odoo": {
    "command": "mcp-server-odoo",
    "args": [],
    "env": {
      "ODOO_URL": "https://example.odoo.com",
      "ODOO_DB": "mydb",
      "ODOO_MCP_TOKEN": "abc123"
    }
  }
}
```

## Development

```bash
# Clone the repository
git clone https://github.com/ivnvxd/mcp-server-odoo.git
cd mcp-server-odoo

# Install development dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run the development server with MCP Inspector
mcp dev mcp_server_odoo

# Install the server in Claude Desktop
mcp install mcp_server_odoo
```

## License
