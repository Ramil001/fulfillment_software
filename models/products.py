import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)



class FulfillmentProducts(models.Model):
    _inherit = 'product.template'


    fulfillment_product_id = fields.Char(string="Fulfillment Product ID", index=True, readonly=True)
    
    fulfillment_owner_id = fields.Char(string="Fulfillment Owner ID", index=True, readonly=True)

    sale_fulfillment_partner_ids = fields.Many2many(
            'fulfillment.partners',
            'product_sale_fulfillment_rel',
            'product_id',
            'partner_id',
            string='Fulfillment Partners for Sale',
        )

    purchase_fulfillment_partner_ids = fields.Many2many(
        'fulfillment.partners',
        'product_purchase_fulfillment_rel',
        'product_id',
        'partner_id',
        string='Fulfillment Partners for Purchase',
    )
    
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("No fulfillment profile found")
            return records

        api = FulfillmentAPIClient(profile)

        for rec in records:
            payload = {
                "name": rec.name,
                "sku": rec.default_code,          # SKU
                "barcode": rec.barcode,
                "img_url": rec.image_1920 and f"/web/image/product.template/{rec.id}/image_1920" or None,
            }

            payload = {k: v for k, v in payload.items() if v}

            try:
                response = api.product.create(payload)

                fulfillment_id = response.get("data", {}).get("id")
                if not fulfillment_id:
                    _logger.error(f"No fulfillment product id in response: {response}")
                    continue

                rec.write({
                    "fulfillment_product_id": fulfillment_id
                })

                _logger.info(
                    f"Product {rec.name} linked to fulfillment product {fulfillment_id}"
                )

            except Exception as e:
                _logger.exception(f"Failed to create fulfillment product for {rec.name}: {e}")

        return records




    def write(self, vals):
        res = super(FulfillmentProducts, self).write(vals)
        bus = self.env['bus.utils']
        for rec in self:
            message = f"Продукт обновлён: {rec.name} (ID {rec.id})"
            _logger.info(message)
            bus.send_notification(
                title="Обновление продукта",
                message=message,
                level="info",
                sticky=False
            )
            rec.message_post(body=message)
        return res
    
    
    
    
