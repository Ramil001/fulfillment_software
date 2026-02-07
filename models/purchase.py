from odoo import models, fields ,api
import logging
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)

class FulfillmentPurchase(models.Model):
    _inherit = 'purchase.order'
    fulfillment_purchase_id = fields.Char(string="Fulfillment purchase_id", readonly=True)
#
    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            return orders

        client = FulfillmentAPIClient(profile)

        for order in orders:
            picking_type = order.picking_type_id
            warehouse = picking_type.warehouse_id if picking_type else False

            if not warehouse or not warehouse.is_fulfillment:
                continue

            fulfillment_warehouse_id = warehouse.fulfillment_warehouse_id
            if not fulfillment_warehouse_id:
                continue

            products = []
            for line in order.order_line:
                product = line.product_id
                if not product or not product.fulfillment_product_id:
                    continue

                if line.product_qty <= 0:
                    continue

                products.append({
                    "product_id": product.fulfillment_product_id,
                    "quantity": int(line.product_qty),
                })

            if not products:
                continue

            payload = {
                "name": order.origin or order.name,
                "warehouse_id": fulfillment_warehouse_id,
                "products": products,
            }

            try:
                response = client.purchase.create(payload)
                data = response.get("data")

                if isinstance(data, list) and data:
                    order.fulfillment_purchase_id = data[0].get("id")

                _logger.info(
                    f"[FULFILLMENT] Purchase created for PO {order.name} → {order.fulfillment_purchase_id}"
                )

            except Exception as e:
                _logger.error(
                    f"[FULFILLMENT] Failed to create purchase for PO {order.name}: {e}"
                )

        return orders


    def write(self, vals):
        _logger.warning(f"[WRITE OVERRIDE]: {vals}")

        res = super().write(vals)

        for order in self:
            picking_type = False

            if 'picking_type_id' in vals and vals.get('picking_type_id'):
                pid = vals.get('picking_type_id')
                if isinstance(pid, list):
                    pid = pid[0]
                picking_type = self.env['stock.picking.type'].browse(pid)

            elif order.warehouse_id and order.warehouse_id.in_type_id:
                picking_type = order.warehouse_id.in_type_id

            if not picking_type or not picking_type.exists():
                _logger.info(f"[WRITE] PO {order.name}: No picking type resolved")
                continue

            warehouse = picking_type.warehouse_id
            if not warehouse:
                _logger.warning(f"[WRITE] Picking Type {picking_type.display_name} has NO warehouse")
                continue

            _logger.info(
                f"[WRITE] PO {order.name}: "
                f"Picking Type → {picking_type.display_name}, "
                f"Warehouse → {warehouse.display_name}, "
                f"is_fulfillment={warehouse.is_fulfillment}"
            )

        
            line_info = [
                (line.product_id.id, line.product_id.display_name, line.product_qty)
                for line in order.order_line
                if line.product_id
            ]
            _logger.info(f"[WRITE] PO {order.name}: Products: {line_info}")

        return res



    @api.model
    def import_purchase(self, purchase_ids=None):
        """
        Send existing purchase orders to Fulfillment API.
        If purchase_ids is None, sends all draft orders.
        """
        domain = [('state', '=', 'draft')]
        if purchase_ids:
            domain += [('id', 'in', purchase_ids)]

        orders = self.search(domain)
        if not orders:
            _logger.info("[IMPORT PURCHASE] No purchase orders to send.")
            return

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[IMPORT PURCHASE] No fulfillment profile found.")
            return

        fulfillmentApiClient = FulfillmentAPIClient(profile)

        for order in orders:
            _logger.info(f"[IMPORT PURCHASE] Processing PO {order.name}")

            picking_type = order.picking_type_id
            if not picking_type or not picking_type.warehouse_id:
                _logger.warning(f"[IMPORT PURCHASE] PO {order.name}: No warehouse found, skipping.")
                continue

            warehouse = picking_type.warehouse_id
            fulfillment_warehouse_id = warehouse.fulfillment_warehouse_id

            # Prepare products list
            products = []
            for line in order.order_line:
                if not line.product_id:
                    continue
                products.append({
                    "product_id": line.product_id.id,
                    "name": line.product_id.name,
                    "quantity": line.product_qty,
                    "price": line.price_unit,
                    "warehouse_id": fulfillment_warehouse_id,
                })

            payload = {
                "name": order.origin or order.name,
                "warehouse_id": fulfillment_warehouse_id,
                "products": products,
            }

            # Send to Fulfillment API
            try:
                fulfillmentApiClient.purchase.create(payload, fulfillment_warehouse_id)
                _logger.info(f"[IMPORT PURCHASE] PO {order.name} sent to Fulfillment API (warehouse_id={warehouse.id})")
            except Exception as e:
                _logger.error(f"[IMPORT PURCHASE] PO {order.name} API call failed: {e}")