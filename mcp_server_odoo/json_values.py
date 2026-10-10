"""Clean values of ``json`` fields before they reach a client.

Odoo stores a value it cannot encode, such as a function, as its Python repr,
e.g. ``"<function validate at 0x7f...>"``. The repr carries a memory address
and means nothing to a client. Shared by the tool and resource handlers.
"""

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_OBJECT_REPR_RE = re.compile(r"^<[^<>]+ at 0x[0-9a-fA-F]+>$")


def scrub_object_reprs(value: Any) -> Any:
    """Replace object reprs inside a json field value with None."""
    if isinstance(value, dict):
        return {key: scrub_object_reprs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_object_reprs(item) for item in value]
    if isinstance(value, str) and _OBJECT_REPR_RE.match(value):
        return None
    return value


def scrub_json_fields(connection: Any, model: str, records: List[Dict[str, Any]]) -> None:
    """Replace object reprs in the json fields of ``records`` with None
    (in place, blocking).

    Without field metadata the values pass through unchanged.
    """
    try:
        fields_info = connection.fields_get(model)
        names = [name for name, meta in fields_info.items() if (meta or {}).get("type") == "json"]
    except Exception as e:
        logger.debug(f"Could not get field metadata for {model}; json values unchanged: {e}")
        return
    for record in records:
        for name in names:
            if record.get(name):
                record[name] = scrub_object_reprs(record[name])
