"""Tests for basic MCP resource handling."""

import base64
import xmlrpc.client
from unittest.mock import Mock

import pytest
from mcp import types
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import (
    AccessControlError,
    AccessController,
    AccessControlUnavailableError,
)
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import (
    MCPPermissionError,
    NotFoundError,
    ValidationError,
)
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError
from mcp_server_odoo.resources import (
    OdooResourceHandler,
    _guess_mimetype,
    _is_text_mimetype,
    _parse_and_validate_id,
    register_resources,
)

# 1x1 transparent PNG — starts with the PNG magic bytes
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PDF_BYTES = b"%PDF-1.4 fake pdf content"


def _binary_search_read_dispatch(record_rows, attachment_rows):
    """search_read side_effect for the binary-field handler.

    The handler issues two search_read calls: the record fetch (target model)
    and the ir.attachment mimetype lookup. Dispatch on the model; pass an
    Exception instance as ``attachment_rows`` to make the lookup raise.
    """

    def side_effect(model, domain, fields=None, **kwargs):
        if model == "ir.attachment":
            if isinstance(attachment_rows, Exception):
                raise attachment_rows
            return attachment_rows
        return record_rows

    return side_effect


@pytest.fixture
def test_config():
    """Create test configuration."""
    # Load real config from environment for integration tests
    from mcp_server_odoo.config import get_config

    return get_config()


@pytest.fixture
def mock_config():
    """Create mock configuration."""
    config = Mock(spec=OdooConfig)
    config.default_limit = 10
    config.max_limit = 100
    config.max_binary_size = 50 * 1024 * 1024
    return config


@pytest.fixture
def mock_connection():
    """Create mock OdooConnection with realistic field metadata."""
    conn = Mock(spec=OdooConnection)
    conn.is_authenticated = True
    conn.search = Mock()
    conn.read = Mock()
    # Provide realistic field metadata so safe-field filtering is actually exercised
    conn.fields_get = Mock(
        return_value={
            "id": {"type": "integer", "string": "ID"},
            "name": {"type": "char", "string": "Name"},
            "display_name": {"type": "char", "string": "Display Name"},
            "email": {"type": "char", "string": "Email"},
            "is_company": {"type": "boolean", "string": "Is a Company"},
            "country_id": {"type": "many2one", "string": "Country", "relation": "res.country"},
            "child_ids": {"type": "one2many", "string": "Contacts", "relation": "res.partner"},
            "phone": {"type": "char", "string": "Phone"},
            # Binary/image fields stay in the safe list (read with bin_size,
            # rendered as fetchable resource URIs)
            "image_1920": {"type": "binary", "string": "Image"},
            "image_128": {"type": "binary", "string": "Image 128"},
            "avatar_128": {"type": "image", "string": "Avatar 128"},
            # Fields that SHOULD be filtered out by safe-field logic
            "website_description": {"type": "html", "string": "Website Description"},
            "_serialized_data": {"type": "serialized", "string": "Serialized Data"},
            "__last_update": {"type": "datetime", "string": "Last Modified on"},
        }
    )
    return conn


@pytest.fixture
def mock_access_controller():
    """Create mock AccessController."""
    controller = Mock(spec=AccessController)
    controller.validate_model_access = Mock()
    return controller


@pytest.fixture
def mock_app():
    """Create mock FastMCP app that captures decorated resource functions."""
    app = Mock(spec=FastMCP)
    app._resources = {}

    def resource_decorator(*args, **kwargs):
        def wrapper(func):
            app._resources[func.__name__] = func
            return func

        return wrapper

    app.resource = Mock(side_effect=resource_decorator)
    return app


@pytest.fixture
def resource_handler(mock_app, mock_connection, mock_access_controller, mock_config):
    """Create OdooResourceHandler instance."""
    return OdooResourceHandler(mock_app, mock_connection, mock_access_controller, mock_config)


