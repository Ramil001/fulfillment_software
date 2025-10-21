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
        return quants


    def _update_available_quantity(self, product, location, quantity=None, **kwargs):
        """Переопределяем системное обновление остатков."""
        result = super()._update_available_quantity(product, location, quantity=quantity, **kwargs)

        quant = self.env['stock.quant'].search([
            ('product_id', '=', product.id),
            ('location_id', '=', location.id)
        ], limit=1)

        if not quant:
            return result

        real_qty = quant.quantity or 0.0
        reserved_qty = quant.reserved_quantity or 0.0

        quant._log_fulfillment_event(is_create=not bool(quant.fulfillment_stock_id))

        if not quant.fulfillment_stock_id:
            _logger.info("[FULFILLMENT][STOCK] Нет ID → создаём через API (qty=%.2f)", real_qty)
            quant._sync_fulfillment_stock_create(
                input_quantity=real_qty,
                input_reserved=reserved_qty,
            )
        else:
            _logger.info("[FULFILLMENT][STOCK] Есть ID → обновляем через API (qty=%.2f)", real_qty)
            quant._sync_fulfillment_stock_update()

        return result


    def import_stock(self, filters=None):
        """Импорт остатков из Fulfillment API и обновление stock.quant в Odoo."""

        _logger.info("[FULFILLMENT][IMPORT] Запуск импорта остатков (filters=%s)", filters)

        try:
            # 1️⃣ Получаем профиль и инициализируем клиента
            profile = self.env['fulfillment.profile'].search([], limit=1)
            if not profile:
                _logger.error("[FULFILLMENT][IMPORT] Не найден профиль fulfillment.profile")
                return False

            client = FulfillmentAPIClient(profile)

            # 2️⃣ Формируем payload для API
            payload = {
                "filters": filters or {},
                "group_by": ["warehouse_id", "product_id", "location_id"],
                "include_reserved": False,
            }

            _logger.info("[FULFILLMENT][IMPORT] Payload → %s", payload)

            # 3️⃣ Запрос к API
            response = client.stock.get(payload)
            _logger.info("[FULFILLMENT][IMPORT] Response → %s", response)

            if not response or response.get("status") != "success":
                _logger.warning("[FULFILLMENT][IMPORT] Ошибка: %s", response)
                return False

            data_list = response.get("data", [])
            _logger.info("[FULFILLMENT][IMPORT] Получено записей: %d", len(data_list))

            # 4️⃣ Обработка записей
            for item in data_list:
                product_ext_id = item.get("product_id")
                warehouse_ext_id = item.get("warehouse_id")
                location_ext_id = item.get("location_id")
                qty = float(item.get("_sum", {}).get("quantity", 0.0))
                available = float(item.get("_sum", {}).get("available", 0.0))

                # 5️⃣ Поиск соответствующих записей в Odoo
                product = self.env['product.product'].search([
                    ('fulfillment_product_id', '=', product_ext_id)
                ], limit=1)


                location = self.env['stock.location'].search([
                    ('fulfillment_location_id', '=', location_ext_id)
                ], limit=1)


                if not product or not location:
                    _logger.warning(
                        "[FULFILLMENT][IMPORT] Пропуск: нет product/location (%s / %s)",
                        product_ext_id, location_ext_id
                    )
                    continue

                # 6️⃣ Ищем существующий квант
                quant = self.search([
                    ('product_id', '=', product.id),
                    ('location_id', '=', location.id)
                ], limit=1)

                # 7️⃣ Обновление или создание кванта
                if quant:
                    quant.write({
                        'quantity': qty,
                        'reserved_quantity': qty - available if qty > available else 0.0,
                    })
                    _logger.info(
                        "[FULFILLMENT][IMPORT] Обновлён квант: %s (%s) qty=%.2f",
                        product.display_name, location.display_name, qty
                    )
                else:
                    new_quant = self.create({
                        'product_id': product.id,
                        'location_id': location.id,
                        'quantity': qty,
                        'reserved_quantity': qty - available if qty > available else 0.0,
                        'fulfillment_stock_id': None,  # при импорте не создаём ID
                    })
                    _logger.info(
                        "[FULFILLMENT][IMPORT] Создан новый квант: %s (%s) qty=%.2f",
                        new_quant.product_id.display_name,
                        new_quant.location_id.display_name,
                        qty
                    )

            _logger.info("[FULFILLMENT][IMPORT] Импорт завершён успешно")
            return True

        except Exception as e:
            _logger.exception("[FULFILLMENT][IMPORT] Ошибка при импорте остатков: %s", e)
            return False


    def _sync_fulfillment_stock_update(self):
        """Обновление стока в Fulfillment API"""
        self.ensure_one()

        if not self.fulfillment_stock_id:
            _logger.warning("[FULFILLMENT][STOCK UPDATE] Нет fulfillment_stock_id — пропуск")
            return

        try:
            profile = self.env['fulfillment.profile'].search([], limit=1)
            client = FulfillmentAPIClient(profile)

            payload = {
                "quantity": float(self.quantity or 0.0),
                "reserved": float(self.reserved_quantity or 0.0),
            }

            # Если количество и резерв равны нулю — вместо обновления делаем удаление
            if payload["quantity"] == 0 and payload["reserved"] == 0:
                _logger.info(
                    "[FULFILLMENT][STOCK UPDATE] Кол-во = 0 → выполняем DELETE для %s",
                    self.fulfillment_stock_id
                )
                return self._sync_fulfillment_stock_delete()

            _logger.info(
                "[FULFILLMENT][STOCK UPDATE] PUT /stock/%s → %s",
                self.fulfillment_stock_id, payload
            )

            response = client.stock.update(self.fulfillment_stock_id, payload)
            _logger.info("[FULFILLMENT][STOCK UPDATE] Response → %s", response)

            if not response or response.get("status") != "success":
                _logger.warning("[FULFILLMENT][STOCK UPDATE] Ошибка при обновлении стока: %s", response)

        except Exception as e:
            _logger.exception("[FULFILLMENT][STOCK UPDATE] Исключение: %s", e)


    def _sync_fulfillment_stock_delete(self):
        """Удаление стока в Fulfillment API"""
        self.ensure_one()

        if not self.fulfillment_stock_id:
            _logger.info("[FULFILLMENT][STOCK DELETE] Нет ID — пропуск")
            return

        try:
            profile = self.env['fulfillment.profile'].search([], limit=1)
            client = FulfillmentAPIClient(profile)

            _logger.info(
                "[FULFILLMENT][STOCK DELETE] DELETE /stock/%s",
                self.fulfillment_stock_id
            )

            response = client.stock.delete(self.fulfillment_stock_id)
            _logger.info("[FULFILLMENT][STOCK DELETE] Response → %s", response)

            if response and response.get("status") == "success":
                self.fulfillment_stock_id = False
                _logger.info("[FULFILLMENT][STOCK DELETE] Успешно удалено из внешней системы")
            else:
                _logger.warning("[FULFILLMENT][STOCK DELETE] Ошибка удаления: %s", response)

        except Exception as e:
            _logger.exception("[FULFILLMENT][STOCK DELETE] Ошибка при удалении: %s", e)


    def unlink(self):
        """Удаление стока из внешнего Fulfillment API при удалении в Odoo"""
        for quant in self:
            try:
                if quant.fulfillment_stock_id:
                    _logger.info("[FULFILLMENT][UNLINK] Удаляем внешний сток перед удалением %s", quant.id)
                    quant._sync_fulfillment_stock_delete()
            except Exception as e:
                _logger.warning("[FULFILLMENT][UNLINK] Ошибка при удалении: %s", e)

        return super().unlink()


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
                "product_id": str(self.product_id.fulfillment_product_id),
                "warehouse_id": warehouse.fulfillment_warehouse_id,
                "location_id": str(self.location_id.fulfillment_location_id),
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

