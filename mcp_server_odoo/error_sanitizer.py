"""Error message sanitizer for Odoo MCP Server.

This module provides utilities to sanitize error messages before they are
returned to users, removing internal implementation details while maintaining
useful information for debugging.
"""

import re
from typing import Any, Dict, Optional


class ErrorSanitizer:
    """Sanitizes error messages to remove internal implementation details."""

    # True leak vectors that can never appear in legitimate business prose:
    # file references, traceback machinery, module/class internals, memory
    # addresses, database-driver internals. Applied by _scrub_internal_details
    # to extracted UserError/ValidationError/AccessError messages, and (as
    # part of PATTERNS_TO_REMOVE) by the full sanitize_message.
    _BUSINESS_SCRUB_PATTERNS = [
        # Python traceback machinery — the frame pattern must run BEFORE the
        # absolute-path pattern below empties the frame's quoted path (the
        # full-line match keys on it).
        (r"Traceback \(most recent call last\):", ""),
        (r'^\s*File "[^"]+", line \d+.*$', ""),
        # Absolute .py file paths
        (r"(/[^/\s]+)+/[^/\s]+\.py", ""),
        # Module paths — the odoo pattern is a deliberate blanket over every
        # "odoo.<something>:" shape (leading underscore allowed: Odoo 19 logs
        # via odoo._monkeypatches, odoo.orm, ...). Accepted tradeoff: a
        # hostname like "odoo.mycompany.com:" inside business prose gets
        # scrubbed too — rare and cosmetic, whereas enumerating module roots
        # trails reality with every Odoo release.
        (r"mcp_server_odoo\.[a-zA-Z_\.]+:", ""),
        (r"odoo\.[a-zA-Z_\.][\w.]*:", ""),
        # Class names
        (r"<class \'[^\']+\'>", ""),
        (r"MCPObjectController:", ""),
        (r"OdooConnectionError:", ""),
        # Memory addresses and object references
        (r"\s+at\s+0x[0-9a-fA-F]+", ""),
        (r"Object at\s+0x[0-9a-fA-F]+", "Object"),
        # Database driver internals
        (r"psycopg2\.[a-zA-Z_.]+:", ""),
        # Postgres diagnostics. These ride INSIDE business messages too —
        # Odoo wraps IntegrityError into a ValidationError — so they must be
        # stripped on the business path as well, not only by the traceback
        # reduction. The shapes below carry column names and live row values
        # and effectively never occur in author-written prose, unlike a bare
        # CONTEXT:/HINT: line (which a UserError may legitimately end with
        # and which is deliberately preserved).
        (r"(?im)^\s*DETAIL:\s*Key \([^)]*\)=\([^)]*\).*$", ""),
        (r"(?im)^\s*DETAIL:\s*Failing row contains.*$", ""),
        (r"(?i)Key \([^)]*\)=\([^)]*\)", ""),
        (r'(?i)constraint "[^"]+"', "constraint"),
        (r'(?i)unique index "[^"]+"', "unique index"),
        # Drop the orphan token a stripped constraint clause can leave behind
        (r"(?im)^\s*(unique\s+)?constraint\s*$", ""),
        # Deployment topology. A connection/DNS failure quotes the endpoint it
        # tried, so internal hostnames, private IPs and non-default ports ride
        # out to the client — including through the ERROR_MAPPINGS branch,
        # which is why these live in the BUSINESS list and not only in
        # PATTERNS_TO_REMOVE. Ordered scheme-URL first so its authority is
        # consumed before the bare host:port pattern sees it.
        (r'(?i)\b[a-z][a-z0-9+.-]*://[^\s\'"<>]+', "<url>"),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b", "<host>"),
        # A dotted name followed by :port. The TLD-shaped last label keeps
        # decimals ("1.5:30") and model references ("res.partner:write") out.
        (r"(?i)\b[a-z0-9][a-z0-9._-]*\.[a-z]{2,}:\d{1,5}\b", "<host>"),
        # Bracketed IPv6 authority, with or without a port
        (r"\[[0-9a-fA-F:]+\](?::\d{1,5})?", "<host>"),
        # host name quoted by libpq/DNS failures
        (r'(?i)(host\s+name|host)\s+"[^"]+"', r"\1 <host>"),
    ]

    # Full removal list for raw fault strings and tracebacks: the leak
    # vectors above plus patterns that can also match business prose — a
    # quoted filename ('attach file "myscript.py"'), a bare "line 3" ("You
    # cannot delete order line 3") or a stack-frame "in foo()" residue.
    # Once paths are stripped those leak nothing, so the defensive business
    # scrub must not apply them; sanitize_message (which may rewrite prose
    # anyway) still does. Real traceback frames inside business messages
    # stay covered by the ^File "...", line N and absolute-path patterns
    # above.
    PATTERNS_TO_REMOVE = _BUSINESS_SCRUB_PATTERNS + [
        # Quoted .py filenames
        (r'(File|file)\s*"[^"]+\.py"', "file"),
        # Line numbers
        (r",?\s*line\s+\d+", ""),
        # Stack frame residue
        (r"in\s+<[^>]+>", ""),
        (r"in\s+[a-zA-Z_]+\(\)", ""),
    ]

    # Marker identifying traceback-shaped messages
    # A real traceback frame: 'File "<path>", line <n>[, in <name>]'.
    _TRACEBACK_FRAME_RE = re.compile(r'^File "[^"]*", line \d+')

    TRACEBACK_MARKER = "Traceback (most recent call last)"

    # Shape of the line that starts the final exception message in a traceback
    _EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Violation)\b")

    # Specific error message mappings
    ERROR_MAPPINGS = {
        # Field errors
        r"Invalid field .+ in leaf": "Invalid field '{}' in search criteria",
        r"Field\s+(\w+)\s+does not exist": "Field '{}' does not exist on this model",
        r"Unknown field .+ in domain": "Unknown field '{}' in search criteria",
        # Model errors
        r"Model .+ does not exist": "Model '{}' is not available",
        r"Access denied on model": "You don't have permission to access this model",
        # Database constraint errors (constraint names / values are internal)
        r"duplicate key value violates unique constraint": (
            "A record with these values already exists"
        ),
        r"violates foreign key constraint": "The record is referenced by other records",
        r"violates not-null constraint": "A required value is missing",
        # Connection errors
        r"Failed to execute .+ on .+: (.+)": "Operation failed: {}",
        r"Connection refused": "Cannot connect to Odoo server",
        r"Operation timeout after \d+ seconds": "Request timed out",
        # Authentication errors
        r"Invalid API key": "Authentication failed: Invalid API key",
        # Only a BARE refusal maps to the generic wording. A refusal that
        # explains itself — "MCP access denied: user is not a member of the
        # MCP User group." — must keep its text: the explanation is the
        # actionable part and names a group, not an internal. Matching
        # "Access denied" anywhere replaced those messages wholesale, which
        # is why a missing MCP User group surfaced as an unexplained
        # "Permission denied for this operation". Unmatched messages still
        # run PATTERNS_TO_REMOVE below, so any real internals are scrubbed.
        r"^Access denied\.?$": "Permission denied for this operation",
        # Record errors
        r"Record not found": "The requested record does not exist",
        r"Record .+ does not exist": "Record ID {} not found",
        # Domain errors
        r"Invalid domain": "Invalid search criteria format",
        r"Malformed domain": "Search criteria is not properly formatted",
    }

    @classmethod
    def sanitize_message(cls, message: str) -> str:
        """Sanitize an error message by removing internal details.

        Args:
            message: The original error message

        Returns:
            Sanitized error message safe for user consumption
        """
        if not message:
            return "An error occurred"

        # Traceback-shaped messages carry source lines, SQL constraint
        # names and data values — reduce to the final exception message
        # before any other processing.
        message = cls._reduce_traceback(message)

        sanitized = message

        # First, try to match against known error patterns
        for pattern, replacement in cls.ERROR_MAPPINGS.items():
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                # Captured groups are RAW fault text, and this branch returns
                # before PATTERNS_TO_REMOVE runs below — so a mapping that
                # echoes part of the fault (e.g. "Failed to execute .+ on .+:
                # (.+)") would otherwise hand the client the file paths,
                # host:port pairs and memory addresses that scrub exists to
                # strip. Scrub every interpolated value first, and fall back
                # to the placeholder-free wording if scrubbing empties it.
                if match.groups():
                    groups = [cls._scrub_interpolated(g) for g in match.groups()]
                    if all(groups):
                        return replacement.format(*groups)
                    return cls._strip_placeholder(replacement)
                elif "{}" in replacement:
                    # Try to extract relevant info from the message
                    extracted = cls._scrub_interpolated(
                        cls._extract_relevant_info(message, pattern)
                    )
                    if extracted:
                        return replacement.format(extracted)
                    # Extraction failed — never return a literal '{}'
                    return cls._strip_placeholder(replacement)
                return replacement

        # Remove patterns that expose internals
        for pattern, replacement in cls.PATTERNS_TO_REMOVE:
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.MULTILINE)

        # Clean up multiple spaces and newlines
        sanitized = re.sub(r"\s+", " ", sanitized).strip()

        # If the message is now too generic or empty, provide a better default
        if not sanitized or sanitized == "file" or len(sanitized) < 10:
            return "An error occurred while processing your request"

        # Ensure the message starts with a capital letter
        if sanitized and sanitized[0].islower():
            sanitized = sanitized[0].upper() + sanitized[1:]

        return sanitized

    @classmethod
    def _scrub_internal_details(cls, message: str) -> str:
        """Defensive scrub for an already-extracted business exception message.

        Used only on UserError/ValidationError/AccessError messages that
        ``sanitize_xmlrpc_fault`` has already pulled out of a fault and
        intends to surface to the user. Unlike the full ``sanitize_message``
        (which runs on raw fault strings and tracebacks), this scrub must NOT
        rewrite the prose: it does not run ``ERROR_MAPPINGS``, does not blank
        short/"file"/empty messages to a generic string, does not force
        capitalization, and does not collapse newlines. It applies only the
        ``_BUSINESS_SCRUB_PATTERNS`` substitutions to strip embedded leak
        vectors (file paths, memory addresses, module/class names, psycopg2
        internals), preserving the message's length, wording and line
        structure. Bare line numbers, quoted bare filenames and stack-frame
        residue are deliberately NOT stripped — "You cannot delete order
        line 3" and 'attach file "myscript.py"' are legitimate business
        prose that leaks nothing once paths are gone. If scrubbing empties
        the message (it was nothing but leak vectors), the empty string is
        returned — callers treat that as extraction failure and fall back
        to their class-specific generic, never the unsanitized original.
        """
        scrubbed = message
        for pattern, replacement in cls._BUSINESS_SCRUB_PATTERNS:
            scrubbed = re.sub(pattern, replacement, scrubbed, flags=re.MULTILINE)

        # Collapse runs of spaces/tabs (never newlines), then trim each line.
        scrubbed = re.sub(r"[ \t]+", " ", scrubbed)
        return "\n".join(line.strip() for line in scrubbed.split("\n")).strip()

    @classmethod
    def _scrub_interpolated(cls, value: Optional[str]) -> str:
        """Make a captured fault fragment safe to interpolate into a mapping.

        Applies the business scrub (paths, memory addresses, module/class
        names, psycopg2 internals) and collapses whitespace, so the
        ERROR_MAPPINGS return path matches the single-line, leak-free shape
        of the PATTERNS_TO_REMOVE path it bypasses. Returns "" when nothing
        survives — callers then drop the placeholder entirely.
        """
        if not value:
            return ""
        return re.sub(r"\s+", " ", cls._scrub_internal_details(value)).strip()

    @classmethod
    def _reduce_traceback(cls, message: str) -> str:
        """Reduce a traceback-shaped message to its final exception message.

        Intermediate traceback lines expose source code, file structure,
        constraint names and data values — only the final exception message
        is user-relevant. Odoo business errors (UserError etc.) are often
        multi-line, so everything from the exception line to the end is
        kept, not just the last physical line.
        """
        if cls.TRACEBACK_MARKER not in message:
            return message

        lines = [line.strip() for line in message.splitlines() if line.strip()]
        if not lines:
            return "An error occurred"

        # The final exception message starts at the first exception-shaped
        # line after the last traceback frame; fall back to the last line.
        # Match the actual frame shape, not any line that merely opens with
        # File ": a business message may legitimately quote a filename at the
        # start of a line ('File "invoice.pdf" is not valid XML'), and
        # treating that as a frame discards everything before it — including
        # the exception statement itself.
        last_frame = max(
            (i for i, line in enumerate(lines) if cls._TRACEBACK_FRAME_RE.match(line)),
            default=-1,
        )
        tail = lines[last_frame + 1 :]
        exc_idx = next(
            (i for i, line in enumerate(tail) if cls._EXCEPTION_LINE_RE.match(line)), None
        )
        # Whether the line about to be surfaced is an Odoo business exception.
        # Its author-written message may legitimately END with CONTEXT:/HINT:
        # lines ("UserError: Cannot confirm.\nHINT: unblock the customer"),
        # and those are usually the most actionable part — so the Postgres
        # diagnostic strip below must not touch them. Only a non-business
        # tail (a psycopg2 fault, a raw crash) gets its trailing DETAIL:/
        # HINT:/CONTEXT:/LINE n: rows dropped, since those carry column names
        # and row values.
        surfaced_is_business = (
            exc_idx is not None
            and cls._leading_exception_class(tail[exc_idx]) in cls._LEADING_BUSINESS_CLASSES
        )
        if not surfaced_is_business:
            while tail and re.match(r"^(DETAIL|HINT|CONTEXT|LINE \d)", tail[-1], re.IGNORECASE):
                tail.pop()
            if not tail:
                return "An error occurred"
            exc_idx = next(
                (i for i, line in enumerate(tail) if cls._EXCEPTION_LINE_RE.match(line)),
                len(tail) - 1,
            )

        final = "\n".join(tail[exc_idx:])
        if not surfaced_is_business:
            # Strip inline DETAIL appended to the exception message
            final = re.split(r"\s+DETAIL:", final)[0].strip()
        return final.strip() or "An error occurred"

    @classmethod
    def _strip_placeholder(cls, replacement: str) -> str:
        """Return the placeholder-free variant of a '{}' template."""
        stripped = replacement.replace(": {}", "").replace(" '{}'", "").replace("{}", "")
        return re.sub(r"\s+", " ", stripped).strip(" :'\"")

    @classmethod
    def _extract_relevant_info(cls, message: str, pattern: str) -> Optional[str]:
        """Extract relevant information from error message.

        Args:
            message: The error message
            pattern: The pattern that matched

        Returns:
            Extracted information or None
        """
        # Try to extract field names - look for the actual field name after model prefix
        if "field" in pattern.lower():
            # First try to find field after model name (e.g., res.partner.field_name)
            full_field_match = re.search(
                r"[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_.]*\.([a-zA-Z_][a-zA-Z0-9_]*)",
                message,
            )
            if full_field_match:
                return full_field_match.group(1)
            # Otherwise try to find any quoted field name
            field_match = re.search(r"['\"]([a-zA-Z_][a-zA-Z0-9_]*)['\"]", message)
            if field_match:
                return field_match.group(1)

        # Try to extract model names
        model_match = re.search(
            r"model\s+['\"]?([a-zA-Z_][a-zA-Z0-9_.]*)['\"]?", message, re.IGNORECASE
        )
        if model_match and "model" in pattern.lower():
            return model_match.group(1)

        # Try to extract record IDs
        id_match = re.search(r"ID\s+(\d+)", message, re.IGNORECASE)
        if id_match and "record" in pattern.lower():
            return id_match.group(1)

        return None

    @classmethod
    def sanitize_error_details(cls, details: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize error details dictionary.

        Args:
            details: Original error details

        Returns:
            Sanitized error details
        """
        if not details:
            return {}

        sanitized = {}

        # Only include safe fields
        safe_fields = {"model", "operation", "record_id", "field", "domain"}

        for key, value in details.items():
            if key in safe_fields:
                sanitized[key] = value
            elif key == "error_type":
                # Map internal error types to user-friendly categories
                sanitized["category"] = cls._map_error_type(value)

        sanitized.pop("traceback", None)

        return sanitized

    @classmethod
    def _map_error_type(cls, error_type: str) -> str:
        """Map internal error type to user-friendly category.

        Args:
            error_type: Internal Python error type name

        Returns:
            User-friendly error category
        """
        mappings = {
            "ValidationError": "validation_error",
            "ValueError": "invalid_input",
            "TypeError": "invalid_type",
            "KeyError": "not_found",
            "NotFoundError": "not_found",
            "PermissionError": "permission_denied",
            "MCPPermissionError": "permission_denied",
            "MCPConnectionError": "connection_error",
            "MCPSystemError": "internal_error",
            "AccessControlError": "access_denied",
            "AuthenticationError": "authentication_failed",
            "ConnectionError": "connection_error",
            "OdooConnectionError": "connection_error",
            "TimeoutError": "timeout",
            "SystemError": "internal_error",
        }

        return mappings.get(error_type, "error")

    # Validation-class Odoo exception names treated as business faults when
    # they lead the reduced fault (class-first routing, see
    # sanitize_xmlrpc_fault) or appear anywhere in a fault with no leading
    # class. "AccessError"/"Access Denied" are deliberately absent HERE (the
    # substring set): only the leading-class form classifies an AccessError
    # as business (see _LEADING_BUSINESS_CLASSES) — a mere mention in a
    # class-less fault, and the literal "Access Denied" login-failure fault,
    # keep their connection-flavored classification.
    _BUSINESS_EXCEPTION_CLASSES = (
        "UserError",
        "ValidationError",
        "MissingError",
    )

    # Leading-class business set: the validation classes plus AccessError. A
    # fault that STARTS with AccessError is a record-rule/ACL denial whose
    # message is actionable prose ("You are not allowed to modify ... allowed
    # for the following groups: ...") — a business condition the caller
    # should adapt to, not a transport problem to retry.
    _LEADING_BUSINESS_CLASSES = _BUSINESS_EXCEPTION_CLASSES + ("AccessError",)

    # Class-agnostic business-error shapes — these classify a fault as a
    # business error regardless of which exception class carried it (e.g.
    # "ValueError: Invalid field 'x' on model 'y'").
    _BUSINESS_TEXT_MARKERS = (
        "Invalid field",
        "Object does not exist",
    )

    # Markers identifying a fault as a user-facing business error rather
    # than a transport problem — the same shapes sanitize_xmlrpc_fault maps
    # to user-facing messages.
    BUSINESS_FAULT_MARKERS = _BUSINESS_EXCEPTION_CLASSES + _BUSINESS_TEXT_MARKERS

    @classmethod
    def is_business_fault(cls, fault_string: str) -> bool:
        """Whether an XML-RPC fault string carries a user-facing business error.

        Matched on the traceback-reduced message (same as
        sanitize_xmlrpc_fault) so an intermediate frame's source line
        mentioning e.g. ``raise ValidationError`` cannot classify a fault
        whose final exception is something else. Class-first, mirroring
        sanitize_xmlrpc_fault's routing: when the reduced fault STARTS with a
        recognizable exception class, that class decides — a UserError whose
        message merely mentions "ValidationError" is still a business fault,
        and a ValueError mentioning one is not. A leading AccessError is
        business too (its message is actionable access-rules prose); the
        literal "Access Denied" login fault has no leading class and stays
        non-business. The class-agnostic text markers still apply either way.
        """
        reduced = cls._reduce_traceback(fault_string)
        leading = cls._leading_exception_class(reduced)
        if leading is not None:
            return leading in cls._LEADING_BUSINESS_CLASSES or any(
                marker in reduced for marker in cls._BUSINESS_TEXT_MARKERS
            )
        return any(marker in reduced for marker in cls.BUSINESS_FAULT_MARKERS)

    # Leading exception class of a reduced fault: modern form
    # "[odoo.exceptions.]UserError: msg" or repr form "UserError('msg'...)",
    # anchored at the start so a class name merely MENTIONED inside a message
    # ("A ValidationError occurred in the payroll batch") cannot match.
    _LEADING_EXCEPTION_RE = re.compile(r"^(?:[\w.]+\.)?([A-Za-z_]\w*(?:Error|Exception))\b\s*[:(]")

    # Generic fallbacks when the exception's message can't be extracted.
    # AccessError's text is actionable ("You are not allowed to modify ...
    # allowed for the following groups: ..."); its fallback is an accurate
    # generic — NOT the invalid-credentials text, which belongs to the
    # literal "Access Denied" login-failure fault in the legacy substring
    # chain. A leading AccessError is also a business fault
    # (_LEADING_BUSINESS_CLASSES), so its message surfaces without a
    # connection-error prefix.
    _BUSINESS_MESSAGE_FALLBACKS = {
        "UserError": "Operation failed due to business rule violation",
        "ValidationError": "Validation error: Please check your input",
        "AccessError": "Access denied by Odoo's access rules",
    }

    @classmethod
    def _leading_exception_class(cls, reduced: str) -> Optional[str]:
        """Bare exception class name a traceback-reduced fault STARTS with.

        Returns e.g. ``UserError`` for ``odoo.exceptions.UserError: msg`` or
        ``UserError('msg')``; None when the fault does not open with an
        exception-shaped ``Name:``/``Name(`` token.
        """
        match = cls._LEADING_EXCEPTION_RE.match(reduced.lstrip())
        return match.group(1) if match else None

    @classmethod
    def _extract_business_message(cls, exc_name: str, reduced: str) -> Optional[str]:
        """Author-written message of a business exception in a reduced fault.

        Tries the repr form ``ExcName('msg')`` first, then the modern form
        ``[odoo.exceptions.]ExcName: msg``. The repr form captures only the
        FIRST string argument — content free of the opening delimiter,
        terminated by that delimiter then a comma or closing paren — so
        ``ValidationError('Bad value', 'field')`` yields "Bad value", not a
        mangled span reaching the last quote. The modern form is anchored to
        a line start (MULTILINE) — a fault that merely MENTIONS
        "ValidationError: retry queue full" mid-sentence must not have its
        tail extracted as the user's validation problem. DOTALL both times —
        business messages are often multi-line ("You cannot delete X
        because:\\n- reason 1\\n- reason 2") and every line is kept. Returns
        None when neither shape matches.
        """
        repr_match = re.search(rf'{exc_name}\((["\'])((?:(?!\1).)*)\1\s*[,)]', reduced, re.DOTALL)
        if repr_match:
            return repr_match.group(2)
        # ^[ \t]* : the ExcName may be indented but must begin its line
        # (matching _leading_exception_class's lstrip'd routing).
        modern_match = re.search(
            rf"^[ \t]*(?:[\w.]+\.)?{exc_name}:\s*(.+)", reduced, re.DOTALL | re.MULTILINE
        )
        if modern_match:
            return modern_match.group(1).strip()
        return None

    @classmethod
    def sanitize_business_fault(cls, fault_string: str) -> str:
        """Sanitize a fault the TRANSPORT already identified as a business error.

        Odoo's ``/xmlrpc/2/*`` endpoint reports UserError/ValidationError and
        AccessError through ``faultCode`` (2 / 4) and sends the author-written
        message BARE — no exception-class prefix, no traceback. String-shape
        routing therefore cannot recognize these, so ``_raise_for_fault``
        classifies them by code and calls this instead of
        ``sanitize_xmlrpc_fault``: the message is scrubbed of leak vectors but
        keeps its prose, its length and — critically — its line structure, so
        multi-line business errors (an AccessError's "allowed for the
        following groups:" list) survive intact.
        """
        if cls.TRACEBACK_MARKER in fault_string:
            # Not the documented shape for these codes; fall back to the
            # full traceback-aware path rather than surfacing frames.
            return cls.sanitize_xmlrpc_fault(fault_string)
        leading = cls._leading_exception_class(fault_string)
        if leading in cls._BUSINESS_MESSAGE_FALLBACKS:
            return cls._business_fault_message(leading, fault_string)
        scrubbed = cls._scrub_internal_details(fault_string)
        return scrubbed or cls._BUSINESS_MESSAGE_FALLBACKS["UserError"]

    @classmethod
    def _business_fault_message(cls, exc_name: str, reduced: str) -> str:
        """User-facing message for a UserError/ValidationError/AccessError fault.

        The extracted message is author-facing and meant to be surfaced, so it
        is only defensively scrubbed of embedded leak vectors
        (_scrub_internal_details) — its prose, length and newlines are
        preserved, unlike sanitize_message. Falls back to the class's generic
        message when nothing is extractable, or when the scrub emptied the
        extraction (a message that was nothing but leak vectors — e.g. a bare
        file path — must never surface unsanitized).
        """
        extracted = cls._extract_business_message(exc_name, reduced)
        if extracted:
            scrubbed = cls._scrub_internal_details(extracted)
            if scrubbed:
                return scrubbed
        return cls._BUSINESS_MESSAGE_FALLBACKS[exc_name]

    @classmethod
    def sanitize_xmlrpc_fault(cls, fault_string: str) -> str:
        """Sanitize XML-RPC fault messages from Odoo.

        Args:
            fault_string: Raw fault string from XML-RPC

        Returns:
            Sanitized error message
        """
        # Full server tracebacks in faultString: keep only the final
        # exception message before classifying
        fault_string = cls._reduce_traceback(fault_string)

        # Class-first routing: when the fault STARTS with a recognized Odoo
        # exception class, that class picks the branch. Substring probing
        # would let message CONTENT hijack the routing — "UserError: Invalid
        # field mapping in your import file" is not an invalid-field fault,
        # and "UserError: A ValidationError occurred..." is not a
        # ValidationError; both must surface their real message.
        leading = cls._leading_exception_class(fault_string)
        # UserError / ValidationError / AccessError: surface the author-written
        # message (defensively scrubbed) — for AccessError that text is
        # actionable ("You are not allowed to modify ... allowed for the
        # following groups: ..."), not a credentials problem.
        if leading in cls._BUSINESS_MESSAGE_FALLBACKS:
            return cls._business_fault_message(leading, fault_string)
        if leading == "MissingError":
            return "The requested record was not found"

        # No leading class picked the branch — legacy substring routing
        # (order preserved) so every non-prefixed fault shape behaves as
        # before.
        if "Access Denied" in fault_string:
            return "Access denied: Invalid credentials or insufficient permissions"
        elif "Object does not exist" in fault_string:
            return "The requested resource does not exist"
        elif "Invalid field" in fault_string:
            # Try to extract field name
            field_match = re.search(
                r"field\s+['\"]?([a-zA-Z_][a-zA-Z0-9_\.]*)['\"]?", fault_string, re.IGNORECASE
            )
            if field_match:
                return f"Invalid field '{field_match.group(1)}' in request"
            return "Invalid field in request"
        elif "MissingError" in fault_string:
            return "The requested record was not found"
        elif "ValidationError" in fault_string:
            return cls._business_fault_message("ValidationError", fault_string)
        elif "UserError" in fault_string:
            return cls._business_fault_message("UserError", fault_string)
        else:
            # Generic sanitization
            return cls.sanitize_message(fault_string)