class TestOdooResourceHandler:
    """Test OdooResourceHandler functionality."""

    def test_init(self, mock_app, mock_connection, mock_access_controller, mock_config):
        """Test handler initialization."""
        handler = OdooResourceHandler(
            mock_app, mock_connection, mock_access_controller, mock_config
        )

        assert handler.app == mock_app
        assert handler.connection == mock_connection
        assert handler.access_controller == mock_access_controller
        assert handler.config == mock_config

        # Check that resources were registered
        assert mock_app.resource.call_count >= 1

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_success(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test successful record retrieval with safe-field filtering."""
        # Setup mocks
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [
            {
                "id": 1,
                "name": "Test Partner",
                "display_name": "Test Partner",
                "email": "test@example.com",
                "is_company": True,
                "country_id": (1, "United States"),
                "child_ids": [2, 3, 4],
                "phone": False,
                "avatar_128": "3.5 KB",  # bin_size placeholder
            }
        ]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        # Verify access control was called
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        # active_test=False: archived records must be retrievable, matching
        # the binary handlers and the get_record tool (plain read).
        mock_connection.search.assert_called_once_with(
            "res.partner", [("id", "=", 1)], context={"active_test": False}
        )

        # Verify safe-field filtering: html/serialized/private fields excluded
        read_call_args = mock_connection.read.call_args
        assert read_call_args is not None
        # read should be called with (model, ids, safe_fields, bin_size context)
        assert read_call_args[0][0] == "res.partner"
        assert read_call_args[0][1] == [1]
        assert read_call_args[0][3] == {"bin_size": True}
        safe_fields = read_call_args[0][2]
        assert isinstance(safe_fields, list)
        # Binary/image fields are included (bin_size makes the read cheap;
        # the formatter renders them as fetchable resource URIs)
        assert "image_1920" in safe_fields
        assert "image_128" in safe_fields
        assert "avatar_128" in safe_fields
        # HTML fields must be excluded
        assert "website_description" not in safe_fields
        # Serialized fields must be excluded
        assert "_serialized_data" not in safe_fields
        # Private fields (starting with __) must be excluded
        assert "__last_update" not in safe_fields
        # Normal fields must be included
        assert "name" in safe_fields
        assert "email" in safe_fields
        assert "is_company" in safe_fields

        # Check result format
        assert "Record: res.partner/1" in result
        assert "Name: Test Partner" in result
        assert "=" * 50 in result
        # Populated binary field renders as a fetchable URI with the size
        assert "avatar_128: odoo://res.partner/record/1/avatar_128 (3.5 KB)" in result

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_not_found(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test record not found error."""
        # Setup mocks
        mock_connection.search.return_value = []

        # Test retrieval
        with pytest.raises(NotFoundError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "999")

        assert "Record not found: res.partner with ID 999 does not exist" in str(exc_info.value)

        # Verify calls
        mock_access_controller.validate_model_access.assert_called_once_with("res.partner", "read")
        mock_connection.search.assert_called_once_with(
            "res.partner", [("id", "=", 999)], context={"active_test": False}
        )
        mock_connection.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_archived_record_disables_active_test(
        self, resource_handler, mock_connection
    ):
        """The existence search passes active_test=False so archived records
        (partners/products/employees) are served instead of 404ing — matching
        the binary handlers and the get_record tool (plain read)."""
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Archived Partner"}]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Archived Partner" in result
        assert mock_connection.search.call_args[1].get("context") == {"active_test": False}

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_invalid_id(self, resource_handler):
        """Test invalid record ID."""
        # Test with non-numeric ID
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "abc")

        assert "Invalid record ID 'abc'" in str(exc_info.value)

        # Test with negative ID
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "-5")

        assert "Record ID must be positive" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_oversized_id(self, resource_handler, mock_connection):
        """An id above the XML-RPC 32-bit bound fails cleanly before any RPC."""
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", str(2**31))

        assert "Invalid record ID" in str(exc_info.value)
        mock_connection.search.assert_not_called()
        mock_connection.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_huge_digit_id(self, resource_handler, mock_connection):
        """A 5000-digit id gets the clean invalid-id validation error — never
        CPython's internal int() conversion-limit text."""
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1" * 5000)

        message = str(exc_info.value)
        assert "Invalid record ID" in message
        assert "exceeds the maximum allowed value" in message
        assert "4300" not in message
        assert "Exceeds the limit" not in message
        mock_connection.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_underscore_id_rejected(self, resource_handler):
        """Underscore-bearing ids ("1_000") are rejected — int() would coerce
        them to 1000, disagreeing with the strict URI regex."""
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1_000")

        assert "Invalid record ID '1_000'" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_generic_error_sanitized(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """A generic mid-handler error must not leak an internal file path."""
        mock_connection.search.return_value = [1]
        mock_connection.read.side_effect = RuntimeError(
            "Internal failure while reading /opt/odoo/internal/x.py record data"
        )

        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        message = str(exc_info.value)
        assert "Failed to retrieve record" in message
        assert "/opt/odoo/internal/x.py" not in message

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_permission_denied(
        self, resource_handler, mock_access_controller
    ):
        """Test permission denied error."""
        # Setup mock to raise permission error
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied for res.partner"
        )

        # Test retrieval
        with pytest.raises(MCPPermissionError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        # Self-labelling refusals keep their own wording — no "Access
        # denied: Access denied ..." doubling.
        message = str(exc_info.value)
        assert message == "Access denied for res.partner"
        assert not message.lower().startswith("access denied: access denied")

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_not_authenticated(
        self, resource_handler, mock_connection
    ):
        """Test error when not authenticated."""
        # Setup mock
        mock_connection.is_authenticated = False

        # Test retrieval
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Not authenticated with Odoo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_connection_error(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test connection error during retrieval."""
        # Setup mock to raise connection error
        mock_connection.search.side_effect = OdooConnectionError("Connection lost")

        # Test retrieval
        with pytest.raises(ValidationError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Connection error:" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_read_returns_empty(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test NotFoundError when search finds ID but read returns empty list."""
        # search finds the record ID, but read returns nothing
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = []

        with pytest.raises(NotFoundError) as exc_info:
            await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "Record not found" in str(exc_info.value)
        assert "res.partner" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_record_retrieval_all_fields_unsafe(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Test fallback to unfiltered read when all fields are html/serialized/private (unsafe)."""
        # fields_get returns ONLY unsafe fields, so safe_fields will be empty
        mock_connection.fields_get.return_value = {
            "website_description": {"type": "html", "string": "Website Description"},
            "_serialized_data": {"type": "serialized", "string": "Serialized Data"},
            "__last_update": {"type": "datetime", "string": "Last Modified on"},
        }
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Unsafe Partner"}]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        # Since all fields are unsafe, safe_fields is None => fallback to no filter
        mock_connection.read.assert_called_once_with("res.partner", [1], None, {"bin_size": True})
        assert "Unsafe Partner" in result
        # Nothing credential-like was stripped, so no withholding trailer
        assert "withheld" not in result

    @pytest.mark.asyncio
    async def test_handle_record_retrieval_fields_get_fallback(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Fallback all-fields read (fields_get down) still strips credential-like fields."""
        mock_connection.search.return_value = [1]
        # fields_get raises an exception, triggering the fallback path
        mock_connection.fields_get.side_effect = Exception("fields_get unavailable")
        mock_connection.read.return_value = [
            {"id": 1, "name": "Fallback Partner", "api_key": "sk-secret-123"}
        ]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        # read should have been called without a field list (fallback)
        mock_connection.read.assert_called_once_with("res.partner", [1], None, {"bin_size": True})

        # Result should still contain the record data
        assert "res.partner" in result
        assert "Fallback Partner" in result
        # ... but never the credential-like field the fallback read pulled in
        assert "api_key" not in result
        assert "sk-secret-123" not in result
        # ... and the withholding is visible in the text, not just debug-logged
        # (wording shared with the tools surface via field_security.withheld_note)
        assert "1 credential-like field(s) withheld" in result
        assert "request them by name via the get_record/search_records tools" in result
        # Resource framing points at the tools' fields parameter (resources
        # themselves have no fields parameter to honor the advice with)

    def test_get_safe_fields_excludes_credential_like_names(
        self, resource_handler, mock_connection
    ):
        """Resources honor the same credential filter as the tools' bulk reads;
        the withheld credential names are returned alongside the safe list."""
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name"},
            "api_key": {"type": "char", "string": "API Key"},
            "webhook_secret": {"type": "char", "string": "Webhook Secret"},
            "auth_token": {"type": "char", "string": "Token"},
        }

        assert resource_handler._get_safe_fields("res.partner") == (
            ["name"],
            ["api_key", "auth_token", "webhook_secret"],
        )

    @pytest.mark.asyncio
    async def test_record_retrieval_never_reads_credential_fields(
        self, resource_handler, mock_connection
    ):
        """The record resource's bulk read excludes credential-like fields."""
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name"},
            "api_key": {"type": "char", "string": "API Key"},
        }
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test"}]

        await resource_handler._handle_record_retrieval("res.partner", "1")

        fields_read = mock_connection.read.call_args[0][2]
        assert "api_key" not in fields_read
        assert "name" in fields_read

    @pytest.mark.asyncio
    async def test_record_retrieval_normal_path_notes_withheld_fields(
        self, resource_handler, mock_connection
    ):
        """Metadata-available path: a credential field is excluded from the
        read AND the withholding is visible as the formatted-text trailer."""
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name"},
            "access_token": {"type": "char", "string": "Access Token"},
        }
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test Partner"}]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        fields_read = mock_connection.read.call_args[0][2]
        assert "access_token" not in fields_read
        assert "access_token" not in result
        # Trailer on the NORMAL path, not just the metadata-unavailable fallback
        assert "1 credential-like field(s) withheld" in result
        assert "request them by name via the get_record/search_records tools" in result
        # Resource framing points at the tools' fields parameter (resources
        # themselves have no fields parameter to honor the advice with)

    @pytest.mark.asyncio
    async def test_record_retrieval_binary_field_rendered_as_uri(
        self, resource_handler, mock_connection
    ):
        """A populated binary field is read (with bin_size) and rendered as a fetchable URI."""
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [
            {"id": 1, "name": "Test Partner", "image_128": "12.5 KB"}
        ]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        # The binary field was requested, under bin_size so only a size
        # placeholder travels over the wire
        read_call_args = mock_connection.read.call_args
        assert "image_128" in read_call_args[0][2]
        assert read_call_args[0][3] == {"bin_size": True}
        # ... and rendered as the fetchable resource URI with the size appended
        assert "image_128: odoo://res.partner/record/1/image_128 (12.5 KB)" in result

    @pytest.mark.asyncio
    async def test_record_retrieval_empty_binary_field_not_set(
        self, resource_handler, mock_connection
    ):
        """An empty binary field renders as "Not set", never as a URI."""
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test Partner", "image_128": False}]

        result = await resource_handler._handle_record_retrieval("res.partner", "1")

        assert "image_128: Not set" in result
        assert "odoo://res.partner/record/1/image_128" not in result

    def test_get_safe_fields_excludes_credential_named_binary(
        self, resource_handler, mock_connection
    ):
        """A credential-named binary field stays excluded from the safe list."""
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "string": "Name"},
            "image_128": {"type": "binary", "string": "Image 128"},
            "private_key": {"type": "binary", "string": "Private Key"},
        }

        safe_fields, withheld = resource_handler._get_safe_fields("res.partner")
        assert "image_128" in safe_fields
        assert "private_key" not in safe_fields
        assert withheld == ["private_key"]


