from odoo import models, fields


class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'
    
    is_fulfillment = fields.Boolean(string="Is this a fulfillment warehouse?")
    fulfillment_creator_id = fields.Char(string="Fulfillment created Id")
    fulfillment_owner_id = fields.Many2one(
     'fulfillment.partners',
     string="Linked fulfillment partner"
    )

   
    
    