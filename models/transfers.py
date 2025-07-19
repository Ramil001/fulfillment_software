from odoo import models, fields, api
import logging
import requests
from ..services.client import FulfillmentAPIClient


class FulfillmentTransfers(models.Model):
    _inherit = 'stock.picking'