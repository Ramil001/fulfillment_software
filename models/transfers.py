from odoo import models, api, _
from odoo.exceptions import ValidationError
import logging
from ..services.client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)


class FulfillmentTransfers(models.Model):
    _inherit = 'stock.picking'

    @api.model
    def create_fulfillment_receipt(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.error("[Fulfillment] Profile not found")
            return False

        client = FulfillmentAPIClient(profile)
        try:
            purchases = client.get_purchase_orders()
            _logger.info(f"[PURCHASES]: {purchases}")
        except Exception as e:
            raise ValidationError(_("Fulfillment API error: %s") % str(e))

        partner = self.env['res.partner'].search([], limit=1)
        if not partner:
            partner = self.env['res.partner'].create({'name': 'Fulfillment Partner'})

        picking_type = self.env.ref('stock.picking_type_in', raise_if_not_found=False)
        location_suppliers = self.env.ref('stock.stock_location_suppliers', raise_if_not_found=False)
        location_stock = self.env.ref('stock.stock_location_stock', raise_if_not_found=False)

        for purchase in purchases:
            picking = self.env['stock.picking'].create({
                'partner_id': partner.id,
                'picking_type_id': picking_type.id if picking_type else False,
                'location_id': location_suppliers.id if location_suppliers else False,
                'location_dest_id': location_stock.id if location_stock else False,
                'origin': purchase['name'],
            })

            for order in purchase.get('orders', []):
                product_code = f"FULFILL-{order['product_id']}"
                product = self.env['product.product'].search([
                    ('default_code', '=', product_code)
                ], limit=1)

                if not product:
                    product = self.env['product.product'].create({
                        'name': f"Fulfillment Product {order['product_id']}",
                        'default_code': product_code,
                        'type': 'product',
                    })
                    _logger.info(f"[Fulfillment] Created product {product.name}")

                self.env['stock.move'].create({
                    'product_id': product.id,
                    'name': purchase['name'],
                    'product_uom_qty': order['quantity'],
                    'product_uom': product.uom_id.id,
                    'picking_id': picking.id,
                    'location_id': picking.location_id.id,
                    'location_dest_id': picking.location_dest_id.id,
                })

            picking.action_confirm()
            _logger.info(f"[Fulfillment] Created picking {picking.name} from purchase {purchase['name']}")

        return True

