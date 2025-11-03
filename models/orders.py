from odoo import models, fields


class FulfillmentOrder(models.Model):
    _inherit = 'sale.order'
    
    fulfillment_order_id = fields.Char(string="Fulfillment Order ID", readonly=True)
         
     

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fulfillment_partner_id = fields.Many2one(
        'fulfillment.partners',
        string='Fulfillment Delivery',
        help='Кто отправляет этот товар',
    )
