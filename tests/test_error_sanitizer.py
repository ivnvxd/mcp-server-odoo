"""Tests for error message sanitization."""

import pytest

from mcp_server_odoo.error_sanitizer import ErrorSanitizer


class TestErrorSanitizer:
    """Test error message sanitization functionality."""

    def test_sanitize_file_paths(self):
        """Test that file paths are removed."""
        message = 'File "/home/user/odoo/models.py", line 123, in execute'
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "/home/user" not in sanitized
        assert "line 123" not in sanitized
        assert ".py" not in sanitized

    def test_sanitize_module_paths(self):
        """Test that module paths are removed."""
        message = "mcp_server_odoo.odoo_connection: Connection failed"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "mcp_server_odoo." not in sanitized
        assert "Connection failed" in sanitized

    def test_sanitize_class_names(self):
        """Test that class names are removed."""
        message = "Error: <class 'xmlrpc.client.Fault'> occurred"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "<class" not in sanitized
        assert "xmlrpc.client" not in sanitized

    def test_sanitize_memory_addresses(self):
        """Test that memory addresses are removed."""
        message = "Object at 0x7f8b8c0d5f40 not found"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "0x7f8b8c0d5f40" not in sanitized
        assert "Object at" not in sanitized

    def test_sanitize_traceback(self):
        """Test that traceback information is removed."""
        message = """Traceback (most recent call last):
          File "test.py", line 10, in <module>
            raise ValueError("Test error")
        ValueError: Test error"""
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "Traceback" not in sanitized
        assert 'File "test.py"' not in sanitized
        assert "Test error" in sanitized

    def test_field_error_mapping(self):
        """Test specific field error mappings."""
        message = "Invalid field res.partner.invalid_field in leaf ('invalid_field', '=', True)"
        sanitized = ErrorSanitizer.sanitize_message(message)
        # The sanitizer extracts just the field name, not the full model.field path
        assert sanitized == "Invalid field 'invalid_field' in search criteria"

        message = "Field bogus_field does not exist"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Field 'bogus_field' does not exist on this model"

    def test_model_error_mapping(self):
        """Test model error mappings."""
        message = "Model sale.order does not exist"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Model 'sale.order' is not available"

    def test_connection_error_mapping(self):
        """Test connection error mappings."""
        message = "Connection refused"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Cannot connect to Odoo server"

        message = "Operation timeout after 30 seconds"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Request timed out"

    def test_xmlrpc_fault_sanitization(self):
        """Test XML-RPC fault message sanitization."""
        fault = "Access Denied: Invalid API key or insufficient permissions"
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "Access denied: Invalid credentials or insufficient permissions"

        fault = "ValidationError: Field 'vat' is required"
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "Field 'vat' is required"

        fault = "UserError('Cannot delete record that has dependencies')"
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "Cannot delete record that has dependencies"

    def test_sanitize_validation_error_extracts_message(self):
        """ValidationError faults surface their real business message."""
        # Modern traceback form: keep the message after the exception name.
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/opt/odoo/odoo/models.py", line 1, in _check\n'
            "    raise ValidationError(msg)\n"
            "odoo.exceptions.ValidationError: Quantity must be positive"
        )
        assert ErrorSanitizer.sanitize_xmlrpc_fault(fault) == "Quantity must be positive"

        # Repr form: extract the quoted message. Short messages survive
        # intact — the narrow defensive scrub never blanks a business message.
        assert ErrorSanitizer.sanitize_xmlrpc_fault("ValidationError('Bad state')") == "Bad state"

        # Multi-line business message: DOTALL captures every line; the narrow
        # defensive scrub (_scrub_internal_details) preserves the newlines,
        # unlike the full sanitize_message which would collapse them to spaces.
        multi = (
            "odoo.exceptions.ValidationError: Cannot confirm because:\n"
            "- total is negative\n"
            "- tax is missing"
        )
        result = ErrorSanitizer.sanitize_xmlrpc_fault(multi)
        assert result == "Cannot confirm because:\n- total is negative\n- tax is missing"

        # Bare/garbled ValidationError with nothing extractable: generic fallback.
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("raised a ValidationError somewhere")
            == "Validation error: Please check your input"
        )

    def test_sanitize_user_error_generic_fallback(self):
        """A UserError fault with nothing extractable falls back to the generic
        business-rule message (mirrors the ValidationError bare-fallback path)."""
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("raised a UserError somewhere")
            == "Operation failed due to business rule violation"
        )

    def test_repr_message_with_embedded_apostrophe_survives(self):
        """A repr-form message quoted with " and containing an apostrophe (as
        Python's repr produces) is extracted whole, not truncated at the
        apostrophe."""
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault('ValidationError("You can\'t do this")')
            == "You can't do this"
        )
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault('UserError("You can\'t delete this record")')
            == "You can't delete this record"
        )

    def test_repr_message_multi_argument_extracts_first_only(self):
        """A repr-form fault with several arguments extracts the FIRST string
        argument — not a mangled span reaching the last closing quote."""
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("ValidationError('Bad value', 'field')")
            == "Bad value"
        )
        assert ErrorSanitizer.sanitize_xmlrpc_fault("UserError('msg', None)") == "msg"

    def test_repr_message_single_argument_both_quote_styles(self):
        """Single-argument repr extraction is unchanged for both quote styles."""
        assert ErrorSanitizer.sanitize_xmlrpc_fault("UserError('plain message')") == "plain message"
        assert ErrorSanitizer.sanitize_xmlrpc_fault('UserError("plain message")') == "plain message"

    def test_repr_message_multiline_captured_whole(self):
        """A repr-form message spanning real newlines is captured in full via
        DOTALL, not truncated at the first line or dropped to the generic
        fallback."""
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault(
                "ValidationError('Cannot save:\nreason A\nreason B')"
            )
            == "Cannot save:\nreason A\nreason B"
        )
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("UserError('Blocked:\n- step 1\n- step 2')")
            == "Blocked:\n- step 1\n- step 2"
        )

    def test_extracted_business_message_defensively_scrubbed(self):
        """Extracted UserError/ValidationError messages pass through the narrow
        _scrub_internal_details: embedded leak vectors are stripped, but prose,
        short messages and newlines survive (unlike the full sanitize_message)."""
        # (a) UserError with an embedded path — path removed, words kept.
        user_sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(
            "UserError('Import failed for /opt/odoo/data/x.py during load')"
        )
        assert "/opt/odoo" not in user_sanitized
        assert ".py" not in user_sanitized
        assert "Import failed" in user_sanitized

        # (b) ValidationError with an embedded path — same scrubbing.
        val_sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(
            "ValidationError('Config /opt/odoo/data/x.py is invalid')"
        )
        assert "/opt/odoo" not in val_sanitized
        assert "is invalid" in val_sanitized

        # (c) Plain business prose is returned unchanged through both branches.
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("UserError('Quantity must be positive')")
            == "Quantity must be positive"
        )
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("ValidationError('Quantity must be positive')")
            == "Quantity must be positive"
        )

        # (d) A short (<10 char) message is returned intact, NOT blanked to a
        # generic string (the full sanitize_message would replace it).
        assert ErrorSanitizer.sanitize_xmlrpc_fault("UserError('Too big')") == "Too big"

        # (e) A multi-line business message keeps its newlines.
        multi = ErrorSanitizer.sanitize_xmlrpc_fault(
            "odoo.exceptions.UserError: Cannot delete because:\n- it is posted\n- it has a payment"
        )
        assert multi == "Cannot delete because:\n- it is posted\n- it has a payment"

        # A message that merely CONTAINS an ERROR_MAPPINGS trigger word is
        # surfaced faithfully, NOT rewritten (the full sanitize_message would
        # rewrite "Access denied" to "Permission denied for this operation").
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("UserError('Access denied to this sales order')")
            == "Access denied to this sales order"
        )

    def test_scrub_emptied_message_falls_back_to_generic(self):
        """A business message that is NOTHING but leak vectors (e.g. a bare
        file path) must fall back to the class generic — never surface the
        unsanitized original."""
        for fault in (
            "UserError('/opt/odoo/addons/custom/broken.py')",
            "odoo.exceptions.UserError: /opt/odoo/addons/custom/broken.py",
        ):
            sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
            assert sanitized == "Operation failed due to business rule violation"
            assert "/opt/odoo" not in sanitized
            assert "broken.py" not in sanitized

        # The scrub itself reports emptiness as '' (callers handle fallback).
        assert ErrorSanitizer._scrub_internal_details("/opt/odoo/addons/custom/broken.py") == ""

        # Normal messages are unaffected.
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("UserError('Quantity must be positive')")
            == "Quantity must be positive"
        )

    def test_business_scrub_keeps_quoted_filename_prose(self):
        """A quoted bare filename is legitimate business prose ('attach file
        "myscript.py"') — the narrow business scrub must not eat it."""
        message = 'Please attach file "myscript.py" before continuing'
        assert ErrorSanitizer._scrub_internal_details(message) == message
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault(f"odoo.exceptions.UserError: {message}") == message
        )

    def test_business_scrub_still_strips_traceback_frame_lines(self):
        """A real traceback frame embedded in a business message is still
        stripped (the ^File "...", line N pattern, not the quoted-filename
        one, covers it)."""
        scrubbed = ErrorSanitizer._scrub_internal_details(
            'Cannot import:\nFile "/opt/odoo/x.py", line 3, in load\nfix your data'
        )
        assert "/opt/odoo" not in scrubbed
        assert 'File "' not in scrubbed
        assert "Cannot import:" in scrubbed
        assert "fix your data" in scrubbed

    def test_full_sanitize_message_still_strips_quoted_filenames(self):
        """sanitize_message (raw faults/tracebacks) must KEEP stripping quoted
        .py filenames — only the business scrub spares them."""
        sanitized = ErrorSanitizer.sanitize_message('Unexpected failure in File "x.py" while busy')
        assert "x.py" not in sanitized

    def test_leading_class_beats_substring_routing(self):
        """Class-first routing: the exception class LEADING the fault picks the
        branch — message content can no longer hijack the routing and destroy
        the real business message."""
        # A UserError whose message contains "Invalid field" is NOT an
        # invalid-field fault — the full message must survive.
        message = "Invalid field mapping in your import file, fix column 3"
        assert ErrorSanitizer.sanitize_xmlrpc_fault(f"odoo.exceptions.UserError: {message}") == (
            message
        )

        # A UserError whose message mentions "ValidationError" is NOT a
        # ValidationError — no generic "check your input" rewrite.
        message = "A ValidationError occurred in the payroll batch"
        assert ErrorSanitizer.sanitize_xmlrpc_fault(f"odoo.exceptions.UserError: {message}") == (
            message
        )

    def test_mid_sentence_class_mention_not_extracted(self):
        """The modern-form extraction is anchored to a line start — a fault
        that merely MENTIONS "ValidationError: ..." mid-sentence must not
        have its tail extracted as the user's validation problem; it falls
        through to the class's generic fallback."""
        fault = "Worker log said ValidationError: retry queue full at 12:03"
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault(fault)
            == "Validation error: Please check your input"
        )

        # A line-start (even indented) modern form still extracts normally.
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("  ValidationError: Quantity must be positive")
            == "Quantity must be positive"
        )

    def test_invalid_field_without_class_prefix_still_rewritten(self):
        """A genuine invalid-field fault with no leading exception class keeps
        the existing substring routing and field-name rewrite."""
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("Invalid field 'foo' on model 'res.partner'")
            == "Invalid field 'foo' in request"
        )

    def test_leading_access_error_surfaces_real_message(self):
        """A fault led by AccessError surfaces Odoo's actionable message —
        not the invalid-credentials boilerplate (that text belongs to the
        literal "Access Denied" login-failure fault)."""
        fault = "odoo.exceptions.AccessError: You are not allowed to modify 'res.partner'"
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault(fault)
            == "You are not allowed to modify 'res.partner'"
        )

    def test_access_error_groups_message_survives_scrubbed(self):
        """The full modify/groups AccessError text (reduced from a server
        traceback) survives end-to-end, with leak vectors scrubbed."""
        message = (
            "You are not allowed to modify 'Contact' (res.partner).\n"
            "This operation is allowed for the following groups:\n"
            "- Administration/Settings\n"
            "- Sales/Administrator"
        )
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/opt/odoo/odoo/models.py", line 3720, in write\n'
            "    self.check_access_rights('write')\n"
            f"odoo.exceptions.AccessError: {message}"
        )
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == message
        assert "/opt/odoo" not in sanitized

    def test_access_error_without_extractable_message_falls_back(self):
        """A leading AccessError with nothing extractable gets the accurate
        access-rules generic, not the invalid-credentials text."""
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("odoo.exceptions.AccessError:")
            == "Access denied by Odoo's access rules"
        )

    def test_literal_access_denied_keeps_credentials_text(self):
        """The literal 'Access Denied' login-failure fault (no leading class)
        keeps the invalid-credentials wording via the legacy substring chain."""
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault("Access Denied")
            == "Access denied: Invalid credentials or insufficient permissions"
        )

    def test_is_business_fault_class_first(self):
        """is_business_fault mirrors the class-first routing."""
        # Leading business classes → True, whatever the message mentions.
        assert ErrorSanitizer.is_business_fault(
            "odoo.exceptions.UserError: A ValidationError occurred in the payroll batch"
        )
        assert ErrorSanitizer.is_business_fault(
            "odoo.exceptions.UserError: Invalid field mapping in your import file, fix column 3"
        )
        assert ErrorSanitizer.is_business_fault("ValidationError('Bad state')")
        assert ErrorSanitizer.is_business_fault("odoo.exceptions.MissingError: Record gone")
        # Class-agnostic markers still classify under a foreign leading class.
        assert ErrorSanitizer.is_business_fault(
            "ValueError: Invalid field 'bad_field' on model 'res.partner'"
        )
        # Non-prefixed business shapes keep the substring fallback.
        assert ErrorSanitizer.is_business_fault("Object does not exist: res.partner(999,)")
        assert ErrorSanitizer.is_business_fault("Invalid field 'foo' on model 'res.partner'")
        # A recognized non-business leading class no longer classifies via a
        # class name mentioned in its message body.
        assert not ErrorSanitizer.is_business_fault(
            "ValueError: user mentioned ValidationError in a comment"
        )
        # A leading AccessError is a business fault: its access-rules
        # explanation must surface without a connection-error prefix.
        assert ErrorSanitizer.is_business_fault("odoo.exceptions.AccessError: not allowed")
        # ... but only the leading-class form: a mere mention under a foreign
        # leading class, and the literal "Access Denied" login-failure fault,
        # stay connection-flavored (auth setup, not record rules).
        assert not ErrorSanitizer.is_business_fault(
            "RuntimeError: wrapped an AccessError internally"
        )
        assert not ErrorSanitizer.is_business_fault("Access Denied")

    def test_business_scrub_keeps_line_number_prose(self):
        """ "line N" is legitimate business prose (order lines, import rows) —
        the narrow business scrub must not eat it, directly or end-to-end."""
        message = "You cannot delete order line 3 because it is invoiced"

        # Directly via the scrub.
        assert ErrorSanitizer._scrub_internal_details(message) == message

        # End-to-end via a modern-form ValidationError fault.
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/opt/odoo/odoo/models.py", line 99, in _check\n'
            "    raise ValidationError(msg)\n"
            f"odoo.exceptions.ValidationError: {message}"
        )
        assert ErrorSanitizer.sanitize_xmlrpc_fault(fault) == message

    def test_business_scrub_keeps_newline_before_line_number_prose(self):
        """A line starting with "line 5 ..." keeps its newline and prose —
        the old bare line-number pattern's leading \\s* used to consume the
        preceding \\n and merge the lines."""
        message = "Import failed:\nline 5 has an invalid date"
        assert ErrorSanitizer._scrub_internal_details(message) == message
        assert (
            ErrorSanitizer.sanitize_xmlrpc_fault(f"odoo.exceptions.UserError: {message}") == message
        )

    def test_business_scrub_still_strips_paths(self):
        """The narrow scrub still removes embedded .py paths."""
        scrubbed = ErrorSanitizer._scrub_internal_details(
            "Import of /opt/odoo/x.py failed on line 3"
        )
        assert "/opt/odoo" not in scrubbed
        assert ".py" not in scrubbed
        assert "line 3" in scrubbed  # prose survives once the path is gone

    def test_business_scrub_still_strips_odoo_module_paths(self):
        """A genuine odoo.exceptions module path embedded in a message is
        still stripped."""
        scrubbed = ErrorSanitizer._scrub_internal_details(
            "Rejected by odoo.exceptions.ValidationError: bad state"
        )
        assert "odoo.exceptions" not in scrubbed
        assert "bad state" in scrubbed

    def test_business_scrub_strips_underscore_odoo_module_roots(self):
        """The blanket odoo.* pattern catches roots with a leading underscore
        (Odoo 19 logs via odoo._monkeypatches) — no enumeration to trail."""
        scrubbed = ErrorSanitizer._scrub_internal_details(
            "Raised in odoo._monkeypatches.foo: cannot patch this"
        )
        assert "odoo._monkeypatches" not in scrubbed
        assert "cannot patch this" in scrubbed

    def test_full_sanitize_message_still_strips_line_numbers(self):
        """sanitize_message (raw tracebacks/faults) must KEEP stripping line
        numbers — only the business scrub spares them."""
        sanitized = ErrorSanitizer.sanitize_message(
            'Internal Server Error File "/opt/odoo/models.py", line 123, in execute'
        )
        assert "line 123" not in sanitized

    def test_sanitize_missing_error(self):
        """Test that MissingError fault is sanitized to a user-friendly message."""
        fault = "MissingError: Record does not exist or has been deleted."
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "The requested record was not found"

    def test_sanitize_error_details(self):
        """Test error details sanitization."""
        details = {
            "error_type": "ValidationError",
            "traceback": "Full traceback here...",
            "model": "res.partner",
            "operation": "create",
            "internal_path": "/opt/odoo/addons",
        }

        sanitized = ErrorSanitizer.sanitize_error_details(details)

        assert "traceback" not in sanitized
        assert "internal_path" not in sanitized
        assert sanitized["model"] == "res.partner"
        assert sanitized["operation"] == "create"
        assert sanitized["category"] == "validation_error"

    def test_error_type_mapping(self):
        """Test internal error type mapping."""
        assert ErrorSanitizer._map_error_type("ValidationError") == "validation_error"
        assert ErrorSanitizer._map_error_type("OdooConnectionError") == "connection_error"
        assert ErrorSanitizer._map_error_type("NotFoundError") == "not_found"
        assert ErrorSanitizer._map_error_type("UnknownError") == "error"

    def test_empty_message_handling(self):
        """Test handling of empty messages."""
        assert ErrorSanitizer.sanitize_message("") == "An error occurred"
        assert ErrorSanitizer.sanitize_message(None) == "An error occurred"

    def test_preserve_useful_information(self):
        """Test that useful information is preserved."""
        message = "Cannot find partner with email test@example.com"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "test@example.com" in sanitized

        message = "Invalid value 'abc' for integer field"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "'abc'" in sanitized
        assert "integer" in sanitized

    def test_capitalization(self):
        """Test that messages are properly capitalized."""
        message = "connection failed"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized[0].isupper()

    def test_internal_details_removal(self):
        """Test removal of internal implementation details."""
        message = "MCPObjectController: Invalid field res.partner.test_field"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "MCPObjectController:" not in sanitized
        assert "Invalid field" in sanitized

    def test_complex_error_message(self):
        """Test sanitization of complex real-world error."""
        message = """Error executing tool search_records: Connection error: Failed to execute search_count on res.partner: Internal Server Error in MCPObjectController: Invalid field res.partner.invalid_field in leaf ('invalid_field', '=', True)
        File "/opt/odoo/addons/mcp_server/controllers/xmlrpc.py", line 123"""

        sanitized = ErrorSanitizer.sanitize_message(message)

        # Should not contain internal details
        assert "MCPObjectController" not in sanitized
        assert "/opt/odoo" not in sanitized
        assert "line 123" not in sanitized
        assert "search_count" not in sanitized

        # Should contain useful information
        assert "Invalid field" in sanitized


