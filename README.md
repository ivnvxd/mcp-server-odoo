# MCP Server for Odoo

A Model Context Protocol (MCP) server implementation for Odoo. This package allows AI models to interact with Odoo data and functionality through a standardized MCP interface, enabling AI assistants like Claude to search, retrieve, and eventually manipulate Odoo data.

## Features

- Expose Odoo data as MCP resources through a standardized interface
- Search functionality with domain filters and complex queries
- Pagination and summarization for large datasets
- Smart formatting of Odoo data optimized for LLM consumption
- Progressive disclosure of data with URIs for detailed access
- Follows existing Odoo permission model and security constraints
- Easy configuration through environment variables or CLI arguments
- Auto-detection of default database when not specified

## Installation

```bash
pip install mcp-server-odoo
# or
uv install mcp-server-odoo
```

## Configuration

Configure the MCP server using environment variables:

- `ODOO_URL`: URL of your Odoo instance
- `ODOO_DB`: Database name (optional - will auto-detect if not specified)
- `ODOO_MCP_TOKEN`: Authentication token from the Odoo MCP module
- `ODOO_USERNAME`: Odoo username
- `ODOO_PASSWORD`: Odoo password
- `ODOO_MCP_LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `ODOO_MCP_DEFAULT_LIMIT`: Default record limit (default: 50)
- `ODOO_MCP_MAX_LIMIT`: Maximum allowed record limit (default: 100)

Or use command-line arguments:

```bash
mcp-server-odoo --url https://example.odoo.com --token abc123
```

### Authentication

The MCP server supports two authentication methods:

1. **Token-based authentication** (recommended):
   - Generate a token in the Odoo MCP module settings
   - Set the token in environment variables or use the `--token` argument
   - Secure and maintains user permissions from Odoo

2. **Username/password authentication**:
   - Use when token authentication is not available
   - Specify using `ODOO_USERNAME` and `ODOO_PASSWORD` environment variables

All operations through the MCP server inherit the permissions of the authenticated Odoo user, respecting existing Odoo security constraints.

### Database Auto-detection

If the `ODOO_DB` environment variable or `--db` argument is not provided, the server will attempt to detect the default database for the Odoo instance automatically. This is particularly useful for single-database Odoo installations.

The auto-detection process:

1. Tries to query the database list endpoint (`/web/database/list`)
2. If that fails, attempts to extract the database from the login page

If auto-detection fails, you'll need to specify the database explicitly.

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

### Parameters

- `domain` - Odoo domain expression (URL-encoded)
- `fields` - Comma-separated list of fields to return
- `limit` - Maximum number of records to return
- `offset` - Pagination offset
- `order` - Sorting criteria

### Example URIs

```
odoo://res.partner/record/42
odoo://product.product/search?domain=[('type','=','product')]&limit=10
odoo://sale.order/browse?ids=1,2,3,4
odoo://res.partner/count?domain=[('country_id.code','=','US')]
odoo://product.template/fields
```

### Data Representation

Resources are returned in a human-readable text format, optimized for LLM consumption:

```
Resource: res.partner/record/42
Name: Deco Addict
Email: deco.addict82@example.com
Phone: +1 555-123-4567
Address:
  Street: 77 Santa Barbara Rd
  City: Pleasant Hill
  ZIP: 94523
  Country: United States
Related Contacts: [odoo://res.partner/search?domain=[('parent_id','=',42)]]
Recent Orders: [odoo://sale.order/search?domain=[('partner_id','=',42)]&limit=5]
```

For large datasets, smart summarization is provided:

```
Search Results: res.partner (1247 total matches)
Showing: Records 1-15 of 1247
Summary:
- Customer type: 923 companies, 324 individuals
- By country: US (412), France (287), Germany (203), Other (345)
- Active status: 1198 active, 49 archived

Records:
1. Deco Addict (Company) - US, Active [odoo://res.partner/record/1]
...

Refinement options:
- Filter by country: odoo://res.partner/search?domain=[('country_id.code','=','US')]
- Next page: odoo://res.partner/search?offset=15&limit=15
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

## Example AI Assistant Interactions

Once configured, you can interact with your Odoo instance using natural language. Here are some examples:

**Basic Information Retrieval**

- "Find all customers in California who purchased something last month"
- "Show me the top 5 products by sales volume this quarter"
- "What's the payment status of invoice INV/2023/00423?"

**Complex Data Analysis**

- "Summarize our sales performance by region for Q1 2023"
- "Compare inventory levels across all warehouses"
- "Show me customers who haven't ordered in the last 6 months"

**Contextual Follow-ups**

- "How many of them are in the technology sector?"
- "Which one has the highest lifetime value?"
- "Can you give me more details about this customer?"

The AI assistant will translate these natural language queries into appropriate MCP resource requests to retrieve and present the relevant information.

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

## Odoo Module Integration

This Python package is designed to work alongside the `mcp_server` Odoo module. The complete system consists of two components:

1. **Odoo Module (`mcp_server`)**
   - Installed within the Odoo environment
   - Handles configuration and security settings
   - Manages model access permissions
   - Provides authentication tokens for the Python package

2. **Python Package (`mcp-server-odoo`)**
   - This package - implements the MCP protocol
   - Connects to Odoo via XML-RPC
   - Uses stdio transport for MCP client communication
   - Handles resource formatting and pagination

### Integration Flow

```
┌──────────────────┐      ┌─────────────────────┐      ┌────────────────┐
│                  │      │                     │      │                │
│  MCP Client      │◄────►│  mcp-server-odoo    │◄────►│  Odoo Instance │
│  (Claude/etc)    │ stdio│  (Python Package)   │XML-RPC│  with mcp_server │
│                  │      │                     │      │                │
└──────────────────┘      └─────────────────────┘      └────────────────┘
```

The Odoo module must be installed in your Odoo instance before using this Python package. For information on installing and configuring the Odoo module, please refer to the [mcp_server module documentation](https://github.com/ivnvxd/odoo-apps/tree/main/mcp_server).

## Support

Thank you for using this project! If you find it helpful and would like to support my work, kindly consider buying me a coffee. Your support is greatly appreciated!

<a href="https://www.buymeacoffee.com/ivnvxd" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

And do not forget to give the project a star if you like it! :star:

## License

This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0. If a copy of the MPL was not distributed with this file, You can obtain one at <http://mozilla.org/MPL/2.0/>.

See the [LICENSE](LICENSE) file for details.
