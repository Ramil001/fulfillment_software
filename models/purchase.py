from odoo import models, fields ,api
import logging
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)

class FulfillmentPurchase(models.Model):
    _inherit = 'purchase.order'
    fulfillment_purchase_id = fields.Char(string="Fulfillment purchase_id", readonly=True)

    @api.model_create_multi
    
    def create(self, vals_list):
        _logger.warning(f"[Fulfillment][Purchase][Order]: [self]: {self} | [vals_list]: {vals_list}")
        return super().create(vals_list)


    def write(self, vals):
        res = super().write(vals)

        for rec in self:
            if rec.fulfillment_purchase_id:
                if any(field in vals for field in ['partner_id', 'date_planned']):
                    rec._update_fulfillment_purchase()

        return res



    def button_confirm(self):
        res = super().button_confirm()

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            return res

        client = FulfillmentAPIClient(profile)

        for order in self:

            if order.fulfillment_purchase_id:
                continue

            warehouse = order.picking_type_id.warehouse_id
            if not warehouse.is_fulfillment or not warehouse.fulfillment_warehouse_id:
                continue

            products = [
                {
                    "product_id": line.product_id.fulfillment_product_id,
                    "quantity": int(line.product_qty),
                }
                for line in order.order_line
                if line.product_id.fulfillment_product_id and line.product_qty > 0
            ]

            if not products:
                _logger.warning(f"[FULFILLMENT] PO {order.name}: no valid products")
                continue

            payload = {
                "name": order.origin or order.name,
                "warehouse_id": warehouse.fulfillment_warehouse_id,
                "products": products,
            }

            try:
                response = client.purchase.create(payload)
                data = response.get("data", {})

                fulfillment_id = data.get("purchase_id")
                if fulfillment_id:
                    order.write({
                        "fulfillment_purchase_id": fulfillment_id
                    })

                    _logger.info(
                        f"[FULFILLMENT] Purchase created: {order.name} → {fulfillment_id}"
                    )

            except Exception:
                _logger.exception(
                    f"[FULFILLMENT] Failed to create purchase for {order.name}"
                )

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