class TestTracebackReduction:
    """Traceback-shaped messages must never leak server internals."""

    UNIQUE_INDEX_FAULT = (
        "Traceback (most recent call last):\n"
        '  File "/opt/odoo/odoo/service/model.py", line 134, in retrying\n'
        "    result = func()\n"
        '  File "/opt/odoo/odoo/models.py", line 4567, in write\n'
        "    self._write(vals)\n"
        "psycopg2.errors.UniqueViolation: duplicate key value violates "
        'unique constraint "res_partner_email_uniq"\n'
        "DETAIL:  Key (email)=(secret@internal.corp) already exists.\n"
    )

    def test_unique_violation_leaks_nothing(self):
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(self.UNIQUE_INDEX_FAULT)
        assert "res_partner_email_uniq" not in sanitized
        assert "secret@internal.corp" not in sanitized
        assert "/opt/odoo" not in sanitized
        assert "_write" not in sanitized
        assert sanitized == "A record with these values already exists"

    def test_value_error_traceback_keeps_final_message_only(self):
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/opt/odoo/odoo/api.py", line 525, in _call_kw\n'
            "    result = getattr(recs, method)(*args, **kwargs)\n"
            "ValueError: Wrong value for res.partner.type: 'bogus'\n"
        )
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert "/opt/odoo" not in sanitized
        assert "_call_kw" not in sanitized
        assert "Wrong value" in sanitized

    def test_modern_user_error_message_preserved(self):
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/opt/odoo/odoo/models.py", line 99, in check\n'
            "    raise UserError(msg)\n"
            "odoo.exceptions.UserError: You cannot delete a posted invoice.\n"
        )
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "You cannot delete a posted invoice."

    def test_multiline_user_error_message_kept_intact(self):
        """Business errors are often multi-line — reduction must keep the
        whole final message, not just the last physical line."""
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/opt/odoo/odoo/models.py", line 99, in check\n'
            "    raise UserError(msg)\n"
            "odoo.exceptions.UserError: You cannot delete this invoice because:\n"
            "- it is posted\n"
            "- it has a payment attached\n"
        )
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert "You cannot delete this invoice because:" in sanitized
        assert "- it is posted" in sanitized
        assert "- it has a payment attached" in sanitized
        assert "/opt/odoo" not in sanitized
        assert "raise UserError" not in sanitized

    def test_missing_error_real_wording_no_placeholder(self):
        # Odoo's actual MissingError wording carries no 'ID <n>' token
        message = "Record res.partner(99,) does not exist or has been deleted"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "{}" not in sanitized

    def test_failed_to_execute_detail_preserved(self):
        message = "Failed to execute search on res.partner: timeout"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Operation failed: timeout"
        assert "{}" not in sanitized

    def test_no_mapping_ever_emits_placeholder(self):
        """Property: no sanitizer output contains a literal '{}'."""
        probes = [
            "Record res.partner(7,) does not exist or has been deleted",
            "Failed to execute write on crm.lead: boom",
            "Invalid field in leaf",
            "Unknown field in domain",
            "Model does not exist",
            "Record does not exist",
        ]
        for probe in probes:
            assert "{}" not in ErrorSanitizer.sanitize_message(probe), probe


