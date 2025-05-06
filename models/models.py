from odoo import models, fields

class FulfillmentDashboard(models.Model):
    _name = 'fulfillment.dashboard'
    _description = 'Fulfillment Dashboard'

    name = fields.Char(string='Name')
