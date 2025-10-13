from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)

class StockQuant(models.Model):
    _inherit = 'stock.quant'

    fulfillment_stock_id = fields.Char(
        string='Fulfillment Stock ID',
        help='External fulfillment system stock identifier',
        index=True,
        readonly=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        quants = super().create(vals_list)
        for quant in quants:
            quant._check_fulfillment_quant(is_create=True)
        return quants

    def write(self, vals):
        res = super().write(vals)
        # Не вызываем проверку на каждом write — только при создании
        return res

    def _check_fulfillment_quant(self, is_create=False):
        """Проверяет, принадлежит ли квант фулфиллмент-складу."""
        self.ensure_one()

        if self.fulfillment_stock_id:
            return

        location = self.location_id
        if not location:
            return

        warehouse = self.env['stock.warehouse'].search([
            '|',
            ('lot_stock_id', '=', location.id),
            ('view_location_id', 'parent_of', location.id)
        ], limit=1)

        if warehouse and warehouse.fulfillment_warehouse_id:
            _logger.info(
                "[FULFILLMENT] %s квант без ID: product=%s, qty=%s, location=%s, warehouse=%s (%s)",
                "Создан" if is_create else "Обновлён",
                self.product_id.display_name,
                self.quantity,
                location.complete_name,
                warehouse.name,
                warehouse.fulfillment_warehouse_id,
            )
