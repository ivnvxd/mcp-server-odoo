"""Name-based credential detection shared by the tool and resource handlers.

Best-effort, name-only defense-in-depth for the *bulk* read paths: it de-scores
credential-named fields out of the smart-default selection and strips them from
an ``["__all__"]`` read. Explicitly-named fields are always honored — Odoo
field-level ``groups=`` is the real ACL, not this heuristic.
"""

from typing import Any, Dict, List, Sequence, Union

# Single-segment credential markers: a field is sensitive when one of these is
# its FINAL `_`-delimited segment (`password`, `user_password`,
# `webhook_secret`, `client_secret`, `apikey`, `auth_token`, and `*_pass` such
# as Odoo core's `ir.mail_server.smtp_pass`). Matched only as the last segment,
# so a benign descriptor whose name merely *starts* with the word
# (`password_expiry_date`) or carries the marker mid-name ahead of a metadata
# suffix (`access_token_expiry`) is not caught. The final segment is also
# matched with trailing digits stripped (`password2`) — see
# is_sensitive_field_name. Plural forms (`secrets`, `api_tokens`) are
# deliberately NOT normalized to their singular: a withholding heuristic must
# minimize false positives, and plural-named fields are usually benign
# config/counter fields (`max_tokens`, `num_tokens`), not credential values.
# `token` is a marker
# here because a trailing `_token` almost always names an access/refresh/
# verification credential; `key` is deliberately NOT a single-segment marker
# (too common as a benign suffix — `sort_key`), so it only counts inside the
# credential compounds below. Known accepted false positive: Odoo core's
# `purchase.report.delay_pass` ("Days to Receive") is withheld from bulk
# reads by the `pass` marker — accepted; explicit requests and aggregations
# are unaffected.
# The no-underscore compounds (`apikey`, `privatekey`, `secretkey`,
# `accesskey`) mirror SENSITIVE_MARKER_SEQUENCES for names written without a
# separator. Arbitrary compounds (`webhooksecret`, ...) are deliberately NOT
# chased — that enumeration has no end, and this heuristic is only
# defense-in-depth.
SENSITIVE_FIELD_MARKERS = (
    "password",
    "passwd",
    "pass",
    "secret",
    "apikey",
    "privatekey",
    "secretkey",
    "accesskey",
    "token",
    # OAuth refresh tokens (`*_rtoken`), stored WebAuthn credentials
    # (`*_passkey`), password salts, one-time-password secrets and PIN codes
    # (`hr.employee.pin`, POS `pin`). Scanned against 2475 distinct field
    # names on a stock Odoo 19 database: `pin` was the only match and it is a
    # real credential, so none of these cost a false positive there.
    "rtoken",
    "passkey",
    "salt",
    "otp",
    "pin",
    # ECPay's AES signing pair (`l10n_tw_edi_ecpay_hashkey` /
    # `...hashIV`). `hashkey` is just the no-separator spelling of the
    # ("hash", "key") compound below, and `hashiv` is its counterpart —
    # splitting them would half-cover one module. Scanning every
    # `x = fields.Y(` definition in Odoo 19 core + enterprise (14708 distinct
    # names) found these two as the ONLY no-separator credential compounds,
    # so this is not the start of an open-ended enumeration.
    "hashkey",
    "hashiv",
)

# Multi-segment `*_key` credential markers: matched as the CONSECUTIVE
# trailing segments of the name (so `openai_api_key` matches `api_key` and
# `stripe_secret_key` matches `secret_key`, but `api_key_expiry_date` does not
# — the compound must end at the last segment). This is the only way a `key`
# field is flagged — bare `key` is not a single-segment marker (`sort_key`
# is benign), so it counts solely inside these credential compounds.
SENSITIVE_MARKER_SEQUENCES = (
    ("api", "key"),
    ("private", "key"),
    ("secret", "key"),
    ("access", "key"),
    # Payment/webhook signing material: `*_hmac_key`, `*_signature_key`,
    # `*_transaction_key`, `*_hash_key`, `*_encryption_key`, `*_signing_key`.
    ("hmac", "key"),
    ("signature", "key"),
    ("transaction", "key"),
    ("hash", "key"),
    ("encryption", "key"),
    ("signing", "key"),
)

