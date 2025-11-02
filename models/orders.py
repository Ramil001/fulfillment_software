from odoo import models, fields

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fulfillment_partner_id = fields.Many2one(
        'fulfillment.partners',
        string='Fulfillment Partner',
        help='Кто отправляет этот товар',
    )