class TestBinaryFieldResource:
    """Test the odoo://{model}/record/{id}/{field} resource handler."""

    @pytest.mark.asyncio
    async def test_read_success_with_attachment_mimetype(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Populated binary field returns raw bytes + attachment mimetype."""
        mock_connection.search_read.side_effect = _binary_search_read_dispatch(
            record_rows=[{"id": 1, "image_128": base64.b64encode(PNG_BYTES).decode("ascii")}],
            attachment_rows=[{"id": 9, "mimetype": "image/png"}],
        )

        content, mimetype = await resource_handler._handle_binary_field_read(
            "res.partner", "1", "image_128"
        )

        assert content == PNG_BYTES
        assert mimetype == "image/png"
        # Read access is validated for the record model and, when the mimetype
        # is resolved from the backing attachment, for ir.attachment too.
        mock_access_controller.validate_model_access.assert_any_call("res.partner", "read")
        mock_access_controller.validate_model_access.assert_any_call("ir.attachment", "read")
        # Two round trips: a bin_size probe (size only, no payload) so an
        # oversized field is refused before its bytes are fetched, then the
        # full-bytes fetch which must NOT pass bin_size. Both disable
        # active_test — an id-leaf domain does NOT disable it on its own, so
        # archived records would otherwise 404 here while get_record (plain
        # read) serves them and advertises their binary URIs.
        probe_call, record_call = mock_connection.search_read.call_args_list[:2]
        assert probe_call[0] == ("res.partner", [["id", "=", 1]], ["image_128"])
        assert probe_call[1] == {"context": {"active_test": False, "bin_size": True}}
        assert record_call[0] == ("res.partner", [["id", "=", 1]], ["image_128"])
        assert record_call[1] == {"context": {"active_test": False}}

    @pytest.mark.asyncio
    async def test_archived_record_binary_read_disables_active_test(
        self, resource_handler, mock_connection
    ):
        """The record fetch passes active_test=False so archived records
        (partners/products/employees) serve their binaries instead of 404ing."""
        mock_connection.search_read.side_effect = _binary_search_read_dispatch(
            record_rows=[{"id": 1, "image_128": base64.b64encode(PNG_BYTES).decode("ascii")}],
            attachment_rows=[],
        )

        await resource_handler._handle_binary_field_read("res.partner", "1", "image_128")

        # Both the probe and the payload fetch must disable active_test
        probe_call, record_call = mock_connection.search_read.call_args_list[:2]
        assert probe_call[1]["context"]["active_test"] is False
        assert record_call[1].get("context") == {"active_test": False}

    @pytest.mark.asyncio
    async def test_attachment_datas_delegates_url_type(self, resource_handler, mock_connection):
        """ir.attachment/datas via the generic binary path delegates to the
        attachment handler: a url-type attachment serves its URL instead of
        the generic 'holds no data' 404."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "link",
                "mimetype": False,
                "datas": False,
                "type": "url",
                "url": "https://example.com/doc",
            }
        ]

        content, mimetype = await resource_handler._handle_binary_field_read(
            "ir.attachment", "42", "datas"
        )

        assert content == "https://example.com/doc"
        assert mimetype == "text/uri-list"

    @pytest.mark.asyncio
    async def test_attachment_datas_delegates_binary_type(self, resource_handler, mock_connection):
        """ir.attachment/datas via the generic binary path returns the blob
        with the attachment's STORED mimetype (not a sniffed one)."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "report.pdf",
                "mimetype": "application/pdf",
                "datas": base64.b64encode(PDF_BYTES).decode("ascii"),
                "type": "binary",
                "url": False,
            }
        ]

        content, mimetype = await resource_handler._handle_binary_field_read(
            "ir.attachment", "42", "datas"
        )

        assert content == PDF_BYTES
        assert mimetype == "application/pdf"
        # Delegation happens before the generic fetch — a single attachment
        # search_read, no fields_get on ir.attachment.
        mock_connection.fields_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_mimetype_sniffed_without_attachment(self, resource_handler, mock_connection):
        """No backing attachment → mimetype sniffed from magic bytes."""
        mock_connection.search_read.side_effect = _binary_search_read_dispatch(
            record_rows=[{"id": 1, "image_128": base64.b64encode(PNG_BYTES).decode("ascii")}],
            attachment_rows=[],
        )

        content, mimetype = await resource_handler._handle_binary_field_read(
            "res.partner", "1", "image_128"
        )

        assert content == PNG_BYTES
        assert mimetype == "image/png"

    @pytest.mark.asyncio
    async def test_mimetype_lookup_denied_falls_back_to_sniffing(
        self, resource_handler, mock_connection
    ):
        """Denied ir.attachment mimetype lookup → magic-byte sniffing, not an error."""
        mock_connection.search_read.side_effect = _binary_search_read_dispatch(
            record_rows=[{"id": 1, "image_128": base64.b64encode(PNG_BYTES).decode("ascii")}],
            attachment_rows=AccessControlError("ir.attachment not enabled"),
        )

        content, mimetype = await resource_handler._handle_binary_field_read(
            "res.partner", "1", "image_128"
        )

        assert content == PNG_BYTES
        assert mimetype == "image/png"

    @pytest.mark.asyncio
    async def test_text_mimetype_returns_str(self, resource_handler, mock_connection):
        """Textual mimetypes are decoded inline as str, not returned as bytes."""
        mock_connection.search_read.side_effect = _binary_search_read_dispatch(
            record_rows=[{"id": 1, "image_128": base64.b64encode(b"hello world").decode("ascii")}],
            attachment_rows=[{"id": 9, "mimetype": "text/plain"}],
        )

        content, mimetype = await resource_handler._handle_binary_field_read(
            "res.partner", "1", "image_128"
        )

        assert content == "hello world"
        assert mimetype == "text/plain"

    @pytest.mark.asyncio
    async def test_unknown_field(self, resource_handler, mock_connection):
        with pytest.raises(NotFoundError, match="Unknown field 'no_such_field'"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "no_such_field")

    @pytest.mark.asyncio
    async def test_non_binary_field(self, resource_handler, mock_connection):
        with pytest.raises(ValidationError, match="not a binary field"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "email")

    @pytest.mark.asyncio
    async def test_empty_value(self, resource_handler, mock_connection):
        """Empty binary field (Odoo returns False) → clean NotFoundError."""
        mock_connection.search_read.return_value = [{"id": 1, "image_128": False}]

        with pytest.raises(NotFoundError, match="holds no data"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_128")

    @pytest.mark.asyncio
    async def test_missing_record(self, resource_handler, mock_connection):
        """Missing id → search_read returns [] → clean NotFoundError."""
        mock_connection.search_read.return_value = []

        with pytest.raises(NotFoundError, match="Record not found"):
            await resource_handler._handle_binary_field_read("res.partner", "999", "image_128")

    @pytest.mark.asyncio
    async def test_invalid_record_id(self, resource_handler):
        with pytest.raises(ValidationError, match="Invalid record ID"):
            await resource_handler._handle_binary_field_read("res.partner", "abc", "image_128")
        with pytest.raises(ValidationError, match="Invalid record ID"):
            await resource_handler._handle_binary_field_read("res.partner", "-1", "image_128")

    @pytest.mark.asyncio
    async def test_record_id_beyond_xmlrpc_range(self, resource_handler, mock_connection):
        """ids past the 32-bit XML-RPC marshalling limit fail fast with a
        clean invalid-id error instead of a transport OverflowError."""
        with pytest.raises(ValidationError, match="Invalid record ID"):
            await resource_handler._handle_binary_field_read("res.partner", str(2**31), "image_128")
        mock_connection.search_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_permission_denied(self, resource_handler, mock_access_controller):
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied for res.partner"
        )

        with pytest.raises(MCPPermissionError, match="Access denied"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_128")

    @pytest.mark.asyncio
    async def test_not_authenticated(self, resource_handler, mock_connection):
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError, match="Not authenticated"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_128")

    @pytest.mark.asyncio
    async def test_connection_error_wrapped(self, resource_handler, mock_connection):
        """OdooConnectionError surfaces as a clean ValidationError."""
        mock_connection.search_read.side_effect = OdooConnectionError("Odoo unreachable")

        with pytest.raises(ValidationError, match="Connection error"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_128")

    @pytest.mark.asyncio
    async def test_access_check_unavailable(self, resource_handler, mock_access_controller):
        """Access-control infrastructure failure → ValidationError, not permission error."""
        mock_access_controller.validate_model_access.side_effect = AccessControlUnavailableError(
            "MCP endpoints unreachable"
        )

        with pytest.raises(ValidationError, match="Could not verify access"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_128")

    @pytest.mark.asyncio
    async def test_unexpected_error_sanitized(self, resource_handler, mock_connection):
        """Generic failures land on the sanitize-and-wrap rung — internal
        details (like server file paths) never reach the client message."""
        mock_connection.fields_get.side_effect = RuntimeError(
            "boom reading /opt/odoo/internal/filestore_helper.py"
        )

        with pytest.raises(ValidationError, match="Failed to read binary field") as exc_info:
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_128")
        assert "/opt/odoo/internal/filestore_helper.py" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_event_loop_not_blocked_by_binary_read(self, resource_handler, mock_connection):
        """The handler's sequential blocking reads run in worker threads — a slow
        RPC must not stall the loop (regression guard for the to_thread wrapping)."""
        import asyncio
        import time

        def slow_search_read(model, domain, fields=None, **kwargs):
            time.sleep(0.2)  # blocks its worker thread, must not block the loop
            if model == "ir.attachment":
                return [{"id": 9, "mimetype": "image/png"}]
            return [{"id": 1, "image_128": base64.b64encode(PNG_BYTES).decode("ascii")}]

        mock_connection.search_read.side_effect = slow_search_read

        start = time.monotonic()
        read_task = asyncio.create_task(
            resource_handler._handle_binary_field_read("res.partner", "1", "image_128")
        )
        # An independent awaitable must make progress while the RPC blocks.
        await asyncio.sleep(0.01)
        heartbeat_elapsed = time.monotonic() - start

        content, mimetype = await read_task
        assert content == PNG_BYTES
        assert mimetype == "image/png"
        assert heartbeat_elapsed < 0.15, (
            f"event loop was blocked for {heartbeat_elapsed:.3f}s by a synchronous RPC"
        )


class TestAttachmentResource:
    """Test the odoo://attachment/{id} resource handler."""

    @pytest.mark.asyncio
    async def test_read_binary_attachment(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """Binary attachment returns raw bytes + its stored mimetype."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "report.pdf",
                "mimetype": "application/pdf",
                "datas": base64.b64encode(PDF_BYTES).decode("ascii"),
                "type": "binary",
                "url": False,
            }
        ]

        content, mimetype = await resource_handler._handle_attachment_read("42")

        assert content == PDF_BYTES
        assert mimetype == "application/pdf"
        mock_access_controller.validate_model_access.assert_called_once_with(
            "ir.attachment", "read"
        )
        # Metadata (incl. the stored file_size) first, payload second — so an
        # oversized attachment is refused before its bytes are pulled in.
        # active_test=False: id-leaf domains do NOT disable Odoo's
        # active_test — without it archived records would 404 (ir.attachment
        # has no `active` field today; passed for symmetry/future-proofing).
        meta_call, payload_call = mock_connection.search_read.call_args_list[:2]
        assert meta_call[0] == (
            "ir.attachment",
            [["id", "=", 42]],
            ["name", "mimetype", "type", "url", "file_size", "res_model"],
        )
        assert meta_call[1] == {"context": {"active_test": False}}
        assert payload_call[0] == ("ir.attachment", [["id", "=", 42]], ["datas"])

    @pytest.mark.asyncio
    async def test_read_text_attachment(self, resource_handler, mock_connection):
        """Textual mimetype → content served as str."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "data.csv",
                "mimetype": "text/csv",
                "datas": base64.b64encode(b"a,b\n1,2").decode("ascii"),
                "type": "binary",
                "url": False,
            }
        ]

        content, mimetype = await resource_handler._handle_attachment_read("42")

        assert content == "a,b\n1,2"
        assert mimetype == "text/csv"

    @pytest.mark.asyncio
    async def test_read_url_attachment(self, resource_handler, mock_connection):
        """URL-type attachment returns its URL as text."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "link",
                "mimetype": False,
                "datas": False,
                "type": "url",
                "url": "https://example.com/doc",
            }
        ]

        content, mimetype = await resource_handler._handle_attachment_read("42")

        assert content == "https://example.com/doc"
        assert mimetype == "text/uri-list"

    @pytest.mark.asyncio
    async def test_missing_mimetype_sniffed(self, resource_handler, mock_connection):
        """No stored mimetype → sniffed from magic bytes."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "img",
                "mimetype": False,
                "datas": base64.b64encode(PNG_BYTES).decode("ascii"),
                "type": "binary",
                "url": False,
            }
        ]

        content, mimetype = await resource_handler._handle_attachment_read("42")

        assert content == PNG_BYTES
        assert mimetype == "image/png"

    @pytest.mark.asyncio
    async def test_url_attachment_without_url(self, resource_handler, mock_connection):
        """type='url' with an empty url field → clean NotFoundError."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "broken link",
                "mimetype": False,
                "datas": False,
                "type": "url",
                "url": False,
            }
        ]

        with pytest.raises(NotFoundError, match="URL attachment without a URL"):
            await resource_handler._handle_attachment_read("42")

    @pytest.mark.asyncio
    async def test_empty_datas_raises_not_found(self, resource_handler, mock_connection):
        """Empty binary attachment (datas False) → NotFoundError, consistent
        with the empty-binary-field behavior (no zero-byte blob)."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "empty.bin",
                "mimetype": "application/octet-stream",
                "datas": False,
                "type": "binary",
                "url": False,
            }
        ]

        with pytest.raises(NotFoundError, match="holds no data"):
            await resource_handler._handle_attachment_read("42")

    @pytest.mark.asyncio
    async def test_attachment_not_found(self, resource_handler, mock_connection):
        """Missing id → search_read returns [] → clean NotFoundError."""
        mock_connection.search_read.return_value = []

        with pytest.raises(NotFoundError, match="Attachment not found"):
            await resource_handler._handle_attachment_read("999")

    @pytest.mark.asyncio
    async def test_invalid_attachment_id(self, resource_handler):
        with pytest.raises(ValidationError, match="Invalid attachment ID"):
            await resource_handler._handle_attachment_read("abc")
        with pytest.raises(ValidationError, match="Attachment ID must be positive"):
            await resource_handler._handle_attachment_read("-1")

    @pytest.mark.asyncio
    async def test_attachment_id_beyond_xmlrpc_range(self, resource_handler, mock_connection):
        """ids past the 32-bit XML-RPC marshalling limit fail fast with a
        clean invalid-id error instead of a transport OverflowError."""
        with pytest.raises(ValidationError, match="Invalid attachment ID"):
            await resource_handler._handle_attachment_read(str(2**31))
        mock_connection.search_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_permission_denied(self, resource_handler, mock_access_controller):
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied for ir.attachment"
        )

        with pytest.raises(MCPPermissionError, match="Access denied"):
            await resource_handler._handle_attachment_read("42")

    @pytest.mark.asyncio
    async def test_not_authenticated(self, resource_handler, mock_connection):
        mock_connection.is_authenticated = False

        with pytest.raises(ValidationError, match="Not authenticated"):
            await resource_handler._handle_attachment_read("42")

    @pytest.mark.asyncio
    async def test_connection_error_wrapped(self, resource_handler, mock_connection):
        """OdooConnectionError surfaces as a clean ValidationError."""
        mock_connection.search_read.side_effect = OdooConnectionError("Odoo unreachable")

        with pytest.raises(ValidationError, match="Connection error"):
            await resource_handler._handle_attachment_read("42")

    @pytest.mark.asyncio
    async def test_access_check_unavailable(self, resource_handler, mock_access_controller):
        """Access-control infrastructure failure → ValidationError, not permission error."""
        mock_access_controller.validate_model_access.side_effect = AccessControlUnavailableError(
            "MCP endpoints unreachable"
        )

        with pytest.raises(ValidationError, match="Could not verify access"):
            await resource_handler._handle_attachment_read("42")

    @pytest.mark.asyncio
    async def test_unexpected_error_sanitized(self, resource_handler, mock_connection):
        """Generic failures land on the sanitize-and-wrap rung — internal
        details (like server file paths) never reach the client message."""
        mock_connection.search_read.side_effect = RuntimeError(
            "boom reading /opt/odoo/internal/filestore_helper.py"
        )

        with pytest.raises(ValidationError, match="Failed to read attachment") as exc_info:
            await resource_handler._handle_attachment_read("42")
        assert "/opt/odoo/internal/filestore_helper.py" not in str(exc_info.value)


