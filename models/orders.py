from odoo import models, fields, api, tools
import logging
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError


_logger = logging.getLogger(__name__)



class FulfillmentOrder(models.Model):
    _inherit = 'sale.order'

    fulfillment_order_id = fields.Char(
        string="Fulfillment Order ID",
        readonly=True,
        copy=False,
        index=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Создание заказа и синхронизация с Fulfillment API"""
        _logger.info(f"[DEBUG][ORDER][CREATE]: {vals_list}")

        records = super(FulfillmentOrder, self).create(vals_list)

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT] Профиль интеграции не найден, пропускаем синхронизацию.")
            return records

        client = FulfillmentAPIClient(profile)

        for order in records:
            try:
                payload = {
                    "order_name": order.name,
                    "order_id": order.id,
                    "items": [
                        {
                            "product_id": (
                                line.product_id.fulfillment_product_id
                                or line.product_id.default_code
                                or str(line.product_id.id)
                            ),
                            "quantity": int(line.product_uom_qty),
                            "fulfillment_partner_id": (
                                line.fulfillment_item_manager.fulfillment_id
                                if line.fulfillment_item_manager and line.fulfillment_item_manager.exists()
                                else None
                            ),
                        }
                        for line in order.order_line
                    ],
                    "total": float(order.amount_total),
                    "currency": order.currency_id.name or "UAH",
                }


                _logger.info(f"[FULFILLMENT][SYNC] Payload для API: {payload}")

                response = client.order.create(payload)
                _logger.info(f"[FULFILLMENT][SYNC] Ответ API: {response}")

                order.fulfillment_order_id = response.get("order_id") or response.get("id")

            except FulfillmentAPIError as e:
                _logger.error(f"[FULFILLMENT][ERROR] Ошибка синхронизации заказа {order.name}: {e}")
            except Exception as e:
                _logger.exception(f"[FULFILLMENT][UNEXPECTED] Ошибка при отправке заказа {order.name}: {e}")

        return records

            

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fulfillment_item_manager = fields.Many2one(
        'fulfillment.partners',
        string='Fulfillment Delivery',
        help='Кто отправляет этот товар',
    )
    fulfillment_line_id = fields.Char(
        string="Fulfillment Line ID",
        readonly=True,
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Перед созданием — проверяем корректность fulfillment_item_manager"""
        for vals in vals_list:
            if vals.get("fulfillment_item_manager"):
                partner_exists = self.env['fulfillment.partners'].browse(
                    vals["fulfillment_item_manager"]
                ).exists()
                if not partner_exists:
                    _logger.warning(
                        f"[FULFILLMENT][CLEANUP] Партнёр {vals['fulfillment_item_manager']} не найден, поле очищено"
                    )
                    vals["fulfillment_item_manager"] = False
        return super().create(vals_list)

    def write(self, vals):
        """Перед обновлением — проверяем корректность fulfillment_item_manager"""
        if vals.get("fulfillment_item_manager"):
            partner_exists = self.env['fulfillment.partners'].browse(
                vals["fulfillment_item_manager"]
            ).exists()
            if not partner_exists:
                _logger.warning(
                    f"[FULFILLMENT][CLEANUP] Некорректный партнёр {vals['fulfillment_item_manager']} — очищаем"
                )
                vals["fulfillment_item_manager"] = False
        return super().write(vals)

    @api.model
    def _auto_init(self):
        """Исправление битых связей при установке/обновлении модуля"""
        res = super()._auto_init()

        # Очистим все несуществующие ссылки
        query = """
        UPDATE sale_order_line
        SET fulfillment_item_manager = NULL
        WHERE fulfillment_item_manager IS NOT NULL
          AND fulfillment_item_manager NOT IN (SELECT id FROM fulfillment_partners)
        """
        self.env.cr.execute(query)
        _logger.info("[FULFILLMENT][CLEANUP] Все битые ссылки fulfillment_item_manager очищены")

        return res