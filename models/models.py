# -*- coding: utf-8 -*-

# from odoo import models, fields, api


# class fulfillment_software(models.Model):
#     _name = 'fulfillment_software.fulfillment_software'
#     _description = 'fulfillment_software.fulfillment_software'

#     name = fields.Char()
#     value = fields.Integer()
#     value2 = fields.Float(compute="_value_pc", store=True)
#     description = fields.Text()
#
#     @api.depends('value')
#     def _value_pc(self):
#         for record in self:
#             record.value2 = float(record.value) / 100

