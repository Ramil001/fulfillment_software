from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

class FulfillmentPurchase(models.Model):
    _inherit = 'purchase.order'

    @api.model
  
    def create(self, vals):
        _logger.info(f"[create OVERRIDE]: {vals}")

    def write(self, vals):
        _logger.warning(f"[write OVERRIDE]: {vals}")
