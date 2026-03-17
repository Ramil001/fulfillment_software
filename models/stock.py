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
        _logger.info(f"[create]")
        quants = super().create(vals_list)
        for vals, quant in zip(vals_list, quants):
            quant._log_fulfillment_event(is_create=True)
        return quants


    def _update_available_quantity(self, product, location, quantity=None, **kwargs):
        _logger.info(f"[_update_available_quantity]")
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
        _logger.info(f"[import_stock]")

        try:
            profile = self.env['fulfillment.profile'].search([], limit=1)
            if not profile:
                _logger.error("[FULFILLMENT][IMPORT] Не найден профиль fulfillment.profile")
                return False

            client = FulfillmentAPIClient(profile)

            payload = {
                "filters": filters or {},
                "group_by": ["warehouse_id", "product_id", "location_id"],
                "include_reserved": False,
            }

            _logger.info("[FULFILLMENT][IMPORT] Payload → %s", payload)

            response = client.stock.get(payload)
            _logger.info("[FULFILLMENT][IMPORT] Response → %s", response)

            if not response or response.get("status") != "success":
                _logger.warning("[FULFILLMENT][IMPORT] Ошибка: %s", response)
                return False

            data_list = response.get("data", [])
            _logger.info("[FULFILLMENT][IMPORT] Получено записей: %d", len(data_list))
            
            self._sync_fulfillment_locations(data_list)

            for item in data_list:
                product_ext_id = item.get("product_id")
                warehouse_ext_id = item.get("warehouse_id")
                location_ext_id = item.get("location_id")
                qty = float(item.get("_sum", {}).get("quantity", 0.0))
                available = float(item.get("_sum", {}).get("available", 0.0))

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

                quant = self.search([
                    ('product_id', '=', product.id),
                    ('location_id', '=', location.id)
                ], limit=1)

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
                        'fulfillment_stock_id': None, 
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


    def _sync_fulfillment_locations(self, data_list):
        _logger.info(f"[_sync_fulfillment_locations]")
        
        for item in data_list:
            warehouse_ext_id = item.get("warehouse_id")
            warehouse_name = item.get("warehouse_name") or "Без имени"
            location_ext_id = item.get("location_id")
            location_name = item.get("location_name") or "Stock"

            # Склад
            warehouse = self.env['stock.warehouse'].search([
                ('fulfillment_warehouse_id', '=', warehouse_ext_id)
            ], limit=1)
            if not warehouse:
                stock_location = self.env['stock.location'].create({'name': 'Stock'})
                warehouse = self.env['stock.warehouse'].create({
                    'name': warehouse_name,
                    'code': warehouse_name,
                    'lot_stock_id': stock_location.id,
                    'fulfillment_warehouse_id': warehouse_ext_id
                })
                _logger.info(f"[SYNC] Создан склад: {warehouse.name} ({warehouse_ext_id})")

            # Локация
            location = self.env['stock.location'].search([
                ('fulfillment_location_id', '=', location_ext_id)
            ], limit=1)
            if not location:
                location = self.env['stock.location'].create({
                    'name': location_name,
                    'location_id': warehouse.lot_stock_id.id,
                    'fulfillment_location_id': location_ext_id
                })
                _logger.info(f"[SYNC] Создана локация: {location.name} ({location_ext_id})")
        

    def _sync_fulfillment_stock_update(self):
        _logger.info(f"[_sync_fulfillment_stock_update]")
        
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
        _logger.info(f"[_sync_fulfillment_stock_delete]")
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
        _logger.info(f"[unlink]")
        for quant in self:
            try:
                if quant.fulfillment_stock_id:
                    _logger.info("[FULFILLMENT][UNLINK] Удаляем внешний сток перед удалением %s", quant.id)
                    quant._sync_fulfillment_stock_delete()
            except Exception as e:
                _logger.warning("[FULFILLMENT][UNLINK] Ошибка при удалении: %s", e)

        return super().unlink()


    def _log_fulfillment_event(self, is_create=False):
        _logger.info(f"[_log_fulfillment_event]")
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
            

    def _sync_fulfillment_stock_create(self, input_quantity=None, input_reserved=None):
        _logger.info(f"[_sync_fulfillment_stock_create]")
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

            response = client.stock.create(payload)
            _logger.info("[FULFILLMENT][STOCK CREATE] Response → %s", response)

            if response and response.get("status") == "success":
                data = response.get("data", {})

                self.fulfillment_stock_id = data.get("stock_id")

                _logger.info(
                    "[FULFILLMENT] Сток успешно создан во внешней системе: id=%s",
                    self.fulfillment_stock_id,
                )
            else:
                _logger.warning("[FULFILLMENT] Ошибка создания стока: %s", response)

        except Exception as e:
            _logger.exception("[FULFILLMENT] Ошибка при синхронизации стока: %s", e)






class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    def name_get(self):
        result = super().name_get()
        new_result = []

        product_id = self.env.context.get('product_id')
        product = None

        if product_id:
            product = self.env['product.product'].browse(product_id)

        for rec_id, name in result:
            rec = self.browse(rec_id)

            if product:
                qty = product.with_context(
                    location=rec.lot_stock_id.id
                ).qty_available

                name = f"{name} ({int(qty)}"

            new_result.append((rec_id, name))

        return new_result


    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):

        result = super().name_search(
            name=name,
            args=args,
            operator=operator,
            limit=limit,
        )

        new_result = []

        product_id = self.env.context.get('product_id')
        product = None

        if product_id:
            product = self.env['product.product'].browse(product_id)

        for rec_id, display_name in result:
            rec = self.browse(rec_id)

            if product:
                qty = product.with_context(
                    location=rec.lot_stock_id.id
                ).qty_available

                display_name = f"{display_name} ({int(qty)})"

            new_result.append((rec_id, display_name))

        return new_result