class TestBusinessFaultLineStructure:
    """Odoo sends business errors BARE (faultCode 2/4) — no class prefix, no
    traceback — so the message must survive with its line structure intact.
    """

    def test_business_traceback_keeps_context_and_hint(self):
        """A UserError's own CONTEXT:/HINT: lines are part of the message.

        The Postgres-diagnostic strip must not eat them — they are usually
        the most actionable half of the error.
        """
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/dist-packages/odoo/api.py", line 100, in call_kw\n'
            "    result = _call_kw_model(method, model, args, kwargs)\n"
            "odoo.exceptions.UserError: Cannot confirm the order.\n"
            "CONTEXT: the customer is blocked\n"
            "HINT: unblock the customer first"
        )
        out = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert "Cannot confirm the order." in out
        assert "CONTEXT: the customer is blocked" in out
        assert "HINT: unblock the customer first" in out

    def test_postgres_diagnostics_still_stripped(self):
        """The strip still fires for non-business faults — DETAIL carries
        column names and row values."""
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/dist-packages/odoo/sql_db.py", line 100, in execute\n'
            "    res = self._obj.execute(query, params)\n"
            "psycopg2.errors.UniqueViolation: duplicate key value violates unique "
            'constraint "res_partner_ref_uniq"\n'
            "DETAIL: Key (ref)=(ABC123) already exists."
        )
        out = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert "ABC123" not in out
        assert "res_partner_ref_uniq" not in out

    def test_bare_business_message_keeps_newlines(self):
        """An AccessError's group list is multi-line and must stay that way."""
        bare = (
            "You are not allowed to access 'Mail Server' (ir.mail_server) records.\n"
            "\n"
            "This operation is allowed for the following groups:\n"
            "\t- Role / Administrator"
        )
        out = ErrorSanitizer.sanitize_business_fault(bare)
        assert "\n" in out, "line structure must survive"
        assert "Role / Administrator" in out

    def test_bare_business_message_keeps_context_and_hint(self):
        bare = "Cannot confirm the order.\nCONTEXT: blocked\nHINT: unblock first"
        out = ErrorSanitizer.sanitize_business_fault(bare)
        assert "CONTEXT: blocked" in out
        assert "HINT: unblock first" in out


