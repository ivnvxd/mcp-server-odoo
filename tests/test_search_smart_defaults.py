"""Test smart field selection for search_records."""

from unittest.mock import MagicMock, Mock, patch

import pytest
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection, OdooValidationFault
from mcp_server_odoo.tools import UNREADABLE_FIELDS_TTL, OdooToolHandler


class TestSearchSmartDefaults:
    """Test smart field selection for search_records when fields not specified."""

    @pytest.fixture
    def tool_handler(self):
        """Create a tool handler with mocked dependencies."""
        app = Mock()
        connection = Mock()
        access_controller = Mock()
        config = Mock()
        config.allowed_companies = None
        config.default_limit = 10
        config.max_limit = 100
        config.max_smart_fields = 15

        return OdooToolHandler(app, connection, access_controller, config)

    @pytest.mark.asyncio
    async def test_search_with_no_fields_uses_smart_defaults(self, tool_handler):
        """Test that search_records uses smart defaults when fields is None."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 2
        tool_handler.connection.search.return_value = [1, 2]

        # Mock fields_get to return field metadata
        tool_handler.connection.fields_get.return_value = {
            "id": {"type": "integer", "required": True},
            "name": {"type": "char", "required": True, "searchable": True},
            "email": {"type": "char", "searchable": True},
            "phone": {"type": "char", "searchable": True},
            "create_date": {"type": "datetime"},
            "message_ids": {"type": "one2many"},  # Should be excluded
            "_barcode_scan": {"type": "char"},  # Should be excluded (technical)
            "image_1920": {"type": "binary"},  # Should be excluded (binary)
        }

        # Mock read to return records with only smart default fields
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test 1",
                "email": "test1@example.com",
                "create_date": "20250607T10:00:00",
            },
            {
                "id": 2,
                "name": "Test 2",
                "email": "test2@example.com",
                "create_date": "20250607T11:00:00",
            },
        ]

        # Call the handler with fields=None
        handler = tool_handler._handle_search_tool
        await handler("res.partner", [], None, 10, 0, None)

        # Verify smart defaults were used
        # The read call should have been made with specific fields, not None
        tool_handler.connection.read.assert_called_once()
        call_args = tool_handler.connection.read.call_args
        fields_arg = call_args[0][2]  # Third positional argument

        # Should have selected smart default fields
        assert fields_arg is not None
        assert isinstance(fields_arg, list)
        assert "id" in fields_arg
        assert "name" in fields_arg
        assert "email" in fields_arg

        # Should exclude technical/binary/relation fields
        assert "message_ids" not in fields_arg
        assert "_barcode_scan" not in fields_arg
        assert "image_1920" not in fields_arg

    @pytest.mark.asyncio
    async def test_search_with_specific_fields(self, tool_handler):
        """Test that search_records uses specified fields when provided."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]
        tool_handler.connection.read.return_value = [
            {"id": 1, "name": "Test", "phone": "+1234567890"}
        ]

        # Call with specific fields
        handler = tool_handler._handle_search_tool
        fields = ["name", "phone"]
        await handler("res.partner", [], fields, 10, 0, None)

        # Verify specified fields were used
        tool_handler.connection.read.assert_called_once_with(
            "res.partner", [1], fields, {"bin_size": True}
        )

    @pytest.mark.asyncio
    async def test_search_with_all_fields(self, tool_handler):
        """Test that search_records can fetch all fields when explicitly requested."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]
        tool_handler.connection.read.return_value = [
            {
                "id": 1,
                "name": "Test",
                "email": "test@example.com",
                "phone": "+1234567890",
                "create_date": "20250607T10:00:00",
                "message_ids": [1, 2, 3],
                "_barcode_scan": "12345",
                "image_1920": "base64data...",
                # ... many more fields
            }
        ]

        # Call with __all__ to get all fields
        handler = tool_handler._handle_search_tool
        await handler("res.partner", [], ["__all__"], 10, 0, None)

        # Verify None was passed to read (which means all fields)
        tool_handler.connection.read.assert_called_once_with(
            "res.partner", [1], None, {"bin_size": True}
        )

    @pytest.mark.asyncio
    async def test_search_falls_back_when_fields_get_fails(self, tool_handler):
        """Smart defaults should fall back to all fields when fields_get fails."""
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]
        tool_handler.connection.fields_get.side_effect = Exception("Cannot get fields")
        tool_handler.connection.read.return_value = [{"id": 1, "name": "Test"}]

        await tool_handler._handle_search_tool("res.partner", [], None, 10, 0, None)

        # Should fall back to no field filtering
        fields_arg = tool_handler.connection.read.call_args[0][2]
        assert fields_arg is None

    @pytest.mark.asyncio
    async def test_search_smart_defaults_with_datetime_formatting(self, tool_handler):
        """Test that datetime fields are formatted even with smart defaults."""
        # Setup mocks
        tool_handler.connection.is_authenticated = True
        tool_handler.connection.search_count.return_value = 1
        tool_handler.connection.search.return_value = [1]

        # Mock fields_get — use date_order (a business datetime field that smart
        # selection includes) instead of create_date (which is in the exclude list
        # and would get score 0, making the test internally inconsistent).
        tool_handler.connection.fields_get.return_value = {
            "id": {"type": "integer", "required": True},
            "name": {"type": "char", "required": True},
            "date_order": {"type": "datetime", "store": True},
        }

        # Mock read with datetime that needs formatting
        tool_handler.connection.read.return_value = [
            {"id": 1, "name": "Test", "date_order": "20250607T10:00:00"}
        ]

        # Call the handler with fields=None to trigger smart selection
        handler = tool_handler._handle_search_tool
        result = await handler("res.partner", [], None, 10, 0, None)

        # Verify date_order was included by smart selection
        call_args = tool_handler.connection.read.call_args
        fields_arg = call_args[0][2]
        assert "date_order" in fields_arg

        # Verify datetime was formatted
        assert result["records"][0]["date_order"] == "2025-06-07T10:00:00+00:00"


class TestUnreadableFieldsInBulkReads:
    """A computed field the user cannot read is left out of a default or
    ["__all__"] read instead of failing it. An explicit field list keeps the error."""

    FIELDS = {
        "id": {"type": "integer"},
        "name": {"type": "char", "required": True},
        "email": {"type": "char"},
        "total_due": {"type": "monetary", "store": False},
        "credit": {"type": "monetary", "store": False},
        "image_128": {"type": "binary"},
    }
    REFUSED = {"total_due", "credit"}

    @pytest.fixture
    def connection(self):
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        connection.get_major_version.return_value = 19
        connection.fields_get.return_value = self.FIELDS
        connection.search.return_value = [7]
        connection.search_count.return_value = 1

        def read(model, ids, fields=None, context=None):
            names = list(self.FIELDS) if fields is None else fields
            if self.REFUSED & set(names):
                raise OdooValidationFault(
                    "You are not allowed to access 'Journal Item' records.", 4
                )
            return [{"id": rid, **{name: f"{name}-{rid}" for name in names}} for rid in ids]

        connection.read.side_effect = read
        return connection

    @pytest.fixture
    def handler(self, connection):
        config = OdooConfig(url="http://localhost:8069", api_key="k", database="d")
        return OdooToolHandler(
            MagicMock(spec=MCPServer), connection, MagicMock(spec=AccessController), config
        )

    def _read_field_lists(self, connection):
        return [c.args[2] for c in connection.read.call_args_list]

    async def test_get_record_smart_defaults_leave_out_refused_fields(self, handler, connection):
        result = await handler._handle_get_record_tool("res.partner", 7, None)

        assert result.skipped_fields == ["total_due", "credit"]
        assert "total_due" not in result.record and "credit" not in result.record
        assert result.record["name"] == "name-7"
        assert "cannot read: total_due, credit" in result.metadata.note
        # the probe never reads a binary: on Odoo 20 that returns its content
        assert not any("image_128" in names for names in self._read_field_lists(connection)[1:-1])

    async def test_search_all_fields_leaves_out_refused_fields(self, handler, connection):
        result = await handler._handle_search_tool("res.partner", None, ["__all__"], 10, 0, None)

        assert result["skipped_fields"] == ["total_due", "credit"]
        assert set(result["records"][0]) == {"id", "name", "email", "image_128"}
        assert "cannot read" in result["note"]

    async def test_the_refused_fields_are_cached(self, handler, connection):
        await handler._handle_search_tool("res.partner", None, ["__all__"], 10, 0, None)
        connection.read.reset_mock()

        result = await handler._handle_search_tool("res.partner", None, ["__all__"], 10, 0, None)

        # one read, without the refused fields, and no probe
        assert self._read_field_lists(connection) == [["id", "name", "email", "image_128"]]
        assert result["skipped_fields"] == ["total_due", "credit"]

    async def test_the_cache_expires(self, handler, connection):
        with patch("mcp_server_odoo.tools.time.monotonic", return_value=1000.0):
            await handler._handle_search_tool("res.partner", None, ["__all__"], 10, 0, None)
        connection.read.reset_mock()

        later = 1000.0 + UNREADABLE_FIELDS_TTL + 1
        with patch("mcp_server_odoo.tools.time.monotonic", return_value=later):
            await handler._handle_search_tool("res.partner", None, ["__all__"], 10, 0, None)

        # the first read tries every field again
        assert self._read_field_lists(connection)[0] is None

    @pytest.mark.parametrize("call", ["get_record", "search"])
    async def test_an_explicit_field_list_keeps_the_error(self, handler, connection, call):
        with pytest.raises(ValidationError, match="not allowed to access"):
            if call == "get_record":
                await handler._handle_get_record_tool("res.partner", 7, ["name", "total_due"])
            else:
                await handler._handle_search_tool(
                    "res.partner", None, ["name", "total_due"], 10, 0, None
                )
        assert connection.read.call_count == 1

    async def test_a_read_where_every_field_fails_keeps_the_error(self, handler, connection):
        """A record the user cannot read refuses every field: nothing to leave out."""
        connection.read.side_effect = OdooValidationFault(
            "You are not allowed to access 'Contact' records.", 4
        )

        with pytest.raises(ValidationError, match="'Contact' records"):
            await handler._handle_get_record_tool("res.partner", 7, None)

    async def test_another_fault_is_not_probed(self, handler, connection):
        connection.read.side_effect = OdooValidationFault("Invalid field 'x'", 2)

        with pytest.raises(ValidationError, match="Invalid field"):
            await handler._handle_get_record_tool("res.partner", 7, None)
        assert connection.read.call_count == 1

    def test_the_probe_halves_the_field_list(self, handler, connection):
        names = [f"x_{i:02d}" for i in range(64)] + ["total_due"]

        def read(model, ids, fields=None, context=None):
            if "total_due" in fields:
                raise OdooValidationFault("refused", 4)
            return [{"id": 7}]

        connection.read.side_effect = read

        assert handler._find_unreadable_fields("res.partner", 7, names, {"bin_size": True}) == [
            "total_due"
        ]
        # one refused field among 65: 2 reads per halving plus the first, not 65
        assert connection.read.call_count == 2 * 7 + 1
