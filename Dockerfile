FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mcp_server_odoo/ mcp_server_odoo/
RUN uv pip install --system --no-cache .

FROM python:3.12-slim-bookworm

RUN useradd -m -u 1000 mcp

COPY --from=builder --chown=mcp:mcp /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder --chown=mcp:mcp /usr/local/bin/mcp-server-odoo /usr/local/bin/

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

LABEL org.opencontainers.image.title="mcp-server-odoo"
LABEL org.opencontainers.image.description="MCP Server for Odoo ERP — connect AI assistants to Odoo via XML-RPC"
LABEL org.opencontainers.image.url="https://github.com/ivnvxd/mcp-server-odoo"
LABEL org.opencontainers.image.source="https://github.com/ivnvxd/mcp-server-odoo"
LABEL org.opencontainers.image.licenses="MPL-2.0"

USER mcp

CMD ["mcp-server-odoo", "--transport", "streamable-http", "--host", "0.0.0.0"]
