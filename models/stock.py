from odoo import models, fields, api
import logging

# правильные импорты
from ..lib.api_client import FulfillmentAPIClient
from ..lib.api_client.stock import StockAPI

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
        for vals, quant in zip(vals_list, quants):
            quant._log_fulfillment_event(is_create=True)

            # Берём количество, которое было указано при создании
            init_qty = vals.get('quantity', 0.0)
            reserved_qty = vals.get('reserved_quantity', 0.0)

            quant._sync_fulfillment_stock_create(
                input_quantity=init_qty,
                input_reserved=reserved_qty
            )
        return quants

    def _update_available_quantity(self, product, location, quantity=None, **kwargs):
        """Переопределяем системное обновление остатков."""
        result = super()._update_available_quantity(product, location, quantity=quantity, **kwargs)

        quant = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', '=', location.id)
        ], limit=1)

        if quant:
            quant._log_fulfillment_event(is_create=False)
            quant._sync_fulfillment_stock_update()

        return result

    def _log_fulfillment_event(self, is_create=False):
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

    def _sync_fulfillment_stock_create(self, input_quantity=None, input_reserved=None):
        """Отправка созданного кванта во внешний Fulfillment API."""
        self.ensure_one()

        if self.fulfillment_stock_id:
            return

        warehouse = self.env['stock.warehouse'].search([
            '|',
            ('lot_stock_id', '=', self.location_id.id),
            ('view_location_id', 'parent_of', self.location_id.id)
        ], limit=1)

        if not warehouse or not warehouse.fulfillment_warehouse_id:
            _logger.debug("[FULFILLMENT] Пропущен — нет fulfillment_warehouse_id")
            return

        try:
            profile = self.env['fulfillment.profile'].search([], limit=1)
            client = FulfillmentAPIClient(profile)

            qty = input_quantity if input_quantity is not None else (self.quantity or 0.0)
            reserved = input_reserved if input_reserved is not None else (self.reserved_quantity or 0.0)

            payload = {
                "product_id": str(self.product_id.fulfillment_product_id or self.product_id.id),
                "warehouse_id": warehouse.fulfillment_warehouse_id,
                "location_id": str(self.location_id.fulfillment_location_id or self.location_id.id),
                "quantity": float(qty),
                "reserved": float(reserved),
            }

            _logger.info("[FULFILLMENT][STOCK CREATE] Payload → %s", payload)

            # Вызываем create через клиент (а не напрямую StockAPI)
            response = client.stock.create(payload)
            _logger.info("[FULFILLMENT][STOCK CREATE] Response → %s", response)

            if response and response.get("status") == "success":
                data = response.get("data", {})

                # Исправлено: теперь берём stock_id, а не числовой id
                self.fulfillment_stock_id = data.get("stock_id")

                _logger.info(
                    "[FULFILLMENT] Сток успешно создан во внешней системе: id=%s",
                    self.fulfillment_stock_id,
                )
            else:
                _logger.warning("[FULFILLMENT] Ошибка создания стока: %s", response)

        except Exception as e:
            _logger.exception("[FULFILLMENT] Ошибка при синхронизации стока: %s", e)

    def _sync_fulfillment_stock_update(self):
        """TODO: обновление стока в Fulfillment API"""
        _logger.info("[FULFILLMENT][STOCK UPDATE] TODO — обновление пока не реализовано")
