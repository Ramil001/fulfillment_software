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
        _logger.info(f"[create]")
        
        records = super().create(vals_list)

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("No fulfillment profile found")
            return records

        api = FulfillmentAPIClient(profile)

        for rec in records:
            if rec.fulfillment_product_id:
                _logger.info(
                    "[Fulfillment] Product '%s' already linked to fulfillment id %s — skipping remote create",
                    rec.name, rec.fulfillment_product_id
                )
                continue

            base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
            payload = {
                "name": rec.name,
                "sku": rec.default_code,
                "barcode": rec.barcode,
                "img_url": (
                    f"{base_url}/web/image/product.template/{rec.id}/image_1920"
                    if rec.image_1920 else None
                ),
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
        _logger.info(f"[write]")
        
        res = super(FulfillmentProducts, self).write(vals)

        sync_fields = {"name", "default_code", "barcode", "image_1920"}
        if not sync_fields.intersection(vals.keys()):
            return res

        if self.env.context.get('skip_fulfillment_push'):
            return res

        profile = self.env['fulfillment.profile'].search([], limit=1)
        api = FulfillmentAPIClient(profile) if profile else None

        for rec in self:
            # --- Build payload for API ---
            base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
            payload = {
                "name": vals.get("name", rec.name),
                "sku": vals.get("default_code", rec.default_code),
                "barcode": vals.get("barcode", rec.barcode),
                "img_url": (
                    f"{base_url}/web/image/product.template/{rec.id}/image_1920"
                    if rec.image_1920 else None
                ),
            }
            payload = {k: v for k, v in payload.items() if v}

            try:
                if rec.fulfillment_product_id and api:
                    # Update existing product
                    response = api.product.update(rec.fulfillment_product_id, payload)
                    _logger.info("[Fulfillment] Updated product %s on API (id=%s)", rec.name, rec.fulfillment_product_id)
                elif api:
                    # Create new product on Fulfillment
                    response = api.product.create(payload)
                    fulfillment_id = response.get("data", {}).get("id")
                    if fulfillment_id:
                        rec.fulfillment_product_id = fulfillment_id
                        _logger.info("[Fulfillment] Created product %s on API (id=%s)", rec.name, fulfillment_id)
                    else:
                        _logger.error("[Fulfillment] No fulfillment_product_id in API response for %s: %s", rec.name, response)
                else:
                    _logger.warning("[Fulfillment] API client not available for product %s", rec.name)

            except Exception as e:
                _logger.exception("[Fulfillment] Failed to sync product %s: %s", rec.name, e)

        return res
