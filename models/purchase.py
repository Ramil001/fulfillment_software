from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    @api.model
    def web_save(self, vals, specification=None):
        _logger.warning(f"[web_save OVERRIDE] {vals}")
        return super().web_save(vals, specification)

    @api.model_create_multi
    def create(self, vals_list):
        _logger.warning(f"[create OVERRIDE] {vals_list}")
        return super().create(vals_list)

    def write(self, vals):
        _logger.warning(f"[write OVERRIDE] {vals}")
        return super().write(vals)
