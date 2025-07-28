import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)



class FulfillmentProducts(models.Model):
    _inherit = 'product.template'


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