class TestDecodeBinaryValue:
    """Direct coverage of the XML-RPC binary value decoding branches."""

    def test_xmlrpc_binary_wrapper(self):
        value = xmlrpc.client.Binary(PNG_BYTES)
        assert OdooResourceHandler._decode_binary_value(value) == PNG_BYTES

    def test_raw_bytes_passthrough(self):
        assert OdooResourceHandler._decode_binary_value(PDF_BYTES) == PDF_BYTES

    def test_invalid_base64_string(self):
        with pytest.raises(ValidationError, match="Could not decode binary value"):
            OdooResourceHandler._decode_binary_value("abc")

    def test_unexpected_type(self):
        with pytest.raises(ValidationError, match="Unexpected binary value type"):
            OdooResourceHandler._decode_binary_value(12345)


class TestMimetypeHelpers:
    """Direct coverage of the mimetype sniffing/classification helpers."""

    def test_guess_mimetype_known_signatures(self):
        assert _guess_mimetype(PNG_BYTES) == "image/png"
        assert _guess_mimetype(PDF_BYTES) == "application/pdf"

    def test_guess_mimetype_unknown_bytes_fall_back_to_octet_stream(self):
        assert _guess_mimetype(b"no magic bytes here") == "application/octet-stream"

    @pytest.mark.parametrize(
        "raw",
        [
            b'<svg xmlns="http://www.w3.org/2000/svg"/>',
            b'  \n<SVG xmlns="http://www.w3.org/2000/svg"/>',  # leading space, upper case
            b"<?xml version='1.0' encoding='UTF-8'?><svg width='1'/>",
            b'\xef\xbb\xbf<?xml version="1.0"?><svg/>',  # UTF-8 BOM
            b"<!DOCTYPE svg PUBLIC '-//W3C//DTD SVG 1.1//EN'><svg/>",
            b"<!-- generated by Odoo --><svg/>",
        ],
        ids=["bare", "whitespace-upper", "xml-prolog", "bom", "doctype", "comment"],
    )
    def test_guess_mimetype_detects_svg(self, raw):
        """Odoo renders every default avatar as SVG, so this is the most common
        binary field served over MCP — it must not degrade to octet-stream."""
        assert _guess_mimetype(raw) == "image/svg+xml"

    @pytest.mark.parametrize(
        "raw",
        [
            b"<!DOCTYPE html><html><body><svg viewBox='0 0'/></body></html>",
            b"<html><svg/></html>",
            b"<!-- header --><html><body><svg/></body></html>",
            b"<?xml version='1.0'?><rss><channel/></rss>",
            b"<config><svg-enabled>1</svg-enabled></config>",
            b"plain text mentioning <svg> in prose",
        ],
        ids=[
            "html-doctype",
            "html-root",
            "html-comment-led",
            "non-svg-xml",
            "unrelated-xml",
            "prose",
        ],
    )
    def test_guess_mimetype_does_not_mistake_other_markup_for_svg(self, raw):
        """An inline <svg> inside an HTML page (or the word in prose) is not an
        SVG document — only <svg> as the root element counts."""
        assert _guess_mimetype(raw) != "image/svg+xml"

    def test_guess_mimetype_svg_scan_is_bounded_to_the_head(self):
        """A huge XML file whose <svg> appears past the sniff window is not
        scanned end-to-end (and is not claimed to be SVG)."""
        raw = b"<?xml version='1.0'?><wrapper>" + b" " * 5000 + b"<svg/></wrapper>"

        assert _guess_mimetype(raw) == "application/octet-stream"

    def test_guess_mimetype_binary_signature_wins_over_svg_scan(self):
        """Magic bytes are checked first: a PNG is never re-sniffed as markup."""
        assert _guess_mimetype(PNG_BYTES) == "image/png"

    def test_svg_is_served_inline_as_text(self):
        """image/svg+xml ends in +xml, so it rides the textual path rather than
        being base64-blobbed — keeping avatars readable to the client."""
        assert _is_text_mimetype(_guess_mimetype(b"<svg/>"))

    @pytest.mark.parametrize(
        "mimetype",
        [
            "text/plain",
            "Text/Plain; CHARSET=UTF-8",  # charset stripped, case folded
            "application/json",  # base in _TEXT_MIMETYPES
            "application/ld+json",  # +json suffix rule
            "image/svg+xml",  # +xml suffix rule
        ],
    )
    def test_is_text_mimetype_accepts(self, mimetype):
        assert _is_text_mimetype(mimetype) is True

    @pytest.mark.parametrize(
        "mimetype",
        [
            "application/pdf",
            "image/png",
            "application/octet-stream",
            "",
            None,
        ],
    )
    def test_is_text_mimetype_rejects(self, mimetype):
        assert _is_text_mimetype(mimetype) is False


