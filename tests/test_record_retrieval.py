"""Tests for record retrieval functionality."""

import unittest
from unittest.mock import MagicMock

import pytest
from mcp import Resource

from mcp_server_odoo.data_formatting import format_field_value, format_record
from mcp_server_odoo.odoo_connection import OdooConnectionError
from mcp_server_odoo.resource_handlers import (
    MODEL_RECORD_PATTERN,
    RecordResourceHandler,
    ResourceHandlerError,
)


class TestRecordResourceHandler(unittest.TestCase):
    """Test cases for record retrieval resource handler."""

    def setUp(self):
        """Set up test environment."""
        # Create a mock OdooConnection
        self.odoo = MagicMock()

        # Initialize the handler
        self.handler = RecordResourceHandler(self.odoo)

        # Setup mock field data for various field types
        self.odoo.get_model_fields.return_value = {
            "name": {"type": "char", "string": "Name"},
            "description": {"type": "text", "string": "Description"},
            "age": {"type": "integer", "string": "Age"},
            "price": {"type": "float", "string": "Price"},
            "amount": {"type": "monetary", "string": "Amount"},
            "is_active": {"type": "boolean", "string": "Active"},
            "date_start": {"type": "date", "string": "Start Date"},
            "datetime_created": {"type": "datetime", "string": "Created On"},
            "state": {"type": "selection", "string": "State"},
            "partner_id": {
                "type": "many2one",
                "string": "Partner",
                "relation": "res.partner",
            },
            "tag_ids": {
                "type": "many2many",
                "string": "Tags",
                "relation": "res.partner.category",
            },
            "line_ids": {
                "type": "one2many",
                "string": "Lines",
                "relation": "product.line",
            },
            "image": {"type": "binary", "string": "Image"},
        }

    def test_can_handle(self):
        """Test can_handle method for valid and invalid URIs."""
        # Valid URI
        resource = Resource(name="test", uri="odoo://res.partner/record/42")
        self.assertTrue(self.handler.can_handle(resource))

        # Invalid URIs
        invalid_uris = [
            "odoo://res.partner/search",
            "odoo://res.partner/browse?ids=1,2,3",
            "odoo://res.partner/count",
            "odoo://res.partner/fields",
            "odoo://res.partner/record/abc",  # Non-numeric ID
            "odoo://res.partner/record/",  # Missing ID
            "file://some/file.txt",  # Different scheme
        ]

        for uri in invalid_uris:
            resource = Resource(name="test", uri=uri)
            self.assertFalse(self.handler.can_handle(resource))

    def test_handle_successful_retrieval(self):
        """Test successful record retrieval."""
        # Mock the Odoo connection to return a record
        mock_record = {
            "id": 42,
            "name": "Test Partner",
            "description": "A test partner",
            "age": 30,
            "price": 99.99,
            "amount": 1000.0,
            "is_active": True,
            "date_start": "2023-01-15",
            "datetime_created": "2023-01-01 10:00:00",
            "state": "active",
            "partner_id": [15, "Related Partner"],
            "tag_ids": [1, 2, 3],
            "line_ids": [4, 5, 6],
            "image": "base64encodedstring",
        }

        self.odoo.read.return_value = [mock_record]

        # Create the resource and handle it
        resource = Resource(name="test", uri="odoo://res.partner/record/42")
        result = self.handler.handle(resource)

        # Verify the Odoo connection was called correctly
        self.odoo.read.assert_called_once_with("res.partner", [42])

        # Verify the result structure
        self.assertFalse(result["is_error"])
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")

        # Check content for expected fields
        content = result["content"][0]["text"]
        self.assertIn("Resource: res.partner/record/42", content)
        self.assertIn("Name: Test Partner", content)
        self.assertIn("Age: 30", content)
        self.assertIn("Active: Yes", content)

        # Check relational fields
        self.assertIn("Partner: Related Partner", content)
        self.assertIn("Tags: 3 related records", content)
        self.assertIn("Lines: 3 related records", content)

    def test_handle_record_not_found(self):
        """Test error handling when record is not found."""
        # Mock the Odoo connection to return empty list (no record found)
        self.odoo.read.return_value = []

        # Create the resource and try to handle it
        resource = Resource(name="test", uri="odoo://res.partner/record/999")

        # Should raise ResourceHandlerError
        with self.assertRaises(ResourceHandlerError) as context:
            self.handler.handle(resource)

        # Verify the error message is descriptive
        self.assertIn("Record not found", str(context.exception))
        self.assertIn("999", str(context.exception))

    def test_handle_connection_error(self):
        """Test error handling when connection fails."""
        # Mock the Odoo connection to raise an error
        self.odoo.read.side_effect = OdooConnectionError("Connection failed")

        # Create the resource and try to handle it
        resource = Resource(name="test", uri="odoo://res.partner/record/42")

        # Should raise ResourceHandlerError
        with self.assertRaises(ResourceHandlerError) as context:
            self.handler.handle(resource)

        # Verify the error message is descriptive
        self.assertIn("Connection failed", str(context.exception))

    def test_handle_invalid_uri(self):
        """Test error handling for invalid URI."""
        # Create an invalid resource
        resource = Resource(name="test", uri="odoo://res.partner/record/abc")

        # Should raise ResourceHandlerError
        with self.assertRaises(ResourceHandlerError) as context:
            self.handler.handle(resource)

        # Verify the error message is descriptive
        self.assertIn("Invalid record URI", str(context.exception))


