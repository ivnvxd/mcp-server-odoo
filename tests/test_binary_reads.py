"""read_without_binary_payloads: a read of only "id" must not invent records."""

from unittest.mock import MagicMock

import pytest

from mcp_server_odoo.binary_reads import read_without_binary_payloads
from mcp_server_odoo.odoo_connection import OdooConnection


@pytest.mark.parametrize("major", [16, 19, 20])
def test_an_id_only_read_drops_missing_records(major):
    """Odoo echoes a missing id back for a read of only "id"."""
    connection = MagicMock(spec=OdooConnection)
    connection.get_major_version.return_value = major
    connection.fields_get.return_value = {"id": {"type": "integer"}}
    connection.read.return_value = [{"id": 3}, {"id": 999}]
    connection.search.return_value = [3]

    records = read_without_binary_payloads(connection, "res.partner", [3, 999], ["id"])

    assert records == [{"id": 3}]
    connection.search.assert_called_once_with(
        "res.partner", [["id", "in", [3, 999]]], context={"active_test": False}
    )