class TestErrorMappingsDoNotLeakCapturedText:
    """The ERROR_MAPPINGS branch returns before PATTERNS_TO_REMOVE runs, so
    any captured group it interpolates has to be scrubbed on the way out."""

    def test_captured_fault_tail_is_scrubbed(self):
        message = (
            "Failed to execute write on res.partner: "
            'File "/opt/odoo/addons/sale/models/sale_order.py", line 42 '
            "at 0x7f9a1b2c3d4e (host db.internal:5432)"
        )
        out = ErrorSanitizer.sanitize_message(message)

        assert "/opt/odoo/addons" not in out
        assert "0x7f9a1b2c3d4e" not in out
        assert "db.internal:5432" not in out

    def test_mapping_still_produces_a_useful_message(self):
        out = ErrorSanitizer.sanitize_message("Failed to execute write on res.partner: boom")
        assert out
        assert "{}" not in out


class TestTracebackFrameDetection:
    """A frame is 'File "<path>", line <n>' — business prose that merely opens
    a line with a quoted filename is not one."""

    def test_business_message_quoting_a_filename_survives(self):
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/dist-packages/odoo/api.py", line 100, in call_kw\n'
            "    result = _call_kw_model(method, model, args, kwargs)\n"
            "odoo.exceptions.UserError: Import failed for 3 documents.\n"
            'File "invoice_2024.pdf" is not a valid XML file.\n'
            "Re-export it and try again."
        )
        out = ErrorSanitizer.sanitize_xmlrpc_fault(fault)

        assert "Import failed for 3 documents." in out, "the exception statement was discarded"
        assert "invoice_2024.pdf" in out
        assert "Re-export it and try again." in out

    def test_real_frames_are_still_stripped(self):
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/dist-packages/odoo/api.py", line 100, in call_kw\n'
            "    result = _call_kw_model(method, model, args, kwargs)\n"
            "odoo.exceptions.UserError: Plain message."
        )
        out = ErrorSanitizer.sanitize_xmlrpc_fault(fault)

        assert out == "Plain message."
        assert "dist-packages" not in out