class TestParseAndValidateId:
    """Direct coverage of the shared id parse/bounds helper."""

    def test_numeric_ok(self):
        assert _parse_and_validate_id("42", "Record ID") == 42

    # "--5" would leak int()'s "invalid literal" text under the old
    # lstrip-based gate; "١٢" (Unicode digits) would be silently coerced.
    @pytest.mark.parametrize("raw", ["abc", "1_000", "0x10", " 5 ", "--5", "١٢"])
    def test_non_plain_integer_rejected(self, raw):
        with pytest.raises(ValueError, match="must be a plain integer"):
            _parse_and_validate_id(raw, "Record ID")

    @pytest.mark.parametrize("raw", ["0", "-3", "-5"])
    def test_non_positive_rejected(self, raw):
        with pytest.raises(ValueError, match="Record ID must be positive"):
            _parse_and_validate_id(raw, "Record ID")

    def test_oversized_rejected(self):
        with pytest.raises(ValueError, match="exceeds the maximum allowed value"):
            _parse_and_validate_id(str(2**31), "Record ID")

    def test_huge_digit_count_rejected_before_int(self):
        """A 5000-digit id fails on the clean bounds message — the length
        guard fires before int(), whose CPython-internal 4300-digit error
        ("Exceeds the limit ...") must never escape."""
        with pytest.raises(ValueError) as exc_info:
            _parse_and_validate_id("1" * 5000, "Record ID")
        message = str(exc_info.value)
        assert "exceeds the maximum allowed value" in message
        assert "4300" not in message
        assert "Exceeds the limit" not in message


class TestTextOrBlob:
    """_text_or_blob honors the declared charset and falls back to blob."""

    def test_utf8_default_returns_str(self):
        content, mimetype = OdooResourceHandler._text_or_blob("héllo".encode("utf-8"), "text/plain")
        assert content == "héllo"
        assert mimetype == "text/plain"

    def test_declared_charset_decoded(self):
        raw = "héllo, wörld".encode("iso-8859-1")
        content, mimetype = OdooResourceHandler._text_or_blob(raw, "text/csv; charset=iso-8859-1")
        assert content == "héllo, wörld"
        assert mimetype == "text/csv; charset=iso-8859-1"

    def test_undecodable_text_served_as_blob(self):
        raw = b"\xff\xfe invalid utf-8"
        content, mimetype = OdooResourceHandler._text_or_blob(raw, "text/plain")
        assert content == raw
        assert mimetype == "text/plain"

    def test_unknown_charset_served_as_blob(self):
        raw = b"hello"
        content, _ = OdooResourceHandler._text_or_blob(raw, "text/plain; charset=not-a-codec")
        assert content == raw

    def test_binary_mimetype_stays_bytes(self):
        content, _ = OdooResourceHandler._text_or_blob(PNG_BYTES, "image/png")
        assert content == PNG_BYTES


