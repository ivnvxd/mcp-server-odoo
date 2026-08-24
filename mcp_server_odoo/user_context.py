"""Personalized session-context block for MCP instructions.

Builds a compact plain-text summary of the connected user's identity,
timezone and company scope, plus fixed UTC datetime-handling guidance.
Spec-compliant MCP clients inject it into the model context on connect
(``initialize.instructions``), and the ``get_current_context`` tool
returns the same block with structured data.
"""

from typing import Any, Dict

from .logging_config import get_logger
from .odoo_connection import OdooConnection

logger = get_logger(__name__)

# Always-safe fallback: the UTC datetime guidance never depends on user
# state, so it is served even when the personalized block cannot be built
# (e.g. standard mode where res.users is not an MCP-enabled model).
UTC_DATETIME_GUIDANCE = (
    "Datetime handling:\n"
    "- All datetimes stored and returned by Odoo are in UTC.\n"
    "- Provide datetimes to tools in UTC.\n"
    "- Convert to the user's timezone only for display."
)


# Every code point str.splitlines() treats as a line break: CR/LF plus the
# Unicode separators (NEL U+0085, LS U+2028, PS U+2029) and the vertical
# whitespace controls (VT, FF, FS, GS, RS).
_LINE_BREAK_TRANSLATION = dict.fromkeys(map(ord, "\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"), " ")

# Cap for the allowed-companies instructions line: a many-company user would
# otherwise bloat ``initialize.instructions`` in every session. Formatting
# only: the structured ``allowed_companies`` data stays complete.
MAX_LISTED_COMPANIES = 10


def _one_line(value: Any) -> str:
    """Collapse line breaks in an interpolated value to single spaces.

    User-editable fields (display name, login, company name) are injected
    verbatim into the plain-text context block that clients feed to the LLM
    as instructions. Stripping embedded line breaks — including the Unicode
    separators U+2028/U+2029/U+0085, which some renderers treat as newlines —
    stops a crafted value from forging extra context lines (prompt injection
    into the caller's own session).
    """
    return str(value).translate(_LINE_BREAK_TRANSLATION)


def get_user_context_data(connection: OdooConnection) -> Dict[str, Any]:
    """Read the connected user's session context.

    Raises when the ``res.users`` read fails; a failing company-name read
    degrades to an empty ``allowed_companies`` instead (the rest of the
    context was already read and stays useful).

    Returns a dict with ``user_name``, ``login``, ``timezone`` (None when
    unset), ``company_id``, ``company_name``, and ``allowed_companies`` — a
    list of ``{"id", "name"}`` dicts populated only when the user can act
    in more than one company (resolved via one extra ``res.company`` read).
    """
    user = connection.read(
        "res.users",
        [connection.uid],
        ["name", "login", "tz", "company_id", "company_ids"],
    )[0]
    # A many2one arrives over XML-RPC as [id, display_name] (False when unset)
    company = user.get("company_id") or [None, ""]
    data: Dict[str, Any] = {
        "user_name": user.get("name") or "",
        "login": user.get("login") or "",
        "timezone": user.get("tz") or None,
        "company_id": company[0],
        "company_name": company[1],
        "allowed_companies": [],
    }
    company_ids = user.get("company_ids") or []
    if len(company_ids) > 1:
        # Separate failure domain: the res.company read can be denied on its
        # own (e.g. standard mode with res.users MCP-enabled but res.company
        # not) — keep the already-read user context and drop only this list.
        try:
            companies = connection.read("res.company", company_ids, ["display_name"])
            data["allowed_companies"] = [
                {"id": c["id"], "name": c["display_name"]} for c in companies
            ]
        except Exception as e:
            logger.warning(f"Could not resolve allowed companies for MCP user context: {e}")
    return data


def format_user_context(data: Dict[str, Any]) -> str:
    """Render the context dict as the plain-text instructions block."""
    timezone_line = (
        _one_line(data["timezone"]) if data["timezone"] else "UTC (user has no timezone set)"
    )
    lines = [
        "You are connected to Odoo via MCP as:",
        f"- User: {_one_line(data['user_name'])} (login: {_one_line(data['login'])})",
        f"- Timezone: {timezone_line}",
    ]
    # A user without a company (company_id False/None) must not render
    # "- Active company:  (ID: None)" — skip the line entirely.
    if data["company_id"]:
        lines.append(
            f"- Active company: {_one_line(data['company_name'])} (ID: {data['company_id']})"
        )
    companies = data["allowed_companies"]
    if len(companies) > 1:
        names = ", ".join(
            f"{_one_line(c['name'])} (ID: {c['id']})" for c in companies[:MAX_LISTED_COMPANIES]
        )
        if len(companies) > MAX_LISTED_COMPANIES:
            names += f" … and {len(companies) - MAX_LISTED_COMPANIES} more"
        lines.append(f"- Allowed companies: {names}")
    lines.append("")
    lines.append(UTC_DATETIME_GUIDANCE)
    return "\n".join(lines)


def build_user_context(connection: OdooConnection) -> str:
    """Build the personalized user-context block for ``initialize.instructions``.

    Best-effort: on any failure it logs and falls back to the always-safe
    UTC guidance so ``initialize`` still yields useful instructions.
    """
    try:
        return format_user_context(get_user_context_data(connection))
    except Exception as e:
        logger.error(f"Error building MCP user context: {e}")
        return UTC_DATETIME_GUIDANCE
