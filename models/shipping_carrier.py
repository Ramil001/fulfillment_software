# -*- coding: utf-8 -*-
from odoo import fields, models


class FulfillmentShippingCarrier(models.Model):
    _name = "fulfillment.shipping.carrier"
    _description = "Shipping carrier"
    _order = "country_group, name"
    _rec_name = "name"

    name = fields.Char(string="Carrier name", required=True)
    code = fields.Char(string="Code", help="Short identifier used in API payloads")
    country_group = fields.Selection([
        ("de", "Germany"),
        ("eu", "EU / Europe"),
        ("int", "International"),
        ("ua", "Ukraine / CIS"),
    ], string="Region", default="eu")
    tracking_url = fields.Char(string="Tracking URL")
    active = fields.Boolean(default=True)
