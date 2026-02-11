"""Workflow-specific tool handlers for Odoo operations.

This module implements higher-level workflow tools that combine multiple
basic operations into complete business processes like quotations, manufacturing,
purchases, and deliveries.

Based on tested workflows from odoo-ai-agentic project.
"""

from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .access_control import AccessControlError, AccessController
from .config import OdooConfig
from .error_handling import NotFoundError, ValidationError
from .logging_config import get_logger, perf_logger
from .odoo_connection import OdooConnection, OdooConnectionError

logger = get_logger(__name__)


class OdooWorkflowHandler:
    """Handles workflow-specific MCP tools for Odoo operations."""

    def __init__(
        self,
        app: FastMCP,
        connection: OdooConnection,
        access_controller: AccessController,
        config: OdooConfig,
    ):
        """Initialize workflow handler.

        Args:
            app: FastMCP application instance
            connection: Odoo connection instance
            access_controller: Access control instance
            config: Odoo configuration instance
        """
        self.app = app
        self.connection = connection
        self.access_controller = access_controller
        self.config = config

        # Register workflow tools
        self._register_workflow_tools()

    def _register_workflow_tools(self):
        """Register all workflow tool handlers with FastMCP."""

        @self.app.tool()
        async def create_quotation(
            customer_id: int,
            product_lines: List[Dict[str, Any]],
            order_date: Optional[str] = None,
        ) -> Dict[str, Any]:
            """Create a sales quotation with order lines.

            Args:
                customer_id: Partner ID of the customer
                product_lines: List of order lines, each containing:
                    - product_id: Product ID
                    - quantity: Quantity to order
                    - price_unit: Unit price (optional, uses product price if not provided)
                order_date: Order date in YYYY-MM-DD format (optional, uses today if not provided)

            Example:
                create_quotation(
                    customer_id=15,
                    product_lines=[
                        {"product_id": 123, "quantity": 2.0, "price_unit": 350.0},
                        {"product_id": 124, "quantity": 1.0}
                    ]
                )

            Returns:
                Dictionary with quotation details including ID, name, total, and URL
            """
            return await self._handle_create_quotation(customer_id, product_lines, order_date)

        @self.app.tool()
        async def confirm_quotation(quotation_id: int) -> Dict[str, Any]:
            """Confirm a quotation to convert it to a sales order.

            Args:
                quotation_id: ID of the quotation to confirm

            Returns:
                Dictionary with confirmed sales order details
            """
            return await self._handle_confirm_quotation(quotation_id)

        @self.app.tool()
        async def create_manufacturing_order(
            product_id: int,
            quantity: float,
            origin: Optional[str] = None,
        ) -> Dict[str, Any]:
            """Create a manufacturing order for a product.

            Requires MRP (Manufacturing) module to be installed.

            Args:
                product_id: Product ID to manufacture
                quantity: Quantity to produce
                origin: Source document reference (e.g., sales order name)

            Returns:
                Dictionary with manufacturing order details
            """
            return await self._handle_create_manufacturing_order(product_id, quantity, origin)

        @self.app.tool()
        async def confirm_manufacturing_order(mo_id: int) -> Dict[str, Any]:
            """Confirm and start a manufacturing order.

            This will:
            1. Confirm the manufacturing order
            2. Assign raw materials from inventory
            3. Mark as ready for production

            Args:
                mo_id: Manufacturing order ID

            Returns:
                Dictionary with updated manufacturing order status
            """
            return await self._handle_confirm_manufacturing_order(mo_id)

        @self.app.tool()
        async def create_purchase_order(
            vendor_id: int,
            product_lines: List[Dict[str, Any]],
        ) -> Dict[str, Any]:
            """Create a purchase order for raw materials or products.

            Args:
                vendor_id: Partner ID of the vendor
                product_lines: List of purchase lines, each containing:
                    - product_id: Product ID
                    - quantity: Quantity to purchase
                    - price_unit: Unit price

            Example:
                create_purchase_order(
                    vendor_id=42,
                    product_lines=[
                        {"product_id": 100, "quantity": 10.0, "price_unit": 15.0},
                        {"product_id": 101, "quantity": 5.0, "price_unit": 25.0}
                    ]
                )

            Returns:
                Dictionary with purchase order details
            """
            return await self._handle_create_purchase_order(vendor_id, product_lines)

        @self.app.tool()
        async def confirm_purchase_order(po_id: int) -> Dict[str, Any]:
            """Confirm a purchase order.

            This creates the incoming shipment for receiving goods.

            Args:
                po_id: Purchase order ID

            Returns:
                Dictionary with confirmed purchase order details
            """
            return await self._handle_confirm_purchase_order(po_id)

        @self.app.tool()
        async def receive_inventory(
            picking_id: Optional[int] = None,
            po_name: Optional[str] = None,
        ) -> Dict[str, Any]:
            """Receive inventory from a purchase order (validate incoming shipment).

            Provide either picking_id OR po_name to identify the shipment.

            Args:
                picking_id: Stock picking ID (incoming shipment)
                po_name: Purchase order name (e.g., "P00016") to find related picking

            Returns:
                Dictionary with receipt confirmation
            """
            return await self._handle_receive_inventory(picking_id, po_name)

        @self.app.tool()
        async def deliver_to_customer(
            picking_id: Optional[int] = None,
            so_name: Optional[str] = None,
        ) -> Dict[str, Any]:
            """Deliver products to customer (validate outgoing delivery).

            Provide either picking_id OR so_name to identify the delivery.

            Args:
                picking_id: Stock picking ID (outgoing delivery)
                so_name: Sales order name (e.g., "S00276") to find related picking

            Returns:
                Dictionary with delivery confirmation
            """
            return await self._handle_deliver_to_customer(picking_id, so_name)

        @self.app.tool()
        async def create_bom(
            product_id: int,
            component_lines: List[Dict[str, Any]],
            bom_type: str = "normal",
        ) -> Dict[str, Any]:
            """Create a Bill of Materials for a product.

            Requires MRP (Manufacturing) module to be installed.

            Args:
                product_id: Finished product ID
                component_lines: List of components, each containing:
                    - product_id: Component product ID
                    - quantity: Quantity needed
                bom_type: Type of BOM ('normal', 'phantom', or 'subcontract')

            Example:
                create_bom(
                    product_id=373,
                    component_lines=[
                        {"product_id": 369, "quantity": 2.0},  # 2x Wood Plank
                        {"product_id": 370, "quantity": 4.0},  # 4x Table Leg
                    ]
                )

            Returns:
                Dictionary with BOM details
            """
            return await self._handle_create_bom(product_id, component_lines, bom_type)

        @self.app.tool()
        async def get_workflow_status(
            order_id: int,
            order_type: str = "sale",
        ) -> Dict[str, Any]:
            """Get complete workflow status for an order.

            Traces the order through its complete lifecycle:
            - Sales Order → Manufacturing → Purchase → Delivery → Invoice

            Args:
                order_id: Order ID
                order_type: Type of order ('sale', 'purchase', or 'manufacturing')

            Returns:
                Dictionary with complete workflow status including:
                - Order details
                - Related manufacturing orders
                - Related purchase orders
                - Related deliveries/receipts
                - Related invoices
            """
            return await self._handle_get_workflow_status(order_id, order_type)

    async def _handle_create_quotation(
        self,
        customer_id: int,
        product_lines: List[Dict[str, Any]],
        order_date: Optional[str],
    ) -> Dict[str, Any]:
        """Handle create quotation request."""
        try:
            with perf_logger.track_operation("workflow_create_quotation"):
                # Check model access
                self.access_controller.validate_model_access("sale.order", "create")

                # Validate customer exists
                customer = self.connection.read("res.partner", [customer_id], ["name"])
                if not customer:
                    raise NotFoundError(f"Customer with ID {customer_id} not found")

                # Build order line data
                order_lines = []
                for line in product_lines:
                    if "product_id" not in line or "quantity" not in line:
                        raise ValidationError(
                            "Each product line must have 'product_id' and 'quantity'"
                        )

                    line_data = {
                        "product_id": line["product_id"],
                        "product_uom_qty": line["quantity"],
                    }

                    # Add price if provided, otherwise Odoo uses product's list price
                    if "price_unit" in line:
                        line_data["price_unit"] = line["price_unit"]

                    order_lines.append((0, 0, line_data))

                # Create quotation
                quotation_data = {
                    "partner_id": customer_id,
                    "order_line": order_lines,
                }

                if order_date:
                    quotation_data["date_order"] = order_date

                quotation_id = self.connection.create("sale.order", quotation_data)

                # Read back created quotation
                quotation = self.connection.read(
                    "sale.order",
                    [quotation_id],
                    ["name", "id", "state", "amount_total", "partner_id"],
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={quotation_id}&model=sale.order&view_type=form"

                return {
                    "success": True,
                    "quotation_id": quotation_id,
                    "quotation_name": quotation["name"],
                    "customer": quotation["partner_id"][1],
                    "total": quotation["amount_total"],
                    "state": quotation["state"],
                    "url": url,
                    "message": f"Successfully created quotation {quotation['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error creating quotation: {e}")
            raise ValidationError(f"Failed to create quotation: {str(e)}") from e

    async def _handle_confirm_quotation(self, quotation_id: int) -> Dict[str, Any]:
        """Handle confirm quotation request."""
        try:
            with perf_logger.track_operation("workflow_confirm_quotation"):
                # Check model access
                self.access_controller.validate_model_access("sale.order", "write")

                # Verify quotation exists
                quotation = self.connection.read(
                    "sale.order", [quotation_id], ["name", "state", "amount_total"]
                )
                if not quotation:
                    raise NotFoundError(f"Quotation with ID {quotation_id} not found")

                quotation = quotation[0]

                if quotation["state"] != "draft":
                    raise ValidationError(
                        f"Quotation {quotation['name']} is in state '{quotation['state']}', "
                        f"cannot confirm (must be 'draft')"
                    )

                # Confirm the quotation
                self.connection.execute(
                    "sale.order", "action_confirm", [quotation_id]
                )

                # Read updated state
                updated = self.connection.read(
                    "sale.order", [quotation_id], ["name", "state", "amount_total"]
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={quotation_id}&model=sale.order&view_type=form"

                return {
                    "success": True,
                    "order_id": quotation_id,
                    "order_name": updated["name"],
                    "state": updated["state"],
                    "total": updated["amount_total"],
                    "url": url,
                    "message": f"Successfully confirmed quotation {updated['name']} → sales order",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error confirming quotation: {e}")
            raise ValidationError(f"Failed to confirm quotation: {str(e)}") from e

    async def _handle_create_manufacturing_order(
        self,
        product_id: int,
        quantity: float,
        origin: Optional[str],
    ) -> Dict[str, Any]:
        """Handle create manufacturing order request."""
        try:
            with perf_logger.track_operation("workflow_create_manufacturing_order"):
                # Check if MRP module is available
                try:
                    self.access_controller.validate_model_access("mrp.production", "create")
                except:
                    raise ValidationError(
                        "MRP (Manufacturing) module not installed or not accessible. "
                        "Install the Manufacturing app in Odoo first."
                    )

                # Validate product exists
                product = self.connection.read("product.product", [product_id], ["name"])
                if not product:
                    raise NotFoundError(f"Product with ID {product_id} not found")

                # Create manufacturing order
                mo_data = {
                    "product_id": product_id,
                    "product_qty": quantity,
                }

                if origin:
                    mo_data["origin"] = origin

                mo_id = self.connection.create("mrp.production", mo_data)

                # Read created MO
                mo = self.connection.read(
                    "mrp.production",
                    [mo_id],
                    ["name", "id", "state", "product_qty", "product_id"],
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={mo_id}&model=mrp.production&view_type=form"

                return {
                    "success": True,
                    "mo_id": mo_id,
                    "mo_name": mo["name"],
                    "product": mo["product_id"][1],
                    "quantity": mo["product_qty"],
                    "state": mo["state"],
                    "url": url,
                    "message": f"Successfully created manufacturing order {mo['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error creating manufacturing order: {e}")
            raise ValidationError(f"Failed to create manufacturing order: {str(e)}") from e

    async def _handle_confirm_manufacturing_order(self, mo_id: int) -> Dict[str, Any]:
        """Handle confirm manufacturing order request."""
        try:
            with perf_logger.track_operation("workflow_confirm_manufacturing_order"):
                # Check model access
                self.access_controller.validate_model_access("mrp.production", "write")

                # Verify MO exists
                mo = self.connection.read("mrp.production", [mo_id], ["name", "state"])
                if not mo:
                    raise NotFoundError(f"Manufacturing order with ID {mo_id} not found")

                mo = mo[0]

                # Confirm MO
                self.connection.execute("mrp.production", "action_confirm", [mo_id])

                # Assign materials
                try:
                    self.connection.execute("mrp.production", "action_assign", [mo_id])
                except Exception as e:
                    logger.warning(f"Could not auto-assign materials: {e}")

                # Read updated state
                updated = self.connection.read(
                    "mrp.production", [mo_id], ["name", "state", "product_qty"]
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={mo_id}&model=mrp.production&view_type=form"

                return {
                    "success": True,
                    "mo_id": mo_id,
                    "mo_name": updated["name"],
                    "state": updated["state"],
                    "quantity": updated["product_qty"],
                    "url": url,
                    "message": f"Successfully confirmed manufacturing order {updated['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error confirming manufacturing order: {e}")
            raise ValidationError(f"Failed to confirm manufacturing order: {str(e)}") from e

    async def _handle_create_purchase_order(
        self,
        vendor_id: int,
        product_lines: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Handle create purchase order request."""
        try:
            with perf_logger.track_operation("workflow_create_purchase_order"):
                # Check model access
                self.access_controller.validate_model_access("purchase.order", "create")

                # Validate vendor exists
                vendor = self.connection.read("res.partner", [vendor_id], ["name"])
                if not vendor:
                    raise NotFoundError(f"Vendor with ID {vendor_id} not found")

                # Build order line data
                order_lines = []
                for line in product_lines:
                    if (
                        "product_id" not in line
                        or "quantity" not in line
                        or "price_unit" not in line
                    ):
                        raise ValidationError(
                            "Each product line must have 'product_id', 'quantity', and 'price_unit'"
                        )

                    line_data = {
                        "product_id": line["product_id"],
                        "product_qty": line["quantity"],
                        "price_unit": line["price_unit"],
                    }

                    order_lines.append((0, 0, line_data))

                # Create purchase order
                po_data = {
                    "partner_id": vendor_id,
                    "order_line": order_lines,
                }

                po_id = self.connection.create("purchase.order", po_data)

                # Read back created PO
                po = self.connection.read(
                    "purchase.order",
                    [po_id],
                    ["name", "id", "state", "amount_total", "partner_id"],
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={po_id}&model=purchase.order&view_type=form"

                return {
                    "success": True,
                    "po_id": po_id,
                    "po_name": po["name"],
                    "vendor": po["partner_id"][1],
                    "total": po["amount_total"],
                    "state": po["state"],
                    "url": url,
                    "message": f"Successfully created purchase order {po['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error creating purchase order: {e}")
            raise ValidationError(f"Failed to create purchase order: {str(e)}") from e

    async def _handle_confirm_purchase_order(self, po_id: int) -> Dict[str, Any]:
        """Handle confirm purchase order request."""
        try:
            with perf_logger.track_operation("workflow_confirm_purchase_order"):
                # Check model access
                self.access_controller.validate_model_access("purchase.order", "write")

                # Verify PO exists
                po = self.connection.read("purchase.order", [po_id], ["name", "state"])
                if not po:
                    raise NotFoundError(f"Purchase order with ID {po_id} not found")

                po = po[0]

                # Confirm PO
                self.connection.execute(
                    "purchase.order", "button_confirm", [po_id]
                )

                # Read updated state
                updated = self.connection.read(
                    "purchase.order", [po_id], ["name", "state", "amount_total"]
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={po_id}&model=purchase.order&view_type=form"

                return {
                    "success": True,
                    "po_id": po_id,
                    "po_name": updated["name"],
                    "state": updated["state"],
                    "total": updated["amount_total"],
                    "url": url,
                    "message": f"Successfully confirmed purchase order {updated['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error confirming purchase order: {e}")
            raise ValidationError(f"Failed to confirm purchase order: {str(e)}") from e

    async def _handle_receive_inventory(
        self,
        picking_id: Optional[int],
        po_name: Optional[str],
    ) -> Dict[str, Any]:
        """Handle receive inventory request."""
        try:
            with perf_logger.track_operation("workflow_receive_inventory"):
                # Check model access
                self.access_controller.validate_model_access("stock.picking", "write")

                # Find picking if not provided
                if not picking_id and not po_name:
                    raise ValidationError("Either picking_id or po_name must be provided")

                if not picking_id:
                    # Find picking by PO name
                    pickings = self.connection.search(
                        "stock.picking",
                        [["origin", "=", po_name], ["picking_type_code", "=", "incoming"]],
                        limit=1,
                    )
                    if not pickings:
                        raise NotFoundError(
                            f"No incoming shipment found for purchase order {po_name}"
                        )
                    picking_id = pickings[0]

                # Verify picking exists
                picking = self.connection.read(
                    "stock.picking", [picking_id], ["name", "state", "origin"]
                )
                if not picking:
                    raise NotFoundError(f"Stock picking with ID {picking_id} not found")

                picking = picking[0]

                # Validate picking
                try:
                    self.connection.execute(
                        "stock.picking", "action_assign", [picking_id]
                    )
                    self.connection.execute(
                        "stock.picking", "button_validate", [picking_id]
                    )
                except Exception as e:
                    logger.warning(f"Validation may require UI: {e}")

                # Read updated state
                updated = self.connection.read(
                    "stock.picking", [picking_id], ["name", "state", "origin"]
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={picking_id}&model=stock.picking&view_type=form"

                return {
                    "success": True,
                    "picking_id": picking_id,
                    "picking_name": updated["name"],
                    "origin": updated.get("origin", ""),
                    "state": updated["state"],
                    "url": url,
                    "message": f"Successfully received inventory: {updated['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error receiving inventory: {e}")
            raise ValidationError(f"Failed to receive inventory: {str(e)}") from e

    async def _handle_deliver_to_customer(
        self,
        picking_id: Optional[int],
        so_name: Optional[str],
    ) -> Dict[str, Any]:
        """Handle deliver to customer request."""
        try:
            with perf_logger.track_operation("workflow_deliver_to_customer"):
                # Check model access
                self.access_controller.validate_model_access("stock.picking", "write")

                # Find picking if not provided
                if not picking_id and not so_name:
                    raise ValidationError("Either picking_id or so_name must be provided")

                if not picking_id:
                    # Find picking by SO name
                    pickings = self.connection.search(
                        "stock.picking",
                        [["origin", "=", so_name], ["picking_type_code", "=", "outgoing"]],
                        limit=1,
                    )
                    if not pickings:
                        raise NotFoundError(
                            f"No outgoing delivery found for sales order {so_name}"
                        )
                    picking_id = pickings[0]

                # Verify picking exists
                picking = self.connection.read(
                    "stock.picking", [picking_id], ["name", "state", "origin"]
                )
                if not picking:
                    raise NotFoundError(f"Stock picking with ID {picking_id} not found")

                picking = picking[0]

                # Validate picking
                try:
                    self.connection.execute(
                        "stock.picking", "action_assign", [picking_id]
                    )
                    self.connection.execute(
                        "stock.picking", "button_validate", [picking_id]
                    )
                except Exception as e:
                    logger.warning(f"Validation may require UI: {e}")

                # Read updated state
                updated = self.connection.read(
                    "stock.picking", [picking_id], ["name", "state", "origin"]
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={picking_id}&model=stock.picking&view_type=form"

                return {
                    "success": True,
                    "picking_id": picking_id,
                    "picking_name": updated["name"],
                    "origin": updated.get("origin", ""),
                    "state": updated["state"],
                    "url": url,
                    "message": f"Successfully delivered to customer: {updated['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error delivering to customer: {e}")
            raise ValidationError(f"Failed to deliver: {str(e)}") from e

    async def _handle_create_bom(
        self,
        product_id: int,
        component_lines: List[Dict[str, Any]],
        bom_type: str,
    ) -> Dict[str, Any]:
        """Handle create BOM request."""
        try:
            with perf_logger.track_operation("workflow_create_bom"):
                # Check if MRP module is available
                try:
                    self.access_controller.validate_model_access("mrp.bom", "create")
                except:
                    raise ValidationError(
                        "MRP (Manufacturing) module not installed or not accessible"
                    )

                # Validate product exists and get template ID
                product = self.connection.read(
                    "product.product", [product_id], ["name", "product_tmpl_id"]
                )
                if not product:
                    raise NotFoundError(f"Product with ID {product_id} not found")

                product = product[0]
                template_id = product["product_tmpl_id"][0]  # Extract ID from tuple

                # Build BOM lines
                bom_lines = []
                for line in component_lines:
                    if "product_id" not in line or "quantity" not in line:
                        raise ValidationError(
                            "Each component line must have 'product_id' and 'quantity'"
                        )

                    line_data = {
                        "product_id": line["product_id"],
                        "product_qty": line["quantity"],
                    }

                    bom_lines.append((0, 0, line_data))

                # Create BOM
                bom_data = {
                    "product_tmpl_id": template_id,  # Use template ID!
                    "product_qty": 1.0,
                    "type": bom_type,
                    "bom_line_ids": bom_lines,
                }

                bom_id = self.connection.create("mrp.bom", bom_data)

                # Read created BOM
                bom = self.connection.read(
                    "mrp.bom",
                    [bom_id],
                    ["id", "product_tmpl_id", "product_qty", "type"],
                )[0]

                # Generate URL
                base_url = self.config.url.rstrip("/")
                url = f"{base_url}/web#id={bom_id}&model=mrp.bom&view_type=form"

                return {
                    "success": True,
                    "bom_id": bom_id,
                    "product": product["name"],
                    "product_id": product_id,
                    "template_id": template_id,
                    "components_count": len(component_lines),
                    "type": bom["type"],
                    "url": url,
                    "message": f"Successfully created BOM for {product['name']}",
                }

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error creating BOM: {e}")
            raise ValidationError(f"Failed to create BOM: {str(e)}") from e

    async def _handle_get_workflow_status(
        self,
        order_id: int,
        order_type: str,
    ) -> Dict[str, Any]:
        """Handle get workflow status request."""
        try:
            with perf_logger.track_operation("workflow_get_status"):
                status = {"order_type": order_type, "order_id": order_id}

                if order_type == "sale":
                    # Get sales order
                    self.access_controller.validate_model_access("sale.order", "read")
                    order = self.connection.read(
                        "sale.order",
                        [order_id],
                        ["name", "state", "amount_total", "partner_id"],
                    )
                    if not order:
                        raise NotFoundError(f"Sales order with ID {order_id} not found")

                    status["order"] = order[0]
                    order_name = order[0]["name"]

                    # Find related manufacturing orders
                    try:
                        mo_ids = self.connection.search(
                            "mrp.production", [["origin", "=", order_name]]
                        )
                        if mo_ids:
                            mos = self.connection.read(
                                "mrp.production", mo_ids, ["name", "state", "product_qty"]
                            )
                            status["manufacturing_orders"] = mos
                    except:
                        pass

                    # Find related deliveries
                    try:
                        picking_ids = self.connection.search(
                            "stock.picking",
                            [["origin", "=", order_name], ["picking_type_code", "=", "outgoing"]],
                        )
                        if picking_ids:
                            pickings = self.connection.read(
                                "stock.picking", picking_ids, ["name", "state"]
                            )
                            status["deliveries"] = pickings
                    except:
                        pass

                elif order_type == "purchase":
                    # Get purchase order
                    self.access_controller.validate_model_access("purchase.order", "read")
                    order = self.connection.read(
                        "purchase.order",
                        [order_id],
                        ["name", "state", "amount_total", "partner_id"],
                    )
                    if not order:
                        raise NotFoundError(f"Purchase order with ID {order_id} not found")

                    status["order"] = order[0]
                    order_name = order[0]["name"]

                    # Find related receipts
                    try:
                        picking_ids = self.connection.search(
                            "stock.picking",
                            [["origin", "=", order_name], ["picking_type_code", "=", "incoming"]],
                        )
                        if picking_ids:
                            pickings = self.connection.read(
                                "stock.picking", picking_ids, ["name", "state"]
                            )
                            status["receipts"] = pickings
                    except:
                        pass

                elif order_type == "manufacturing":
                    # Get manufacturing order
                    self.access_controller.validate_model_access("mrp.production", "read")
                    order = self.connection.read(
                        "mrp.production",
                        [order_id],
                        ["name", "state", "product_qty", "product_id", "origin"],
                    )
                    if not order:
                        raise NotFoundError(f"Manufacturing order with ID {order_id} not found")

                    status["order"] = order[0]

                else:
                    raise ValidationError(
                        f"Invalid order_type: {order_type}. "
                        f"Must be 'sale', 'purchase', or 'manufacturing'"
                    )

                return status

        except ValidationError:
            raise
        except NotFoundError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except Exception as e:
            logger.error(f"Error getting workflow status: {e}")
            raise ValidationError(f"Failed to get workflow status: {str(e)}") from e


def register_workflow_tools(
    app: FastMCP,
    connection: OdooConnection,
    access_controller: AccessController,
    config: OdooConfig,
) -> OdooWorkflowHandler:
    """Register all Odoo workflow tools with the FastMCP app.

    Args:
        app: FastMCP application instance
        connection: Odoo connection instance
        access_controller: Access control instance
        config: Odoo configuration instance

    Returns:
        The workflow handler instance
    """
    handler = OdooWorkflowHandler(app, connection, access_controller, config)
    logger.info("Registered Odoo workflow tools")
    return handler