class TestPostgresDiagnosticsOnBusinessPaths:
    """Odoo wraps IntegrityError into ValidationError, so psycopg2 diagnostics
    ride inside BUSINESS messages too. Preserving an author's CONTEXT:/HINT:
    lines must not also surface constraint names and live row values."""

    LEAKY = (
        "The operation cannot be completed.\n\n"
        'duplicate key value violates unique constraint "res_partner_ref_uniq"\n'
        "DETAIL:  Key (ref)=(ABC123) already exists."
    )

    def test_code2_business_path_strips_diagnostics(self):
        out = ErrorSanitizer.sanitize_business_fault(self.LEAKY)
        assert "res_partner_ref_uniq" not in out
        assert "ABC123" not in out
        assert "Key (ref)" not in out

    def test_validation_error_traceback_strips_diagnostics(self):
        fault = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/dist-packages/odoo/api.py", line 1, in f\n'
            "odoo.exceptions.ValidationError: Cannot complete\n"
            'constraint "res_partner_ref_uniq"\n'
            "DETAIL:  Key (ref)=(ABC123) already exists."
        )
        out = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert "res_partner_ref_uniq" not in out
        assert "ABC123" not in out

    def test_author_context_and_hint_still_survive(self):
        out = ErrorSanitizer.sanitize_business_fault(
            "Cannot confirm the order.\nCONTEXT: the customer is blocked\nHINT: unblock first"
        )
        assert "CONTEXT: the customer is blocked" in out
        assert "HINT: unblock first" in out