class TestRecordResourceIntegration(unittest.TestCase):
    """Integration tests for record retrieval with URI parsing and formatting."""

    def setUp(self):
        """Set up test environment."""
        # Create a mock OdooConnection
        self.odoo = MagicMock()

        # Initialize the handler
        self.handler = RecordResourceHandler(self.odoo)

        # Setup field info
        self.odoo.get_model_fields.return_value = {
            "name": {"type": "char", "string": "Name"},
            "partner_id": {
                "type": "many2one",
                "string": "Partner",
                "relation": "res.partner",
            },
            "line_ids": {
                "type": "one2many",
                "string": "Lines",
                "relation": "product.line",
            },
        }

    def test_uri_pattern(self):
        """Test that the URI pattern matches expected URIs."""
        valid_uris = [
            "odoo://res.partner/record/1",
            "odoo://product.template/record/42",
            "odoo://sale.order/record/999",
        ]

        invalid_uris = [
            "odoo://res.partner/records/1",  # Incorrect operation
            "odoo://res.partner/record/abc",  # Non-numeric ID
            "odoo://res.partner/record/",  # Missing ID
            "file://res.partner/record/1",  # Wrong scheme
        ]

        for uri in valid_uris:
            self.assertIsNotNone(re.match(MODEL_RECORD_PATTERN, uri))

        for uri in invalid_uris:
            self.assertIsNone(re.match(MODEL_RECORD_PATTERN, uri))

    def test_field_formatting_integration(self):
        """Test that field formatting is correctly integrated."""
        # Mock a record
        mock_record = {
            "id": 1,
            "name": "Test Record",
            "partner_id": [2, "Test Partner"],
            "line_ids": [3, 4, 5],
        }

        self.odoo.read.return_value = [mock_record]

        # Create and handle the resource
        resource = Resource(name="test", uri="odoo://test.model/record/1")
        result = self.handler.handle(resource)

        # Verify the formatted output
        content = result["content"][0]["text"]

        # Check for proper formatting of each field type
        self.assertIn("Name: Test Record", content)
        self.assertIn("Partner: Test Partner", content)
        self.assertIn("Lines: 3 related records", content)


# Import this here to avoid circular imports in main code
import re


