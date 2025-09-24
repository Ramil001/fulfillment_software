import logging
from odoo import models, fields, api

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