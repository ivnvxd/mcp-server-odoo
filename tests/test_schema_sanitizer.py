from mcp_server_odoo.schema_sanitizer import sanitize_schema


def test_sanitize_schema_basic():
    schema = {"type": "string"}
    assert sanitize_schema(schema) == {"type": "string"}


def test_sanitize_schema_array_type():
    schema = {"type": ["string", "null"]}
    assert sanitize_schema(schema) == {"type": "string"}


def test_sanitize_schema_anyof():
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert sanitize_schema(schema) == {"type": "string"}


def test_sanitize_schema_nested():
    schema = {
        "type": "object",
        "properties": {
            "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "age": {"type": ["integer", "null"]},
        },
    }
    expected = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    assert sanitize_schema(schema) == expected


def test_sanitize_schema_complex_anyof():
    schema = {
        "description": "A field",
        "anyOf": [{"type": "string", "format": "email"}, {"type": "null"}],
    }
    expected = {"description": "A field", "type": "string", "format": "email"}
    assert sanitize_schema(schema) == expected