@pytest.mark.parametrize(
    "field_name,field_value,field_type,expected",
    [
        ("Name", "Test", "char", "Name: Test"),
        ("Description", "Long text...", "text", "Description: Long text..."),
        ("Age", 25, "integer", "Age: 25"),
        ("Price", 99.99, "float", "Price: 99.99"),
        ("Amount", 1000.0, "monetary", "Amount: 1000.0"),
        ("Active", True, "boolean", "Active: Yes"),
        ("Active", False, "boolean", "Active: No"),
        ("Date", "2023-04-01", "date", "Date: 2023-04-01"),
        (
            "Datetime",
            "2023-04-01 14:30:00",
            "datetime",
            "Datetime: 2023-04-01 14:30:00",
        ),
        ("State", "confirmed", "selection", "State: confirmed"),
        ("Image", "base64data", "binary", "Image: [Binary data]"),
    ],
)
def test_format_field_value(field_name, field_value, field_type, expected):
    """Test formatting of various field types."""
    mock_odoo = MagicMock()
    result = format_field_value(
        field_name=field_name,
        field_value=field_value,
        field_type=field_type,
        model="test.model",
        odoo=mock_odoo,
    )
    assert result == expected


def test_format_many2one_field():
    """Test formatting of many2one fields separately."""
    mock_odoo = MagicMock()
    # Mock the get_model_fields method to return a relation
    mock_odoo.get_model_fields.return_value = {"partner": {"relation": "res.partner"}}

    result = format_field_value(
        field_name="Partner",
        field_value=[42, "Partner Name"],
        field_type="many2one",
        model="test.model",
        odoo=mock_odoo,
    )

    assert "Partner: Partner Name" in result
    assert "[odoo://res.partner/record/42]" in result


@pytest.mark.parametrize(
    "field_name,field_value,field_type,relation,expected_contains",
    [
        (
            "Tags",
            [1, 2, 3],
            "many2many",
            "res.partner.category",
            "Tags: 3 related records [odoo://res.partner.category/browse?ids=1,2,3]",
        ),
        (
            "Lines",
            [4, 5, 6],
            "one2many",
            "product.line",
            "Lines: 3 related records [odoo://product.line/browse?ids=4,5,6]",
        ),
    ],
)
def test_format_relational_fields(
    field_name, field_value, field_type, relation, expected_contains
):
    """Test formatting of relational fields (*2many)."""
    mock_odoo = MagicMock()

    # Mock the get_model_fields method to return the relation info
    mock_odoo.get_model_fields.return_value = {
        field_name.lower(): {"relation": relation}
    }

    result = format_field_value(
        field_name=field_name,
        field_value=field_value,
        field_type=field_type,
        model="test.model",
        odoo=mock_odoo,
    )

    assert expected_contains in result


def test_format_null_value():
    """Test formatting of null (None) values."""
    mock_odoo = MagicMock()

    result = format_field_value(
        field_name="Field",
        field_value=None,
        field_type="char",
        model="test.model",
        odoo=mock_odoo,
    )

    assert result == "Field: Not set"


def test_format_record_excludes_technical_fields():
    """Test that technical fields are excluded from formatted record."""
    mock_odoo = MagicMock()
    mock_odoo.get_model_fields.return_value = {
        "name": {"type": "char", "string": "Name"},
        "__last_update": {"type": "datetime", "string": "Last Update"},
        "create_uid": {"type": "many2one", "string": "Created by"},
        "create_date": {"type": "datetime", "string": "Created on"},
        "write_uid": {"type": "many2one", "string": "Last Updated by"},
        "write_date": {"type": "datetime", "string": "Last Updated on"},
    }

    record = {
        "id": 1,
        "name": "Test Record",
        "__last_update": "2023-04-01 14:30:00",
        "create_uid": [1, "Admin"],
        "create_date": "2023-04-01 10:00:00",
        "write_uid": [1, "Admin"],
        "write_date": "2023-04-01 14:30:00",
    }

    result = format_record(
        model="test.model",
        record=record,
        odoo=mock_odoo,
    )

    # Should only include name, not the technical fields
    assert "Name: Test Record" in result
    assert "__last_update" not in result
    assert "Created by" not in result
    assert "Created on" not in result
    assert "Last Updated by" not in result
    assert "Last Updated on" not in result
