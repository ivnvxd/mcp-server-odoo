"""Tool input schemas must pass the strictest MCP clients in use.

Clients validate ``tools/list`` with different JSON Schema subsets, and one bad
property makes them reject the tool or the whole server:

- Gemini / VertexAI reject unions with more than one real branch (issue #27).
- VS Code rejects an array without ``items`` (issue #139).
- llama.cpp and n8n choke on ``prefixItems``, type arrays and ``$ref``/``$defs``.

The schemas are read through the public listing and ``model_dump(by_alias=True)``
so that this test runs unchanged across mcp SDK versions.
"""

from typing import Any, Dict, Iterator, List, Set, Tuple
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.tools import OdooToolHandler

# (tool, property) pairs that break a rule today. Each entry must still break
# one, so the list shrinks as they are fixed. The typed parameters change
# empties it.
KNOWN_EXCEPTIONS: Set[Tuple[str, str]] = {
    ("search_records", "domain"),
    ("search_records", "fields"),
    ("aggregate_records", "domain"),
    ("call_model_method", "arguments"),
    ("call_model_method", "keyword_arguments"),
}


def _walk(schema: Any, path: str) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield every sub-schema dict with a readable path."""
    if not isinstance(schema, dict):
        return
    yield path, schema
    for key in ("properties", "$defs", "definitions"):
        for name, sub in (schema.get(key) or {}).items():
            yield from _walk(sub, f"{path}.{name}")
    for key in ("items", "additionalProperties", "not"):
        if isinstance(schema.get(key), dict):
            yield from _walk(schema[key], f"{path}[{key}]")
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for index, sub in enumerate(schema.get(key) or []):
            yield from _walk(sub, f"{path}[{key}:{index}]")


def _is_null_branch(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "null"


def schema_violations(schema: Dict[str, Any]) -> List[str]:
    """Rule violations of one tool input schema, as ``"<path>: <rule>"`` strings."""
    violations = []
    if schema.get("type") != "object":
        violations.append("$: top-level type is not object")
    for name, prop in (schema.get("properties") or {}).items():
        effective = [branch for branch in prop.get("anyOf", [prop]) if not _is_null_branch(branch)]
        if any(branch == {} for branch in effective) or prop == {}:
            violations.append(f"$.{name}: bare {{}} schema")
    for path, sub in _walk(schema, "$"):
        if isinstance(sub.get("type"), list):
            violations.append(f"{path}: type array")
        if sub.get("type") == "array" and "items" not in sub:
            violations.append(f"{path}: array without items")
        if "prefixItems" in sub:
            violations.append(f"{path}: prefixItems")
        if "$ref" in sub or "$defs" in sub or "definitions" in sub:
            violations.append(f"{path}: $ref or $defs")
        for key in ("anyOf", "oneOf"):
            branches = sub.get(key)
            if branches is not None:
                real = [b for b in branches if not _is_null_branch(b)]
                if len(real) > 1:
                    violations.append(f"{path}: {key} with {len(real)} non-null branches")
    return violations


def _property_of(violation: str) -> str:
    """``"$.domain[anyOf:0]: ..."`` -> ``"domain"``."""
    head = violation.split(":", 1)[0]
    return head[2:].split(".", 1)[0].split("[", 1)[0] if head.startswith("$.") else ""


@pytest.fixture
async def tool_schemas() -> Dict[str, Dict[str, Any]]:
    """Input schema of every tool, with every optional tool turned on."""
    config = OdooConfig(
        url="http://localhost:8069",
        username="admin",
        password="admin",
        database="test",
        yolo_mode="true",
        enable_method_calls=True,
    )
    app = MCPServer("schema-rules")
    OdooToolHandler(app, MagicMock(spec=OdooConnection), MagicMock(spec=AccessController), config)
    tools = await app.list_tools()
    return {
        dumped["name"]: dumped["inputSchema"]
        for dumped in (tool.model_dump(by_alias=True) for tool in tools)
    }


async def test_optional_tools_are_covered(tool_schemas):
    assert "call_model_method" in tool_schemas
    assert len(tool_schemas) >= 12


async def test_every_tool_schema_passes_the_client_rules(tool_schemas):
    unexpected = [
        f"{tool} {violation}"
        for tool, schema in sorted(tool_schemas.items())
        for violation in schema_violations(schema)
        if (tool, _property_of(violation)) not in KNOWN_EXCEPTIONS
    ]
    assert not unexpected, "\n".join(unexpected)


async def test_known_exceptions_still_break_a_rule(tool_schemas):
    """A fixed property must leave the exception list."""
    still_broken = {
        (tool, _property_of(violation))
        for tool, schema in tool_schemas.items()
        for violation in schema_violations(schema)
    }
    assert KNOWN_EXCEPTIONS <= still_broken, KNOWN_EXCEPTIONS - still_broken


@pytest.mark.parametrize(
    "prop,rule",
    [
        ({"type": "array"}, "array without items"),
        ({"type": ["string", "null"]}, "type array"),
        ({"type": "array", "prefixItems": [{"type": "string"}], "items": {}}, "prefixItems"),
        ({"anyOf": [{"type": "string"}, {"type": "array", "items": {}}]}, "non-null branches"),
        ({"$ref": "#/$defs/Domain"}, "$ref or $defs"),
        ({}, "bare {} schema"),
        ({"anyOf": [{}, {"type": "null"}]}, "bare {} schema"),
    ],
)
def test_each_rule_is_detected(prop, rule):
    schema = {"type": "object", "properties": {"p": prop}}

    assert any(rule in violation for violation in schema_violations(schema))


def test_nullable_single_branch_is_allowed():
    schema = {
        "type": "object",
        "properties": {
            "fields": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}
        },
    }

    assert schema_violations(schema) == []


def test_top_level_object_is_required():
    assert "$: top-level type is not object" in schema_violations({"properties": {}})
