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

    # === Переопределяем create ===
    @api.model_create_multi
    def create(self, vals_list):
        quants = super().create(vals_list)
        for quant in quants:
            quant._check_fulfillment_quant()
        return quants

    # === Переопределяем write ===
    def write(self, vals):
        res = super().write(vals)
        for quant in self:
            quant._check_fulfillment_quant()
        return res

    # === Проверка фулфиллмента ===
    def _check_fulfillment_quant(self):
        """Проверяет, принадлежит ли квант фулфиллмент-складу."""
        self.ensure_one()

        # Пропускаем, если fulfillment_stock_id уже есть
        if self.fulfillment_stock_id:
            return

        location = self.location_id
        if not location:
            return

        # Найдём склад, связанный с этой локацией
        warehouse = self.env['stock.warehouse'].search([
            '|',
            ('lot_stock_id', '=', location.id),
            ('view_location_id', 'parent_of', location.id)
        ], limit=1)

        if warehouse and warehouse.fulfillment_warehouse_id:
            _logger.info(
                "[FULFILLMENT] Новый/обновлённый квант без ID: "
                f"product={self.product_id.display_name}, "
                f"qty={self.quantity}, "
                f"location={location.complete_name}, "
                f"warehouse={warehouse.name} ({warehouse.fulfillment_warehouse_id})"
            )
