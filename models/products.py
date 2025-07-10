import logging
import requests
from odoo import models, fields, api
from ..services.client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)



class FulfillmentProducts(models.Model):
    _inherit = 'product.template'