# A leading boolean-flag segment (`is_secret`) or a trailing `_id`/`_ids`
# relational reference (`token_id`, `api_key_ids`) never holds a credential
# value. _ids is guarded too: an x2many holds record ids, not secret material.
_BOOLEAN_FLAG_PREFIXES = ("is", "has", "can")

# A trailing hash/value/digest segment names the *representation* of what
# precedes it, not a new concept — `password_hash`, `secret_value` and
# `api_key_digest` hold credential material just like their base names. One
# such suffix is popped (after the digit pops) before the marker checks, so
# the checks see the real credential tail; `commit_hash`/`amount_value` stay
# benign because their remaining tail is not a credential marker.
_CREDENTIAL_REPRESENTATION_SUFFIXES = ("hash", "value", "digest")


def is_sensitive_field_name(field_name: str) -> bool:
    """Whether `field_name` looks like it holds a credential value.

    A field is sensitive when, after the suffix normalization below, its name
    ends in a credential marker segment or a credential `*_key` compound;
    `_id`/`_ids` references and `is_`/`has_`/`can_` boolean flags never are.
    The exact matching rules are documented on the SENSITIVE_* constants
    above. Used only on the bulk read paths — callers always honor
    explicitly-named fields.
    """
    segments = field_name.lower().split("_")
    if segments[-1] in ("id", "ids") or segments[0] in _BOOLEAN_FLAG_PREFIXES:
        return False
    if all(segment == "" for segment in segments):
        return False
    # Trailing all-digit segments (`password_2`, `api_key_2`) are copy
    # suffixes, and a trailing underscore leaves an EMPTY final segment
    # (`password_`, `api_key_`, `pass_` — the PEP 8 spelling for names that
    # would otherwise collide with a Python keyword). Both are dropped so the
    # marker checks see the real final segment instead of matching nothing.
    while segments and (segments[-1].isdigit() or segments[-1] == ""):
        segments.pop()
    if not segments:
        return False
    if segments[-1] in _CREDENTIAL_REPRESENTATION_SUFFIXES and len(segments) > 1:
        segments.pop()
    last = segments[-1]
    candidates = {last, last.rstrip("0123456789")}
    for sequence in SENSITIVE_MARKER_SEQUENCES:
        width = len(sequence)
        # Tail-only: the compound must end at the last segment (mirroring the
        # single-marker rule's keying on segments[-1]), so `openai_api_key` is
        # flagged but `api_key_expiry_date` is trailing metadata, not a secret.
        head = tuple(segments[-width:-1])
        if any(head + (candidate,) == sequence for candidate in candidates):
            return True
    return any(candidate in SENSITIVE_FIELD_MARKERS for candidate in candidates)


def withheld_note(withheld: Union[int, Sequence[str]]) -> str:
    """Advisory line for a bulk read that withheld credential-like fields.

    Single source of the wording shared by both surfaces — the tools' result
    note and the resources' formatted-text trailer. Pass the withheld field
    names to show them (tools) or a bare count to stay compact (resources).
    Callers may add surface-specific framing (brackets, newlines) around the
    sentence, but not rephrase it.
    """
    label = (
        f"{withheld} credential-like field(s) withheld"
        if isinstance(withheld, int)
        else f"Credential-like field(s) withheld: {', '.join(withheld)}"
    )
    return f"{label} — request explicitly by name to include"


def strip_sensitive_fields(record: Dict[str, Any]) -> List[str]:
    """Drop credential-named fields from a read result in place.

    Applied only on the *bulk* read paths — the `["__all__"]` sentinel and the
    smart-default fallback that reads all fields — so a field named like a
    credential (`*_api_key`, `*password`, `webhook_secret` ...) is not surfaced
    by a caller that did not ask for it by name. An explicitly-named field is
    honored (never stripped). Mutates `record` in place; returns the
    removed field names (sorted).
    """
    withheld = sorted(name for name in record if is_sensitive_field_name(name))
    for name in withheld:
        del record[name]
    return withheld