class TestDecoratedResourceFallbacks:
    """The decorated template functions (fallback read path) execute end-to-end."""

    @pytest.mark.asyncio
    async def test_get_binary_field_fallback_function(
        self, resource_handler, mock_app, mock_connection
    ):
        mock_connection.search_read.side_effect = _binary_search_read_dispatch(
            record_rows=[{"id": 1, "image_128": base64.b64encode(PNG_BYTES).decode("ascii")}],
            attachment_rows=[],
        )

        content = await mock_app._resources["get_binary_field"]("res.partner", "1", "image_128")

        assert content == PNG_BYTES

    @pytest.mark.asyncio
    async def test_get_attachment_fallback_function(
        self, resource_handler, mock_app, mock_connection
    ):
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "report.pdf",
                "mimetype": "application/pdf",
                "datas": base64.b64encode(PDF_BYTES).decode("ascii"),
                "type": "binary",
                "url": False,
            }
        ]

        content = await mock_app._resources["get_attachment"]("42")

        assert content == PDF_BYTES


class TestBinaryReadOverride:
    """Test the low-level resources/read override (dynamic mimeTypes)."""

    @pytest.fixture
    def real_app_handler(self, mock_connection, mock_access_controller, mock_config):
        """Handler registered on a real FastMCP app (installs the override)."""
        app = FastMCP("test-app")
        handler = OdooResourceHandler(app, mock_connection, mock_access_controller, mock_config)
        return app, handler

    async def _read(self, app, uri: str):
        """Invoke the registered low-level resources/read handler."""
        handler = app._mcp_server.request_handlers[types.ReadResourceRequest]
        request = types.ReadResourceRequest(
            method="resources/read",
            params=types.ReadResourceRequestParams(uri=uri),
        )
        result = await handler(request)
        return result.root.contents

    @pytest.mark.asyncio
    async def test_new_templates_advertised(self, real_app_handler):
        app, _handler = real_app_handler
        templates = await app.list_resource_templates()
        uris = [t.uriTemplate for t in templates]
        assert "odoo://{model}/record/{record_id}/{field}" in uris
        assert "odoo://attachment/{attachment_id}" in uris

    @pytest.mark.asyncio
    async def test_binary_field_blob_with_dynamic_mimetype(self, real_app_handler, mock_connection):
        """Binary field read returns a blob whose mimeType varies per read."""
        app, _handler = real_app_handler
        mock_connection.search_read.side_effect = _binary_search_read_dispatch(
            record_rows=[{"id": 1, "image_128": base64.b64encode(PNG_BYTES).decode("ascii")}],
            attachment_rows=[{"id": 9, "mimetype": "image/png"}],
        )

        contents = await self._read(app, "odoo://res.partner/record/1/image_128")

        assert len(contents) == 1
        content = contents[0]
        assert isinstance(content, types.BlobResourceContents)
        assert content.mimeType == "image/png"
        # Blob round-trips byte-identical
        assert base64.b64decode(content.blob) == PNG_BYTES

    @pytest.mark.asyncio
    async def test_attachment_url_served_as_text(self, real_app_handler, mock_connection):
        app, _handler = real_app_handler
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "link",
                "mimetype": False,
                "datas": False,
                "type": "url",
                "url": "https://example.com/doc",
            }
        ]

        contents = await self._read(app, "odoo://attachment/42")

        assert isinstance(contents[0], types.TextResourceContents)
        assert contents[0].mimeType == "text/uri-list"
        assert contents[0].text == "https://example.com/doc"

    @pytest.mark.asyncio
    async def test_huge_digit_id_clean_error_via_dispatch(self, real_app_handler, mock_connection):
        """A 5000-digit id in a binary-field URI fails with the handlers'
        clean invalid-id error — the dispatch passes ids as raw strings, so
        CPython's int() conversion-limit text never escapes."""
        app, _handler = real_app_handler

        with pytest.raises(ValidationError) as exc_info:
            await self._read(app, f"odoo://res.partner/record/{'1' * 5000}/image_128")

        message = str(exc_info.value)
        assert "Invalid record ID" in message
        assert "4300" not in message
        assert "Exceeds the limit" not in message
        mock_connection.search_read.assert_not_called()

    @pytest.mark.asyncio
    async def test_attachment_datas_uri_delegates_via_dispatch(
        self, real_app_handler, mock_connection
    ):
        """The generic odoo://ir.attachment/record/{id}/datas form serves a
        url-type attachment's URL — identical to odoo://attachment/{id}."""
        app, _handler = real_app_handler
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "link",
                "mimetype": False,
                "datas": False,
                "type": "url",
                "url": "https://example.com/doc",
            }
        ]

        contents = await self._read(app, "odoo://ir.attachment/record/42/datas")

        assert isinstance(contents[0], types.TextResourceContents)
        assert contents[0].mimeType == "text/uri-list"
        assert contents[0].text == "https://example.com/doc"

    @pytest.mark.asyncio
    async def test_non_binary_uris_delegate_to_fastmcp(self, real_app_handler, mock_connection):
        """The 3-segment record template still serves formatted text."""
        app, _handler = real_app_handler
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test Partner"}]

        contents = await self._read(app, "odoo://res.partner/record/1")

        assert isinstance(contents[0], types.TextResourceContents)
        assert "Record: res.partner/1" in contents[0].text

    @pytest.mark.asyncio
    async def test_override_install_idempotent_per_handler(
        self, real_app_handler, mock_connection, mock_access_controller, mock_config
    ):
        """Same handler skips re-install; a new handler replaces, not chains."""
        app, handler = real_app_handler
        installed = app._mcp_server.request_handlers[types.ReadResourceRequest]

        # Re-install by the SAME handler is a no-op (owner sentinel)
        handler._install_binary_read_override()
        assert app._mcp_server.request_handlers[types.ReadResourceRequest] is installed

        # A DIFFERENT handler on the same app replaces the dispatcher
        # (plain dict assignment in the SDK — never chains onto the old one)
        new_handler = OdooResourceHandler(app, mock_connection, mock_access_controller, mock_config)
        replaced = app._mcp_server.request_handlers[types.ReadResourceRequest]
        assert replaced is not installed
        assert app._mcp_server._odoo_binary_override_owner is new_handler

        # Reads still resolve normally through the replacement dispatcher
        mock_connection.search.return_value = [1]
        mock_connection.read.return_value = [{"id": 1, "name": "Test Partner"}]
        contents = await self._read(app, "odoo://res.partner/record/1")
        assert isinstance(contents[0], types.TextResourceContents)
        assert "Record: res.partner/1" in contents[0].text


class TestRegisterResources:
    """Test register_resources function."""

    def test_register_resources(
        self, mock_app, mock_connection, mock_access_controller, mock_config
    ):
        """Test resource registration."""
        handler = register_resources(mock_app, mock_connection, mock_access_controller, mock_config)

        assert isinstance(handler, OdooResourceHandler)
        assert handler.app == mock_app
        assert handler.connection == mock_connection
        assert handler.access_controller == mock_access_controller
        assert handler.config == mock_config

        # Check that resources were registered
        assert mock_app.resource.call_count >= 1


