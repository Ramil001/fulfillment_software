import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class FulfillmentOrders(models.Model):
    _inherit = 'sale.order'
    # Можно оставить пустым или добавить поля на уровне заказа при необходимости
    pass

class FulfillmentOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fulfillment_partner_id = fields.Many2one(
        'fulfillment.partners',
        string="Fulfillment Partner",
        help="Select the fulfillment partner for this order line"
    )