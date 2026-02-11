"""Comprehensive unit tests for workflow tools handlers.

Tests all 10 workflow tool handlers with mocked Odoo connection,
access controller, and configuration. Covers success paths, not-found
errors, access denied errors, and validation errors for each handler.
"""

from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_odoo.access_control import AccessControlError, AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import NotFoundError, ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection, OdooConnectionError
from mcp_server_odoo.workflow_tools import OdooWorkflowHandler, register_workflow_tools


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class TestWorkflowHandler:
    """Test cases for OdooWorkflowHandler class."""

    @pytest.fixture
    def mock_app(self):
        """Create a mock FastMCP app with tool registration tracking."""
        app = MagicMock(spec=FastMCP)
        app._tools = {}

        def tool_decorator():
            def decorator(func):
                app._tools[func.__name__] = func
                return func
            return decorator

        app.tool = tool_decorator
        return app

    @pytest.fixture
    def mock_connection(self):
        """Create a mock OdooConnection (is_authenticated is a @property)."""
        connection = MagicMock(spec=OdooConnection)
        connection.is_authenticated = True
        return connection

    @pytest.fixture
    def mock_access_controller(self):
        """Create a mock AccessController that allows everything by default."""
        controller = MagicMock(spec=AccessController)
        return controller

    @pytest.fixture
    def valid_config(self):
        """Create a valid OdooConfig for testing."""
        return OdooConfig(
            url="http://localhost:8069",
            api_key="test_api_key",
            database="test_db",
            default_limit=10,
            max_limit=100,
        )

    @pytest.fixture
    def handler(self, mock_app, mock_connection, mock_access_controller, valid_config):
        """Create an OdooWorkflowHandler wired to mock dependencies."""
        return OdooWorkflowHandler(
            mock_app, mock_connection, mock_access_controller, valid_config
        )

    # -----------------------------------------------------------------------
    # Handler initialization and tool registration
    # -----------------------------------------------------------------------

    def test_handler_initialization(self, handler, mock_app):
        """Test handler is properly initialized with all dependencies."""
        assert handler.app == mock_app
        assert handler.connection is not None
        assert handler.access_controller is not None
        assert handler.config is not None

    def test_all_workflow_tools_registered(self, handler, mock_app):
        """Test that all 10 workflow tools are registered."""
        expected_tools = [
            "create_quotation",
            "confirm_quotation",
            "create_manufacturing_order",
            "confirm_manufacturing_order",
            "create_purchase_order",
            "confirm_purchase_order",
            "receive_inventory",
            "deliver_to_customer",
            "create_bom",
            "get_workflow_status",
        ]
        for tool_name in expected_tools:
            assert tool_name in mock_app._tools, f"Tool '{tool_name}' not registered"

    # ===================================================================
    # 1. create_quotation
    # ===================================================================

    async def test_create_quotation_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful creation of a quotation."""
        mock_access_controller.validate_model_access.return_value = None

        # Customer lookup
        mock_connection.read.side_effect = [
            # 1st call: customer validation
            [{"id": 10, "name": "Acme Corp"}],
            # 2nd call: read-back of created quotation
            [
                {
                    "id": 1,
                    "name": "S00042",
                    "state": "draft",
                    "amount_total": 700.0,
                    "partner_id": [10, "Acme Corp"],
                }
            ],
        ]
        mock_connection.create.return_value = 1

        create_quotation = mock_app._tools["create_quotation"]
        result = await create_quotation(
            customer_id=10,
            product_lines=[
                {"product_id": 100, "quantity": 2.0, "price_unit": 350.0},
            ],
        )

        assert result["success"] is True
        assert result["quotation_id"] == 1
        assert result["quotation_name"] == "S00042"
        assert result["customer"] == "Acme Corp"
        assert result["total"] == 700.0
        assert result["state"] == "draft"
        assert "url" in result
        assert "sale.order" in result["url"]
        assert result["message"].startswith("Successfully created quotation")
        mock_access_controller.validate_model_access.assert_called_with(
            "sale.order", "create"
        )

    async def test_create_quotation_with_order_date(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test quotation creation with explicit order_date."""
        mock_connection.read.side_effect = [
            [{"id": 10, "name": "Acme Corp"}],
            [
                {
                    "id": 2,
                    "name": "S00043",
                    "state": "draft",
                    "amount_total": 100.0,
                    "partner_id": [10, "Acme Corp"],
                }
            ],
        ]
        mock_connection.create.return_value = 2

        create_quotation = mock_app._tools["create_quotation"]
        result = await create_quotation(
            customer_id=10,
            product_lines=[{"product_id": 200, "quantity": 1.0}],
            order_date="2026-03-15",
        )

        assert result["success"] is True
        # Verify date_order was passed to create
        create_call_args = mock_connection.create.call_args
        quotation_data = create_call_args[0][1]
        assert quotation_data["date_order"] == "2026-03-15"

    async def test_create_quotation_without_price_unit(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test quotation creation when price_unit is omitted from a line."""
        mock_connection.read.side_effect = [
            [{"id": 10, "name": "Acme Corp"}],
            [
                {
                    "id": 3,
                    "name": "S00044",
                    "state": "draft",
                    "amount_total": 0.0,
                    "partner_id": [10, "Acme Corp"],
                }
            ],
        ]
        mock_connection.create.return_value = 3

        create_quotation = mock_app._tools["create_quotation"]
        result = await create_quotation(
            customer_id=10,
            product_lines=[{"product_id": 200, "quantity": 5.0}],
        )

        assert result["success"] is True
        # Verify price_unit is NOT in the created line data
        create_call_args = mock_connection.create.call_args
        quotation_data = create_call_args[0][1]
        line_tuple = quotation_data["order_line"][0]
        line_data = line_tuple[2]
        assert "price_unit" not in line_data

    async def test_create_quotation_customer_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test quotation creation when customer does not exist."""
        mock_connection.read.return_value = []

        create_quotation = mock_app._tools["create_quotation"]
        with pytest.raises(NotFoundError, match="Customer with ID 999 not found"):
            await create_quotation(
                customer_id=999,
                product_lines=[{"product_id": 100, "quantity": 1.0}],
            )

    async def test_create_quotation_missing_product_id(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test quotation creation with missing product_id in line."""
        mock_connection.read.return_value = [{"id": 10, "name": "Acme Corp"}]

        create_quotation = mock_app._tools["create_quotation"]
        with pytest.raises(
            ValidationError, match="Each product line must have 'product_id' and 'quantity'"
        ):
            await create_quotation(
                customer_id=10,
                product_lines=[{"quantity": 2.0}],
            )

    async def test_create_quotation_missing_quantity(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test quotation creation with missing quantity in line."""
        mock_connection.read.return_value = [{"id": 10, "name": "Acme Corp"}]

        create_quotation = mock_app._tools["create_quotation"]
        with pytest.raises(
            ValidationError, match="Each product line must have 'product_id' and 'quantity'"
        ):
            await create_quotation(
                customer_id=10,
                product_lines=[{"product_id": 100}],
            )

    async def test_create_quotation_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test quotation creation with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied to sale.order"
        )

        create_quotation = mock_app._tools["create_quotation"]
        with pytest.raises(ValidationError, match="Access denied"):
            await create_quotation(
                customer_id=10,
                product_lines=[{"product_id": 100, "quantity": 1.0}],
            )

    async def test_create_quotation_connection_error(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test quotation creation with connection error."""
        mock_connection.read.side_effect = OdooConnectionError("Connection lost")

        create_quotation = mock_app._tools["create_quotation"]
        with pytest.raises(ValidationError, match="Connection error"):
            await create_quotation(
                customer_id=10,
                product_lines=[{"product_id": 100, "quantity": 1.0}],
            )

    # ===================================================================
    # 2. confirm_quotation
    # ===================================================================

    async def test_confirm_quotation_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful confirmation of a draft quotation."""
        mock_connection.read.side_effect = [
            # 1st call: verify quotation exists and is draft
            [{"id": 1, "name": "S00042", "state": "draft", "amount_total": 700.0}],
            # 2nd call: read back updated order
            [{"id": 1, "name": "S00042", "state": "sale", "amount_total": 700.0}],
        ]
        mock_connection.execute.return_value = True

        confirm_quotation = mock_app._tools["confirm_quotation"]
        result = await confirm_quotation(quotation_id=1)

        assert result["success"] is True
        assert result["order_id"] == 1
        assert result["order_name"] == "S00042"
        assert result["state"] == "sale"
        assert result["total"] == 700.0
        assert "url" in result
        mock_connection.execute.assert_called_once_with(
            "sale.order", "action_confirm", [1]
        )

    async def test_confirm_quotation_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test confirming a quotation that does not exist."""
        mock_connection.read.return_value = []

        confirm_quotation = mock_app._tools["confirm_quotation"]
        with pytest.raises(NotFoundError, match="Quotation with ID 999 not found"):
            await confirm_quotation(quotation_id=999)

    async def test_confirm_quotation_wrong_state(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test confirming a quotation that is not in draft state."""
        mock_connection.read.return_value = [
            {"id": 1, "name": "S00042", "state": "sale", "amount_total": 700.0}
        ]

        confirm_quotation = mock_app._tools["confirm_quotation"]
        with pytest.raises(ValidationError, match="cannot confirm.*must be 'draft'"):
            await confirm_quotation(quotation_id=1)

    async def test_confirm_quotation_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test confirming a quotation with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        confirm_quotation = mock_app._tools["confirm_quotation"]
        with pytest.raises(ValidationError, match="Access denied"):
            await confirm_quotation(quotation_id=1)

    # ===================================================================
    # 3. create_manufacturing_order
    # ===================================================================

    async def test_create_manufacturing_order_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful creation of a manufacturing order."""
        mock_connection.read.side_effect = [
            # product validation
            [{"id": 50, "name": "Wooden Table"}],
            # read-back of created MO
            [
                {
                    "id": 1,
                    "name": "MO/00001",
                    "state": "draft",
                    "product_qty": 10.0,
                    "product_id": [50, "Wooden Table"],
                }
            ],
        ]
        mock_connection.create.return_value = 1

        create_mo = mock_app._tools["create_manufacturing_order"]
        result = await create_mo(product_id=50, quantity=10.0)

        assert result["success"] is True
        assert result["mo_id"] == 1
        assert result["mo_name"] == "MO/00001"
        assert result["product"] == "Wooden Table"
        assert result["quantity"] == 10.0
        assert result["state"] == "draft"
        assert "mrp.production" in result["url"]

    async def test_create_manufacturing_order_with_origin(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test MO creation with a source document origin."""
        mock_connection.read.side_effect = [
            [{"id": 50, "name": "Wooden Table"}],
            [
                {
                    "id": 2,
                    "name": "MO/00002",
                    "state": "draft",
                    "product_qty": 5.0,
                    "product_id": [50, "Wooden Table"],
                }
            ],
        ]
        mock_connection.create.return_value = 2

        create_mo = mock_app._tools["create_manufacturing_order"]
        result = await create_mo(product_id=50, quantity=5.0, origin="S00042")

        assert result["success"] is True
        create_call_args = mock_connection.create.call_args
        mo_data = create_call_args[0][1]
        assert mo_data["origin"] == "S00042"

    async def test_create_manufacturing_order_product_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test MO creation when product does not exist."""
        mock_connection.read.return_value = []

        create_mo = mock_app._tools["create_manufacturing_order"]
        with pytest.raises(NotFoundError, match="Product with ID 999 not found"):
            await create_mo(product_id=999, quantity=1.0)

    async def test_create_manufacturing_order_mrp_not_installed(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test MO creation when MRP module is not installed."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Model not found"
        )

        create_mo = mock_app._tools["create_manufacturing_order"]
        with pytest.raises(ValidationError, match="MRP.*module not installed"):
            await create_mo(product_id=50, quantity=10.0)

    # ===================================================================
    # 4. confirm_manufacturing_order
    # ===================================================================

    async def test_confirm_manufacturing_order_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful confirmation of a manufacturing order."""
        mock_connection.read.side_effect = [
            # verify MO exists
            [{"id": 1, "name": "MO/00001", "state": "draft"}],
            # read back updated MO
            [{"id": 1, "name": "MO/00001", "state": "confirmed", "product_qty": 10.0}],
        ]
        mock_connection.execute.return_value = True

        confirm_mo = mock_app._tools["confirm_manufacturing_order"]
        result = await confirm_mo(mo_id=1)

        assert result["success"] is True
        assert result["mo_id"] == 1
        assert result["mo_name"] == "MO/00001"
        assert result["state"] == "confirmed"
        assert result["quantity"] == 10.0
        assert "mrp.production" in result["url"]

        # action_confirm and action_assign should both be called
        calls = mock_connection.execute.call_args_list
        assert calls[0][0] == ("mrp.production", "action_confirm", [1])
        assert calls[1][0] == ("mrp.production", "action_assign", [1])

    async def test_confirm_manufacturing_order_assign_fails_gracefully(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that action_assign failure is handled gracefully (logged, not raised)."""
        mock_connection.read.side_effect = [
            [{"id": 1, "name": "MO/00001", "state": "draft"}],
            [{"id": 1, "name": "MO/00001", "state": "confirmed", "product_qty": 10.0}],
        ]

        # action_confirm succeeds, action_assign fails
        def execute_side_effect(model, method, ids):
            if method == "action_assign":
                raise Exception("No materials available")
            return True

        mock_connection.execute.side_effect = execute_side_effect

        confirm_mo = mock_app._tools["confirm_manufacturing_order"]
        result = await confirm_mo(mo_id=1)

        # Should still succeed even though action_assign failed
        assert result["success"] is True

    async def test_confirm_manufacturing_order_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test confirming a MO that does not exist."""
        mock_connection.read.return_value = []

        confirm_mo = mock_app._tools["confirm_manufacturing_order"]
        with pytest.raises(NotFoundError, match="Manufacturing order with ID 999 not found"):
            await confirm_mo(mo_id=999)

    async def test_confirm_manufacturing_order_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test confirming a MO with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        confirm_mo = mock_app._tools["confirm_manufacturing_order"]
        with pytest.raises(ValidationError, match="Access denied"):
            await confirm_mo(mo_id=1)

    # ===================================================================
    # 5. create_purchase_order
    # ===================================================================

    async def test_create_purchase_order_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful creation of a purchase order."""
        mock_connection.read.side_effect = [
            # vendor validation
            [{"id": 20, "name": "Wood Supplier Inc."}],
            # read-back of created PO
            [
                {
                    "id": 1,
                    "name": "P00016",
                    "state": "draft",
                    "amount_total": 275.0,
                    "partner_id": [20, "Wood Supplier Inc."],
                }
            ],
        ]
        mock_connection.create.return_value = 1

        create_po = mock_app._tools["create_purchase_order"]
        result = await create_po(
            vendor_id=20,
            product_lines=[
                {"product_id": 100, "quantity": 10.0, "price_unit": 15.0},
                {"product_id": 101, "quantity": 5.0, "price_unit": 25.0},
            ],
        )

        assert result["success"] is True
        assert result["po_id"] == 1
        assert result["po_name"] == "P00016"
        assert result["vendor"] == "Wood Supplier Inc."
        assert result["total"] == 275.0
        assert result["state"] == "draft"
        assert "purchase.order" in result["url"]

    async def test_create_purchase_order_vendor_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test PO creation when vendor does not exist."""
        mock_connection.read.return_value = []

        create_po = mock_app._tools["create_purchase_order"]
        with pytest.raises(NotFoundError, match="Vendor with ID 999 not found"):
            await create_po(
                vendor_id=999,
                product_lines=[
                    {"product_id": 100, "quantity": 1.0, "price_unit": 10.0}
                ],
            )

    async def test_create_purchase_order_missing_price_unit(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test PO creation with missing price_unit (required for PO lines)."""
        mock_connection.read.return_value = [
            {"id": 20, "name": "Wood Supplier Inc."}
        ]

        create_po = mock_app._tools["create_purchase_order"]
        with pytest.raises(
            ValidationError,
            match="Each product line must have 'product_id', 'quantity', and 'price_unit'",
        ):
            await create_po(
                vendor_id=20,
                product_lines=[{"product_id": 100, "quantity": 10.0}],
            )

    async def test_create_purchase_order_missing_product_id(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test PO creation with missing product_id."""
        mock_connection.read.return_value = [
            {"id": 20, "name": "Wood Supplier Inc."}
        ]

        create_po = mock_app._tools["create_purchase_order"]
        with pytest.raises(
            ValidationError,
            match="Each product line must have 'product_id', 'quantity', and 'price_unit'",
        ):
            await create_po(
                vendor_id=20,
                product_lines=[{"quantity": 10.0, "price_unit": 15.0}],
            )

    async def test_create_purchase_order_missing_quantity(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test PO creation with missing quantity."""
        mock_connection.read.return_value = [
            {"id": 20, "name": "Wood Supplier Inc."}
        ]

        create_po = mock_app._tools["create_purchase_order"]
        with pytest.raises(
            ValidationError,
            match="Each product line must have 'product_id', 'quantity', and 'price_unit'",
        ):
            await create_po(
                vendor_id=20,
                product_lines=[{"product_id": 100, "price_unit": 15.0}],
            )

    async def test_create_purchase_order_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test PO creation with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        create_po = mock_app._tools["create_purchase_order"]
        with pytest.raises(ValidationError, match="Access denied"):
            await create_po(
                vendor_id=20,
                product_lines=[
                    {"product_id": 100, "quantity": 1.0, "price_unit": 10.0}
                ],
            )

    # ===================================================================
    # 6. confirm_purchase_order
    # ===================================================================

    async def test_confirm_purchase_order_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful confirmation of a purchase order."""
        mock_connection.read.side_effect = [
            # verify PO exists
            [{"id": 1, "name": "P00016", "state": "draft"}],
            # read back updated PO
            [{"id": 1, "name": "P00016", "state": "purchase", "amount_total": 275.0}],
        ]
        mock_connection.execute.return_value = True

        confirm_po = mock_app._tools["confirm_purchase_order"]
        result = await confirm_po(po_id=1)

        assert result["success"] is True
        assert result["po_id"] == 1
        assert result["po_name"] == "P00016"
        assert result["state"] == "purchase"
        assert result["total"] == 275.0
        assert "purchase.order" in result["url"]
        mock_connection.execute.assert_called_once_with(
            "purchase.order", "button_confirm", [1]
        )

    async def test_confirm_purchase_order_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test confirming a PO that does not exist."""
        mock_connection.read.return_value = []

        confirm_po = mock_app._tools["confirm_purchase_order"]
        with pytest.raises(NotFoundError, match="Purchase order with ID 999 not found"):
            await confirm_po(po_id=999)

    async def test_confirm_purchase_order_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test confirming a PO with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        confirm_po = mock_app._tools["confirm_purchase_order"]
        with pytest.raises(ValidationError, match="Access denied"):
            await confirm_po(po_id=1)

    # ===================================================================
    # 7. receive_inventory
    # ===================================================================

    async def test_receive_inventory_by_picking_id_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful inventory receipt using picking_id."""
        mock_connection.read.side_effect = [
            # verify picking exists
            [{"id": 5, "name": "WH/IN/00001", "state": "assigned", "origin": "P00016"}],
            # read back updated picking
            [{"id": 5, "name": "WH/IN/00001", "state": "done", "origin": "P00016"}],
        ]
        mock_connection.execute.return_value = True

        receive_inv = mock_app._tools["receive_inventory"]
        result = await receive_inv(picking_id=5)

        assert result["success"] is True
        assert result["picking_id"] == 5
        assert result["picking_name"] == "WH/IN/00001"
        assert result["origin"] == "P00016"
        assert result["state"] == "done"
        assert "stock.picking" in result["url"]

    async def test_receive_inventory_by_po_name_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful inventory receipt using po_name to find picking."""
        mock_connection.search.return_value = [5]
        mock_connection.read.side_effect = [
            # verify picking exists
            [{"id": 5, "name": "WH/IN/00001", "state": "assigned", "origin": "P00016"}],
            # read back updated picking
            [{"id": 5, "name": "WH/IN/00001", "state": "done", "origin": "P00016"}],
        ]
        mock_connection.execute.return_value = True

        receive_inv = mock_app._tools["receive_inventory"]
        result = await receive_inv(po_name="P00016")

        assert result["success"] is True
        assert result["picking_id"] == 5
        mock_connection.search.assert_called_once_with(
            "stock.picking",
            [["origin", "=", "P00016"], ["picking_type_code", "=", "incoming"]],
            limit=1,
        )

    async def test_receive_inventory_neither_id_nor_name(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test receive_inventory with neither picking_id nor po_name."""
        receive_inv = mock_app._tools["receive_inventory"]
        with pytest.raises(
            ValidationError, match="Either picking_id or po_name must be provided"
        ):
            await receive_inv()

    async def test_receive_inventory_po_name_no_picking_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test receive_inventory when no picking matches po_name."""
        mock_connection.search.return_value = []

        receive_inv = mock_app._tools["receive_inventory"]
        with pytest.raises(
            NotFoundError, match="No incoming shipment found for purchase order P00099"
        ):
            await receive_inv(po_name="P00099")

    async def test_receive_inventory_picking_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test receive_inventory when picking_id does not exist."""
        mock_connection.read.return_value = []

        receive_inv = mock_app._tools["receive_inventory"]
        with pytest.raises(
            NotFoundError, match="Stock picking with ID 999 not found"
        ):
            await receive_inv(picking_id=999)

    async def test_receive_inventory_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test receive_inventory with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        receive_inv = mock_app._tools["receive_inventory"]
        with pytest.raises(ValidationError, match="Access denied"):
            await receive_inv(picking_id=5)

    async def test_receive_inventory_validate_fails_gracefully(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that validation failure in picking is handled gracefully."""
        mock_connection.read.side_effect = [
            [{"id": 5, "name": "WH/IN/00001", "state": "assigned", "origin": "P00016"}],
            [{"id": 5, "name": "WH/IN/00001", "state": "assigned", "origin": "P00016"}],
        ]

        # Both action_assign and button_validate fail
        mock_connection.execute.side_effect = Exception("Wizard required")

        receive_inv = mock_app._tools["receive_inventory"]
        result = await receive_inv(picking_id=5)

        # Should still succeed (errors logged as warnings)
        assert result["success"] is True

    # ===================================================================
    # 8. deliver_to_customer
    # ===================================================================

    async def test_deliver_to_customer_by_picking_id_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful delivery using picking_id."""
        mock_connection.read.side_effect = [
            [{"id": 10, "name": "WH/OUT/00001", "state": "assigned", "origin": "S00042"}],
            [{"id": 10, "name": "WH/OUT/00001", "state": "done", "origin": "S00042"}],
        ]
        mock_connection.execute.return_value = True

        deliver = mock_app._tools["deliver_to_customer"]
        result = await deliver(picking_id=10)

        assert result["success"] is True
        assert result["picking_id"] == 10
        assert result["picking_name"] == "WH/OUT/00001"
        assert result["origin"] == "S00042"
        assert result["state"] == "done"
        assert "stock.picking" in result["url"]

    async def test_deliver_to_customer_by_so_name_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful delivery using so_name to find picking."""
        mock_connection.search.return_value = [10]
        mock_connection.read.side_effect = [
            [{"id": 10, "name": "WH/OUT/00001", "state": "assigned", "origin": "S00042"}],
            [{"id": 10, "name": "WH/OUT/00001", "state": "done", "origin": "S00042"}],
        ]
        mock_connection.execute.return_value = True

        deliver = mock_app._tools["deliver_to_customer"]
        result = await deliver(so_name="S00042")

        assert result["success"] is True
        assert result["picking_id"] == 10
        mock_connection.search.assert_called_once_with(
            "stock.picking",
            [["origin", "=", "S00042"], ["picking_type_code", "=", "outgoing"]],
            limit=1,
        )

    async def test_deliver_to_customer_neither_id_nor_name(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test deliver_to_customer with neither picking_id nor so_name."""
        deliver = mock_app._tools["deliver_to_customer"]
        with pytest.raises(
            ValidationError, match="Either picking_id or so_name must be provided"
        ):
            await deliver()

    async def test_deliver_to_customer_so_name_no_picking_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test deliver_to_customer when no picking matches so_name."""
        mock_connection.search.return_value = []

        deliver = mock_app._tools["deliver_to_customer"]
        with pytest.raises(
            NotFoundError, match="No outgoing delivery found for sales order S99999"
        ):
            await deliver(so_name="S99999")

    async def test_deliver_to_customer_picking_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test deliver_to_customer when picking_id does not exist."""
        mock_connection.read.return_value = []

        deliver = mock_app._tools["deliver_to_customer"]
        with pytest.raises(
            NotFoundError, match="Stock picking with ID 999 not found"
        ):
            await deliver(picking_id=999)

    async def test_deliver_to_customer_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test deliver_to_customer with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        deliver = mock_app._tools["deliver_to_customer"]
        with pytest.raises(ValidationError, match="Access denied"):
            await deliver(picking_id=10)

    async def test_deliver_to_customer_validate_fails_gracefully(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that delivery validation failure is handled gracefully."""
        mock_connection.read.side_effect = [
            [{"id": 10, "name": "WH/OUT/00001", "state": "assigned", "origin": "S00042"}],
            [{"id": 10, "name": "WH/OUT/00001", "state": "assigned", "origin": "S00042"}],
        ]
        mock_connection.execute.side_effect = Exception("Wizard required")

        deliver = mock_app._tools["deliver_to_customer"]
        result = await deliver(picking_id=10)

        # Should still succeed (errors logged as warnings)
        assert result["success"] is True

    # ===================================================================
    # 9. create_bom
    # ===================================================================

    async def test_create_bom_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test successful creation of a Bill of Materials."""
        mock_connection.read.side_effect = [
            # product validation (includes product_tmpl_id)
            [{"id": 373, "name": "Wooden Table", "product_tmpl_id": [200, "Wooden Table"]}],
            # read-back of created BOM
            [
                {
                    "id": 1,
                    "product_tmpl_id": [200, "Wooden Table"],
                    "product_qty": 1.0,
                    "type": "normal",
                }
            ],
        ]
        mock_connection.create.return_value = 1

        create_bom = mock_app._tools["create_bom"]
        result = await create_bom(
            product_id=373,
            component_lines=[
                {"product_id": 369, "quantity": 2.0},
                {"product_id": 370, "quantity": 4.0},
            ],
        )

        assert result["success"] is True
        assert result["bom_id"] == 1
        assert result["product"] == "Wooden Table"
        assert result["product_id"] == 373
        assert result["template_id"] == 200
        assert result["components_count"] == 2
        assert result["type"] == "normal"
        assert "mrp.bom" in result["url"]

        # Verify template_id was used in create call
        create_call_args = mock_connection.create.call_args
        bom_data = create_call_args[0][1]
        assert bom_data["product_tmpl_id"] == 200

    async def test_create_bom_phantom_type(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test BOM creation with phantom type."""
        mock_connection.read.side_effect = [
            [{"id": 373, "name": "Kit Product", "product_tmpl_id": [201, "Kit Product"]}],
            [
                {
                    "id": 2,
                    "product_tmpl_id": [201, "Kit Product"],
                    "product_qty": 1.0,
                    "type": "phantom",
                }
            ],
        ]
        mock_connection.create.return_value = 2

        create_bom = mock_app._tools["create_bom"]
        result = await create_bom(
            product_id=373,
            component_lines=[{"product_id": 369, "quantity": 1.0}],
            bom_type="phantom",
        )

        assert result["success"] is True
        assert result["type"] == "phantom"
        create_call_args = mock_connection.create.call_args
        bom_data = create_call_args[0][1]
        assert bom_data["type"] == "phantom"

    async def test_create_bom_product_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test BOM creation when product does not exist."""
        mock_connection.read.return_value = []

        create_bom = mock_app._tools["create_bom"]
        with pytest.raises(NotFoundError, match="Product with ID 999 not found"):
            await create_bom(
                product_id=999,
                component_lines=[{"product_id": 369, "quantity": 2.0}],
            )

    async def test_create_bom_missing_component_product_id(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test BOM creation with missing product_id in component line."""
        mock_connection.read.return_value = [
            {"id": 373, "name": "Wooden Table", "product_tmpl_id": [200, "Wooden Table"]}
        ]

        create_bom = mock_app._tools["create_bom"]
        with pytest.raises(
            ValidationError,
            match="Each component line must have 'product_id' and 'quantity'",
        ):
            await create_bom(
                product_id=373,
                component_lines=[{"quantity": 2.0}],
            )

    async def test_create_bom_missing_component_quantity(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test BOM creation with missing quantity in component line."""
        mock_connection.read.return_value = [
            {"id": 373, "name": "Wooden Table", "product_tmpl_id": [200, "Wooden Table"]}
        ]

        create_bom = mock_app._tools["create_bom"]
        with pytest.raises(
            ValidationError,
            match="Each component line must have 'product_id' and 'quantity'",
        ):
            await create_bom(
                product_id=373,
                component_lines=[{"product_id": 369}],
            )

    async def test_create_bom_mrp_not_installed(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test BOM creation when MRP module is not installed."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Model not found"
        )

        create_bom = mock_app._tools["create_bom"]
        with pytest.raises(
            ValidationError, match="MRP.*module not installed"
        ):
            await create_bom(
                product_id=373,
                component_lines=[{"product_id": 369, "quantity": 2.0}],
            )

    # ===================================================================
    # 10. get_workflow_status
    # ===================================================================

    async def test_get_workflow_status_sale_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status for a sales order with related MOs and deliveries."""
        mock_connection.read.side_effect = [
            # Sales order
            [
                {
                    "id": 1,
                    "name": "S00042",
                    "state": "sale",
                    "amount_total": 700.0,
                    "partner_id": [10, "Acme Corp"],
                }
            ],
            # Related manufacturing orders
            [{"id": 1, "name": "MO/00001", "state": "confirmed", "product_qty": 10.0}],
            # Related deliveries
            [{"id": 10, "name": "WH/OUT/00001", "state": "assigned"}],
        ]
        mock_connection.search.side_effect = [
            [1],   # MO search
            [10],  # Picking search
        ]

        get_status = mock_app._tools["get_workflow_status"]
        result = await get_status(order_id=1, order_type="sale")

        assert result["order_type"] == "sale"
        assert result["order_id"] == 1
        assert result["order"]["name"] == "S00042"
        assert "manufacturing_orders" in result
        assert len(result["manufacturing_orders"]) == 1
        assert "deliveries" in result
        assert len(result["deliveries"]) == 1

    async def test_get_workflow_status_sale_no_related(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status for a sale order with no related MOs or deliveries."""
        mock_connection.read.return_value = [
            {
                "id": 1,
                "name": "S00042",
                "state": "draft",
                "amount_total": 700.0,
                "partner_id": [10, "Acme Corp"],
            }
        ]
        mock_connection.search.return_value = []

        get_status = mock_app._tools["get_workflow_status"]
        result = await get_status(order_id=1, order_type="sale")

        assert result["order_type"] == "sale"
        assert result["order"]["name"] == "S00042"
        assert "manufacturing_orders" not in result
        assert "deliveries" not in result

    async def test_get_workflow_status_purchase_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status for a purchase order with related receipts."""
        mock_connection.read.side_effect = [
            # Purchase order
            [
                {
                    "id": 1,
                    "name": "P00016",
                    "state": "purchase",
                    "amount_total": 275.0,
                    "partner_id": [20, "Wood Supplier Inc."],
                }
            ],
            # Related receipts
            [{"id": 5, "name": "WH/IN/00001", "state": "done"}],
        ]
        mock_connection.search.return_value = [5]

        get_status = mock_app._tools["get_workflow_status"]
        result = await get_status(order_id=1, order_type="purchase")

        assert result["order_type"] == "purchase"
        assert result["order"]["name"] == "P00016"
        assert "receipts" in result
        assert len(result["receipts"]) == 1

    async def test_get_workflow_status_purchase_no_receipts(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status for a purchase with no receipts."""
        mock_connection.read.return_value = [
            {
                "id": 1,
                "name": "P00016",
                "state": "draft",
                "amount_total": 275.0,
                "partner_id": [20, "Wood Supplier Inc."],
            }
        ]
        mock_connection.search.return_value = []

        get_status = mock_app._tools["get_workflow_status"]
        result = await get_status(order_id=1, order_type="purchase")

        assert result["order_type"] == "purchase"
        assert "receipts" not in result

    async def test_get_workflow_status_manufacturing_success(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status for a manufacturing order."""
        mock_connection.read.return_value = [
            {
                "id": 1,
                "name": "MO/00001",
                "state": "confirmed",
                "product_qty": 10.0,
                "product_id": [50, "Wooden Table"],
                "origin": "S00042",
            }
        ]

        get_status = mock_app._tools["get_workflow_status"]
        result = await get_status(order_id=1, order_type="manufacturing")

        assert result["order_type"] == "manufacturing"
        assert result["order"]["name"] == "MO/00001"
        assert result["order"]["origin"] == "S00042"

    async def test_get_workflow_status_invalid_order_type(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status with an invalid order_type."""
        get_status = mock_app._tools["get_workflow_status"]
        with pytest.raises(
            ValidationError,
            match="Invalid order_type.*Must be 'sale', 'purchase', or 'manufacturing'",
        ):
            await get_status(order_id=1, order_type="invoice")

    async def test_get_workflow_status_sale_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status when sales order is not found."""
        mock_connection.read.return_value = []

        get_status = mock_app._tools["get_workflow_status"]
        with pytest.raises(NotFoundError, match="Sales order with ID 999 not found"):
            await get_status(order_id=999, order_type="sale")

    async def test_get_workflow_status_purchase_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status when purchase order is not found."""
        mock_connection.read.return_value = []

        get_status = mock_app._tools["get_workflow_status"]
        with pytest.raises(
            NotFoundError, match="Purchase order with ID 999 not found"
        ):
            await get_status(order_id=999, order_type="purchase")

    async def test_get_workflow_status_manufacturing_not_found(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status when manufacturing order is not found."""
        mock_connection.read.return_value = []

        get_status = mock_app._tools["get_workflow_status"]
        with pytest.raises(
            NotFoundError, match="Manufacturing order with ID 999 not found"
        ):
            await get_status(order_id=999, order_type="manufacturing")

    async def test_get_workflow_status_access_denied(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test workflow status with access denied."""
        mock_access_controller.validate_model_access.side_effect = AccessControlError(
            "Access denied"
        )

        get_status = mock_app._tools["get_workflow_status"]
        with pytest.raises(ValidationError, match="Access denied"):
            await get_status(order_id=1, order_type="sale")

    async def test_get_workflow_status_sale_related_search_fails_gracefully(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that failure to find related MOs/deliveries is handled silently."""
        mock_connection.read.return_value = [
            {
                "id": 1,
                "name": "S00042",
                "state": "sale",
                "amount_total": 700.0,
                "partner_id": [10, "Acme Corp"],
            }
        ]
        # Both searches for related records raise exceptions
        mock_connection.search.side_effect = Exception("MRP not installed")

        get_status = mock_app._tools["get_workflow_status"]
        result = await get_status(order_id=1, order_type="sale")

        # Should succeed without related records (bare except in code)
        assert result["order_type"] == "sale"
        assert result["order"]["name"] == "S00042"
        assert "manufacturing_orders" not in result
        assert "deliveries" not in result

    # ===================================================================
    # URL generation
    # ===================================================================

    async def test_url_generation_format(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that generated URLs follow the expected pattern."""
        mock_connection.read.side_effect = [
            [{"id": 10, "name": "Acme Corp"}],
            [
                {
                    "id": 42,
                    "name": "S00042",
                    "state": "draft",
                    "amount_total": 100.0,
                    "partner_id": [10, "Acme Corp"],
                }
            ],
        ]
        mock_connection.create.return_value = 42

        create_quotation = mock_app._tools["create_quotation"]
        result = await create_quotation(
            customer_id=10,
            product_lines=[{"product_id": 100, "quantity": 1.0}],
        )

        expected_url = "http://localhost:8069/web#id=42&model=sale.order&view_type=form"
        assert result["url"] == expected_url

    async def test_url_generation_trailing_slash_stripped(
        self, mock_app, mock_connection, mock_access_controller
    ):
        """Test that trailing slashes on URL are stripped for URL generation."""
        config = OdooConfig(
            url="http://localhost:8069/",
            api_key="test_key",
            database="test_db",
        )
        handler = OdooWorkflowHandler(
            mock_app, mock_connection, mock_access_controller, config
        )

        mock_connection.read.side_effect = [
            [{"id": 10, "name": "Acme Corp"}],
            [
                {
                    "id": 1,
                    "name": "S00001",
                    "state": "draft",
                    "amount_total": 50.0,
                    "partner_id": [10, "Acme Corp"],
                }
            ],
        ]
        mock_connection.create.return_value = 1

        create_quotation = mock_app._tools["create_quotation"]
        result = await create_quotation(
            customer_id=10,
            product_lines=[{"product_id": 100, "quantity": 1.0}],
        )

        # Should NOT have double slash
        assert "localhost:8069//web" not in result["url"]
        assert result["url"].startswith("http://localhost:8069/web#")

    # ===================================================================
    # OdooConnectionError wrapping
    # ===================================================================

    async def test_connection_error_wrapped_as_validation_error(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that OdooConnectionError in create_quotation is caught specifically."""
        # create_quotation is the only handler with an explicit OdooConnectionError catch
        mock_connection.read.side_effect = OdooConnectionError("Timeout")

        create_quotation = mock_app._tools["create_quotation"]
        with pytest.raises(ValidationError, match="Connection error"):
            await create_quotation(
                customer_id=10,
                product_lines=[{"product_id": 100, "quantity": 1.0}],
            )

    async def test_connection_error_falls_through_generic_handler(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that OdooConnectionError in other handlers falls to generic except."""
        # Handlers other than create_quotation lack specific OdooConnectionError catch
        mock_connection.read.side_effect = OdooConnectionError("Timeout")

        confirm_po = mock_app._tools["confirm_purchase_order"]
        with pytest.raises(ValidationError, match="Failed to confirm purchase order"):
            await confirm_po(po_id=1)

    # ===================================================================
    # Generic exception wrapping
    # ===================================================================

    async def test_generic_exception_wrapped(
        self, handler, mock_connection, mock_access_controller, mock_app
    ):
        """Test that unexpected exceptions are wrapped as ValidationError."""
        mock_connection.read.side_effect = RuntimeError("Unexpected")

        confirm_po = mock_app._tools["confirm_purchase_order"]
        with pytest.raises(ValidationError, match="Failed to confirm purchase order"):
            await confirm_po(po_id=1)


# ---------------------------------------------------------------------------
# register_workflow_tools function
# ---------------------------------------------------------------------------

class TestRegisterWorkflowTools:
    """Test cases for the register_workflow_tools factory function."""

    def test_register_workflow_tools_returns_handler(self):
        """Test that register_workflow_tools returns an OdooWorkflowHandler."""
        mock_app = MagicMock(spec=FastMCP)
        mock_app._tools = {}

        def tool_decorator():
            def decorator(func):
                mock_app._tools[func.__name__] = func
                return func
            return decorator

        mock_app.tool = tool_decorator

        mock_connection = MagicMock(spec=OdooConnection)
        mock_connection.is_authenticated = True
        mock_access_controller = MagicMock(spec=AccessController)
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_key",
            database="test_db",
        )

        handler = register_workflow_tools(
            mock_app, mock_connection, mock_access_controller, config
        )

        assert isinstance(handler, OdooWorkflowHandler)
        assert handler.app == mock_app
        assert handler.connection == mock_connection
        assert handler.access_controller == mock_access_controller
        assert handler.config == config

    def test_register_workflow_tools_registers_all_tools(self):
        """Test that all 10 tools are registered after calling register_workflow_tools."""
        mock_app = MagicMock(spec=FastMCP)
        mock_app._tools = {}

        def tool_decorator():
            def decorator(func):
                mock_app._tools[func.__name__] = func
                return func
            return decorator

        mock_app.tool = tool_decorator

        mock_connection = MagicMock(spec=OdooConnection)
        mock_connection.is_authenticated = True
        mock_access_controller = MagicMock(spec=AccessController)
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="test_key",
            database="test_db",
        )

        register_workflow_tools(
            mock_app, mock_connection, mock_access_controller, config
        )

        assert len(mock_app._tools) == 10
        expected = {
            "create_quotation",
            "confirm_quotation",
            "create_manufacturing_order",
            "confirm_manufacturing_order",
            "create_purchase_order",
            "confirm_purchase_order",
            "receive_inventory",
            "deliver_to_customer",
            "create_bom",
            "get_workflow_status",
        }
        assert set(mock_app._tools.keys()) == expected
