from odoo import models, fields, api

class FulfillmentDashboard(models.Model):
    _name = 'fulfillment.dashboard'
    _description = 'Fulfillment Dashboard'

    name = fields.Char(string="Name")
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    description = fields.Text(string="Description")

    # В вашу модель fulfillment.dashboard добавьте
    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        string='Склады',
        compute='_compute_warehouse_ids',
        store=False
    )

    @api.depends()
    def _compute_warehouse_ids(self):
        for record in self:
            warehouses = self.env['stock.warehouse'].search([])
            record.warehouse_ids = warehouses