class TestDeploymentTopologyScrubbing:
    """A connection or DNS failure quotes the endpoint it tried, so internal
    hostnames, private IPs and non-default ports rode out to the client — on
    the ERROR_MAPPINGS branch too, which returns before PATTERNS_TO_REMOVE.
    """

    @pytest.mark.parametrize(
        "raw,leak",
        [
            (
                "Failed to execute search on res.partner: could not connect to "
                "https://erp.acme-internal.lan:8069/xmlrpc/2/object",
                "erp.acme-internal.lan",
            ),
            (
                "Failed to execute read on res.partner: host db-primary.internal:5432 is down",
                "db-primary.internal",
            ),
            ('could not translate host name "pg-master.corp.lan" to address', "pg-master.corp.lan"),
            ("connection to server at [fd00::5]:5432 failed", "fd00::5"),
            ("Failed to execute write on res.partner: 10.42.7.19:8069 refused", "10.42.7.19"),
        ],
    )
    def test_internal_endpoints_never_reach_the_client(self, raw, leak):
        assert leak not in ErrorSanitizer.sanitize_message(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "Invalid operation on res.partner:write for this user",
            "Quantity ratio 1.5:30 is not allowed",
            "You cannot delete a partner with open invoices.",
        ],
    )
    def test_business_prose_is_untouched(self, raw):
        assert ErrorSanitizer.sanitize_message(raw) == raw


