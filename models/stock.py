from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)

class StockQuant(models.Model):
    _inherit = 'stock.quant'

    fulfillment_stock_id = fields.Char(
        string='Fulfillment Stock ID',
        help='External fulfillment system stock identifier',
        index=True,
    )

    @api.model_create_multi
    
    def create(self, vals_list):
        quants = super().create(vals_list)
        for quant in quants:
            quant._log_fulfillment_event(is_create=True)
        return quants

    def _update_available_quantity(self, product, location, quantity=None, **kwargs):
        """Переопределяем системное обновление остатков."""
        # Просто вызываем оригинальную реализацию с любыми аргументами, что придут
        result = super()._update_available_quantity(product, location, quantity=quantity, **kwargs)

        # Ищем квант
        quant = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', '=', location.id)
        ], limit=1)

        if quant:
            quant._log_fulfillment_event(is_create=False)

        return result

    def _log_fulfillment_event(self, is_create=False):
        """Логируем, если квант относится к fulfillment-складу и нет external ID."""
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
            action = "Создан" if is_create else "Обновлён"
            _logger.info(
                "[FULFILLMENT] %s квант без ID: product=%s, qty=%.2f, location=%s, warehouse=%s (%s)",
                action,
                self.product_id.display_name,
                self.quantity,
                location.complete_name,
                warehouse.name,
                warehouse.fulfillment_warehouse_id,
            )