class TestResourceIntegration:
    """Integration tests with real Odoo server."""

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_real_record_retrieval(self, test_config):
        """Test record retrieval with real server."""
        # Create real connection
        connection = OdooConnection(test_config)
        connection.connect()
        connection.authenticate()

        # Create access controller
        access_controller = AccessController(test_config)

        # Create FastMCP app
        app = FastMCP("test-app")

        # Register resources
        handler = register_resources(app, connection, access_controller, test_config)

        try:
            # Search for a partner record. Base data guarantees partners
            # exist — an empty result would mean the search itself broke,
            # which must FAIL, not vacuously pass.
            partner_ids = connection.search("res.partner", [], limit=1)
            assert partner_ids, "expected at least one res.partner record"

            # Test retrieval
            result = await handler._handle_record_retrieval("res.partner", str(partner_ids[0]))

            # Verify result format
            assert f"Record: res.partner/{partner_ids[0]}" in result
            assert "Name:" in result
            assert "=" * 50 in result  # Separator line

        finally:
            connection.disconnect()

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_real_record_not_found(self, test_config):
        """Test record not found with real server."""
        # Create real connection
        connection = OdooConnection(test_config)
        connection.connect()
        connection.authenticate()

        # Create access controller
        access_controller = AccessController(test_config)

        # Create FastMCP app
        app = FastMCP("test-app")

        # Register resources
        handler = register_resources(app, connection, access_controller, test_config)

        try:
            # Test with non-existent ID
            with pytest.raises(NotFoundError):
                await handler._handle_record_retrieval("res.partner", "999999999")

        finally:
            connection.disconnect()

    @pytest.mark.mcp
    @pytest.mark.asyncio
    async def test_real_permission_denied(self, test_config):
        """Test permission denied with real server."""
        # Create real connection
        connection = OdooConnection(test_config)
        connection.connect()
        connection.authenticate()

        # Create access controller
        access_controller = AccessController(test_config)

        # Create FastMCP app
        app = FastMCP("test-app")

        # Register resources
        handler = register_resources(app, connection, access_controller, test_config)

        try:
            # Use a fake model that fails in both modes: permission denied
            # in standard mode, connection error in YOLO mode
            with pytest.raises((MCPPermissionError, ValidationError)):
                await handler._handle_record_retrieval("nonexistent.model.xyz", "1")

        finally:
            connection.disconnect()


class TestBinarySizeCap:
    """A resources/read buffers the whole payload and the MCP layer re-encodes
    it to base64 (~2.3x peak), so an oversized read is refused up front.
    Populated binaries are advertised as odoo:// URIs now, so clients follow
    them and the ceiling has to be real.
    """

    @pytest.mark.asyncio
    async def test_oversized_attachment_refused_before_decode(
        self, resource_handler, mock_connection, mock_access_controller, mock_config
    ):
        mock_config.max_binary_size = 1024  # 1 KB
        oversized = base64.b64encode(b"x" * 50_000).decode("ascii")
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "huge.bin",
                "mimetype": "application/octet-stream",
                "datas": oversized,
                "type": "binary",
                "url": False,
            }
        ]

        with pytest.raises(ValidationError, match="limit for a single read"):
            await resource_handler._handle_attachment_read("42")

    @pytest.mark.asyncio
    async def test_oversized_binary_field_refused(
        self, resource_handler, mock_connection, mock_access_controller, mock_config
    ):
        mock_config.max_binary_size = 1024
        mock_connection.search_read.return_value = [
            {"id": 1, "image_1920": base64.b64encode(b"x" * 50_000).decode("ascii")}
        ]

        with pytest.raises(ValidationError, match="limit for a single read"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_1920")

    @pytest.mark.asyncio
    async def test_payload_within_limit_still_served(
        self, resource_handler, mock_connection, mock_access_controller, mock_config
    ):
        mock_config.max_binary_size = 50 * 1024 * 1024
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "report.pdf",
                "mimetype": "application/pdf",
                "datas": base64.b64encode(PDF_BYTES).decode("ascii"),
                "type": "binary",
                "url": False,
            }
        ]

        content, mimetype = await resource_handler._handle_attachment_read("42")
        assert content == PDF_BYTES


class TestBinarySizeCapBoundsTheFetch:
    """The cap has to refuse BEFORE the payload is pulled into the process —
    checking after the fetch would only bound the decode, and the fetch is
    where an oversized attachment actually exhausts memory.
    """

    @pytest.mark.asyncio
    async def test_binary_field_refused_before_payload_is_fetched(
        self, resource_handler, mock_connection, mock_config
    ):
        mock_config.max_binary_size = 1024
        # The bin_size probe returns Odoo's human_size placeholder
        mock_connection.search_read.return_value = [{"id": 1, "image_1920": "512.00 Mb"}]

        with pytest.raises(ValidationError, match="limit for a single read"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_1920")

        # Refused on the probe — the full-bytes read never happened
        assert mock_connection.search_read.call_count == 1
        assert mock_connection.search_read.call_args[1]["context"]["bin_size"] is True

    @pytest.mark.asyncio
    async def test_attachment_refused_before_payload_is_fetched(
        self, resource_handler, mock_connection, mock_config
    ):
        mock_config.max_binary_size = 1024
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "huge.bin",
                "mimetype": "application/octet-stream",
                "type": "binary",
                "url": False,
                "file_size": 500 * 1024 * 1024,
            }
        ]

        with pytest.raises(ValidationError, match="limit for a single read"):
            await resource_handler._handle_attachment_read("42")

        # Metadata only — 'datas' was never requested
        assert mock_connection.search_read.call_count == 1
        assert "datas" not in mock_connection.search_read.call_args[0][2]

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ("QUJD", 3),  # base64 str -> 3 decoded bytes
            (b"abcd", 4),  # raw bytes are their own length
            (xmlrpc.client.Binary(b"abcde"), 5),  # Binary wraps raw bytes
            (12345, None),  # unknown -> caller falls through
        ],
    )
    def test_payload_size_matches_decode_semantics(self, resource_handler, payload, expected):
        """Sizing must mirror _decode_binary_value, which treats Binary and
        raw bytes as already-decoded and a str as base64."""
        assert resource_handler._payload_size_bytes(payload) == expected

    @pytest.mark.parametrize(
        "placeholder,expected",
        [
            ("80.00 bytes", 80),
            ("12.50 Kb", 12800),
            ("12.5 KB", 12800),
            ("1.00 Mb", 1024 * 1024),
            ("2.00 Gb", 2 * 1024**3),
            ("not a size", None),
            ("iVBORw0KGgoAAAANSUhEUg", None),  # a base64 payload, not a placeholder
        ],
    )
    def test_size_placeholder_parsing(self, resource_handler, placeholder, expected):
        assert resource_handler._parse_size_placeholder(placeholder) == expected

    @pytest.mark.asyncio
    async def test_unparseable_placeholder_falls_through_to_post_fetch_check(
        self, resource_handler, mock_connection, mock_config
    ):
        """If the probe value is not a recognizable placeholder, the read
        still proceeds and the post-fetch checkpoint does the work."""
        mock_config.max_binary_size = 16
        oversized = base64.b64encode(b"x" * 4096).decode("ascii")
        mock_connection.search_read.return_value = [{"id": 1, "image_1920": oversized}]

        with pytest.raises(ValidationError, match="limit for a single read"):
            await resource_handler._handle_binary_field_read("res.partner", "1", "image_1920")

        # Probe did not parse, so the payload read was attempted
        assert mock_connection.search_read.call_count == 2


class TestComputedBinaryFieldsExcludedFromBulkReads:
    """bin_size turns a STORED binary into a cheap size placeholder, but it
    cannot short-circuit a compute: a non-stored binary runs its Python per
    row. Measured on Odoo 19, sale.order.tax_totals makes a 10-row read 3.3x
    slower and res.partner's six computed avatars 2.2x — and several of them
    are dict-valued widgets that are not fetchable binaries at all.
    """

    def test_non_stored_binaries_dropped_stored_ones_kept(self, resource_handler, mock_connection):
        mock_connection.fields_get.return_value = {
            "name": {"type": "char", "store": True},
            "image_1920": {"type": "binary", "store": True},
            "avatar_1920": {"type": "binary", "store": False},
            "tax_totals": {"type": "binary", "store": False},
            "comment": {"type": "html", "store": True},
        }

        safe_fields, _ = resource_handler._get_safe_fields("res.partner")

        assert "name" in safe_fields
        # Stored binaries stay: the odoo:// resources must remain discoverable
        assert "image_1920" in safe_fields
        # Computed binaries cost a per-row compute for no reliable payload
        assert "avatar_1920" not in safe_fields
        assert "tax_totals" not in safe_fields
        # Pre-existing exclusions unchanged
        assert "comment" not in safe_fields

    def test_binary_without_store_metadata_is_kept(self, resource_handler, mock_connection):
        """Absent metadata means stored, per fields_get's own default."""
        mock_connection.fields_get.return_value = {
            "name": {"type": "char"},
            "image_1920": {"type": "binary"},
        }

        safe_fields, _ = resource_handler._get_safe_fields("res.partner")

        assert "image_1920" in safe_fields