class TestAccessDeniedMappingIsNarrow:
    """An access refusal that explains itself must survive sanitization.

    The blanket `Access denied` -> `Permission denied for this operation`
    mapping matched anywhere in the message, so the module's actionable
    "not a member of the MCP User group" wording was replaced wholesale and
    the real cause never reached the caller.
    """

    def test_explanatory_refusal_keeps_its_text(self):
        raw = "MCP access denied: user is not a member of the MCP User group."

        assert ErrorSanitizer.sanitize_message(raw) == raw

    def test_group_guidance_survives(self):
        raw = (
            "Access denied: your user is not authorized for MCP. "
            "Ask your Odoo administrator for the 'MCP User' group."
        )

        out = ErrorSanitizer.sanitize_message(raw)

        assert "MCP User" in out and "administrator" in out

    @pytest.mark.parametrize("raw", ["Access denied", "Access denied."], ids=["plain", "dotted"])
    def test_bare_refusal_still_maps_to_the_generic(self, raw):
        assert ErrorSanitizer.sanitize_message(raw) == "Permission denied for this operation"

    def test_internals_in_a_refusal_are_still_scrubbed(self):
        """Unmatched messages fall through to PATTERNS_TO_REMOVE, so keeping
        the text does not mean keeping leak vectors."""
        out = ErrorSanitizer.sanitize_message(
            "Access denied: cannot reach http://internal-erp.corp.example.com:8069/mcp"
        )

        assert "internal-erp.corp.example.com" not in out
