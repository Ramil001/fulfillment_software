from odoo import models, fields, api


class FulfillmentOrder(models.Model):
    _inherit = 'sale.order'
    
    fulfillment_order_id = fields.Char(string="Fulfillment Order ID", readonly=True)
              

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fulfillment_item_manager = fields.Many2one(
        'fulfillment.partners',
        string='Fulfillment Delivery',
        help='Кто отправляет этот товар',
    )
    
    api.onchange('fulfillment_item_manager')
    
    def onchange_fulfillment_item_manager(self):
     
        if not self.fulfillment_item_manager:
         return

        
     