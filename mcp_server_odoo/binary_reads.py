"""Record reads that never pull binary payloads.

Up to Odoo 19, reading with ``bin_size`` turns a populated binary into a short
size placeholder (e.g. ``"12.50 Kb"``) that the callers swap for an ``odoo://``
URI. Odoo 20 dropped ``bin_size``: ``read()`` returns every populated binary as
``{content, size, filename}``. Shared by the tools and the text resources, so
neither pulls a payload into a record read.
"""

import logging
from typing import Any, Dict, List, Optional

from .uri_schema import BINARY_FIELD_TYPES, URIValidationError, build_binary_uri

logger = logging.getLogger(__name__)


def reads_without_bin_size(connection: Any) -> bool:
    """True when the server ignores ``bin_size`` (Odoo 20 and later).

    An unknown version (``None``, or a mock in unit tests) takes the
    ``bin_size`` path; callers still recognize the 20 payload shape there.
    """
    major = connection.get_major_version()
    return isinstance(major, int) and major >= 20


def read_without_binary_payloads(
    connection: Any, model: str, ids: List[int], fields: Optional[List[str]]
) -> List[Dict[str, Any]]:
    """``read(ids, fields)`` with binaries as URIs or ``False``, never payloads (blocking).

    On Odoo 20 and later the binaries stay out of the read. One ``search``
    per stored binary field finds the populated records. Odoo 20 cannot
    search a non-stored binary (``avatar_128``), so it always gets its URI and
    the resource read decides whether it holds content.
    """
    if not reads_without_bin_size(connection):
        return connection.read(model, ids, fields, {"bin_size": True})
    try:
        fields_info = connection.fields_get(model)
    except Exception as e:
        logger.warning(f"Could not get field metadata for {model}; reading binaries as-is: {e}")
        return connection.read(model, ids, fields, {"bin_size": True})

    binary_names = {
        name for name, meta in fields_info.items() if (meta or {}).get("type") in BINARY_FIELD_TYPES
    }
    if fields is not None:
        binary_names &= set(fields)
    if not binary_names:
        return connection.read(model, ids, fields, {"bin_size": True})

    requested = fields if fields is not None else list(fields_info)
    readable = [name for name in requested if name not in binary_names] or ["id"]
    records = connection.read(model, ids, readable, {"bin_size": True})

    for name in binary_names:
        # Absent metadata means stored, per fields_get's own default
        if (fields_info.get(name) or {}).get("store", True):
            # active_test=False: an archived record keeps its binaries
            populated = set(
                connection.search(
                    model,
                    [["id", "in", ids], [name, "!=", False]],
                    context={"active_test": False},
                )
            )
        else:
            populated = set(ids)
        for record in records:
            rid = record.get("id")
            if rid not in populated:
                record[name] = False
                continue
            try:
                record[name] = build_binary_uri(model, rid, name)
            except URIValidationError:
                # A field name the URI grammar rejects has no servable URI
                logger.debug(f"No binary URI for {model}.{name}; leaving it out")
    return records