class TestCountAndFieldsErrorsAreSanitized:
    """These two handlers interpolated the raw exception while their four
    siblings sanitized — the same leak the sanitizer exists to close."""

    @pytest.mark.asyncio
    async def test_count_error_is_sanitized(self, resource_handler, mock_connection):
        mock_connection.search_count.side_effect = RuntimeError(
            'File "/opt/odoo/addons/sale/models/sale_order.py", line 42, in _compute'
        )

        with pytest.raises(ValidationError) as exc:
            await resource_handler._handle_count("res.partner", [])

        assert "/opt/odoo/addons" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_fields_error_is_sanitized(self, resource_handler, mock_connection):
        mock_connection.fields_get.side_effect = RuntimeError(
            'File "/opt/odoo/addons/base/models/res_partner.py", line 7, in fields_get'
        )

        with pytest.raises(ValidationError) as exc:
            await resource_handler._handle_fields("res.partner")

        assert "/opt/odoo/addons" not in str(exc.value)


class TestAttachmentGatesOnAttachedModel:
    """Checking only ir.attachment would sidestep the enabled-model allowlist
    wholesale: enable ir.attachment and every attachment body on the database
    becomes readable, including documents hanging off models deliberately left
    out of the allowlist.
    """

    def _meta_row(self, res_model):
        return [
            {
                "id": 42,
                "name": "payslip.pdf",
                "mimetype": "application/pdf",
                "type": "binary",
                "url": False,
                "file_size": len(PDF_BYTES),
                "res_model": res_model,
            }
        ]

    @pytest.mark.asyncio
    async def test_denied_when_attached_model_not_accessible(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        mock_connection.search_read.return_value = self._meta_row("hr.payslip")

        def gate(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("Model 'hr.payslip' is not enabled for MCP access")

        mock_access_controller.validate_model_access.side_effect = gate

        with pytest.raises(MCPPermissionError, match="hr.payslip"):
            await resource_handler._handle_attachment_read("42")

        # Refused on metadata — the payload was never fetched
        assert mock_connection.search_read.call_count == 1

    @pytest.mark.asyncio
    async def test_allowed_when_attached_model_is_accessible(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        mock_connection.search_read.side_effect = [
            self._meta_row("res.partner"),
            [{"id": 42, "datas": base64.b64encode(PDF_BYTES).decode("ascii")}],
        ]
        mock_access_controller.validate_model_access.return_value = None

        content, _ = await resource_handler._handle_attachment_read("42")

        assert content == PDF_BYTES
        mock_access_controller.validate_model_access.assert_any_call("ir.attachment", "read")
        mock_access_controller.validate_model_access.assert_any_call("res.partner", "read")

    @pytest.mark.asyncio
    async def test_standalone_attachment_needs_only_the_attachment_gate(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """No res_model means there is no second model to check."""
        mock_connection.search_read.side_effect = [
            self._meta_row(False),
            [{"id": 42, "datas": base64.b64encode(PDF_BYTES).decode("ascii")}],
        ]
        mock_access_controller.validate_model_access.return_value = None

        content, _ = await resource_handler._handle_attachment_read("42")

        assert content == PDF_BYTES
        assert mock_access_controller.validate_model_access.call_count == 1

    @pytest.mark.parametrize("field", ["raw", "db_datas", "thumbnail"])
    @pytest.mark.asyncio
    async def test_other_attachment_binary_fields_are_gated_too(
        self, resource_handler, mock_connection, mock_access_controller, field
    ):
        """datas, raw and db_datas are delegated to the attachment handler,
        which gates on the metadata it fetches; thumbnail reaches the generic
        binary path and is gated by _gate_attachment_row. Every one of them
        must refuse an attachment hanging off an inaccessible res_model."""
        mock_connection.search_read.return_value = [{"id": 42, "res_model": "hr.payslip"}]

        def gate(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("Model 'hr.payslip' is not enabled for MCP access")

        mock_access_controller.validate_model_access.side_effect = gate

        with pytest.raises(MCPPermissionError, match="hr.payslip"):
            await resource_handler._handle_binary_field_read("ir.attachment", "42", field)

    @pytest.mark.asyncio
    async def test_url_attachment_is_gated_too(
        self, resource_handler, mock_connection, mock_access_controller
    ):
        """A url-type attachment still reveals data about the attached record."""
        mock_connection.search_read.return_value = [
            {
                "id": 42,
                "name": "link",
                "mimetype": False,
                "type": "url",
                "url": "https://example.com/secret",
                "file_size": 0,
                "res_model": "hr.payslip",
            }
        ]

        def gate(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("not enabled")

        mock_access_controller.validate_model_access.side_effect = gate

        with pytest.raises(MCPPermissionError):
            await resource_handler._handle_attachment_read("42")


class TestAttachmentMetadataGatingInResources:
    """The record/search/count resources reach ir.attachment metadata without
    ever touching a payload, so the payload gate alone leaves the allowlist
    sidestep open for `url` and `index_content` (the extracted document text).
    """

    @pytest.fixture
    def scoped_config(self, mock_config):
        # Mock(spec=OdooConfig) leaves is_yolo_enabled truthy, which
        # short-circuits scoping entirely — standard mode is the case here.
        mock_config.is_yolo_enabled = False
        return mock_config

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, scoped_config):
        mock_access_controller.get_enabled_models = Mock(
            return_value=[{"model": "res.partner", "name": "Contact"}]
        )
        return OdooResourceHandler(mock_app, mock_connection, mock_access_controller, scoped_config)

    @pytest.mark.asyncio
    async def test_record_resource_refuses_denied_attachment(
        self, handler, mock_connection, mock_access_controller
    ):
        mock_connection.search_read = Mock(return_value=[{"id": 42, "res_model": "hr.payslip"}])

        def gate(model, operation):
            if model == "hr.payslip":
                raise AccessControlError("not enabled")

        mock_access_controller.validate_model_access.side_effect = gate

        with pytest.raises(MCPPermissionError, match="hr.payslip"):
            await handler._handle_record_retrieval("ir.attachment", "42")

        # Refused before the row was read, not filtered out of the output
        mock_connection.read.assert_not_called()

    @pytest.mark.asyncio
    async def test_record_resource_allows_permitted_attachment(
        self, handler, mock_connection, mock_access_controller
    ):
        mock_connection.search_read = Mock(return_value=[{"id": 42, "res_model": "res.partner"}])
        mock_connection.search.return_value = [42]
        mock_connection.read.return_value = [{"id": 42, "name": "quote.pdf"}]
        mock_access_controller.validate_model_access.side_effect = None

        result = await handler._handle_record_retrieval("ir.attachment", "42")

        assert "quote.pdf" in result
        mock_access_controller.validate_model_access.assert_any_call("res.partner", "read")

    @pytest.mark.asyncio
    async def test_search_resource_scopes_attachments(self, handler, mock_connection):
        mock_connection.search.return_value = []
        mock_connection.search_count.return_value = 0

        await handler._handle_search("ir.attachment", None, None, None, None, None)

        assert mock_connection.search.call_args[0][1] == [
            "|",
            ("res_model", "=", False),
            ("res_model", "in", ["res.partner"]),
        ]

    @pytest.mark.asyncio
    async def test_count_resource_scopes_attachments(self, handler, mock_connection):
        mock_connection.search_count.return_value = 0

        await handler._handle_count("ir.attachment", None)

        assert mock_connection.search_count.call_args[0][1] == [
            "|",
            ("res_model", "=", False),
            ("res_model", "in", ["res.partner"]),
        ]

    @pytest.mark.asyncio
    async def test_other_models_are_untouched(self, handler, mock_connection):
        mock_connection.search.return_value = []
        mock_connection.search_count.return_value = 0

        await handler._handle_count("res.partner", None)

        assert mock_connection.search_count.call_args[0][1] == []
