import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)


class FulfillmentOrder(models.Model):
    _inherit = 'sale.order'

    fulfillment_order_id = fields.Char(
        string="Fulfillment Order ID",
        help="External order identifier in fulfillment system",
        readonly=True,
        index=True
    )

    def _prepare_fulfillment_payload(self):
        """Формирование данных для API"""
        items = []
        for line in self.order_line:
            if not line.product_id:
                continue
            items.append({
                "product_id": line.product_id.fulfillment_product_id
                    or line.product_id.default_code
                    or str(line.product_id.id),
                "quantity": int(line.product_uom_qty),
                "fulfillment_partner_id": (
                    line.fulfillment_item_manager.external_id
                    if line.fulfillment_item_manager and line.fulfillment_item_manager.external_id
                    else None
                )
            })

        payload = {
            "order_ref": self.name,
            "customer_name": self.partner_id.name,
            "customer_email": self.partner_id.email,
            "items": items,
        }
        return payload

    # ──────────────────────────────
    #  Создание заказа
    # ──────────────────────────────
    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)

        for order in orders:
            try:
                has_fulfillment = any(
                    l.fulfillment_item_manager for l in order.order_line
                )
                if not has_fulfillment:
                    continue

                profile = order.env['fulfillment.profile'].search([], limit=1)
                if not profile:
                    _logger.warning("[FULFILLMENT][ORDER] Профиль не найден — пропуск")
                    continue

                client = FulfillmentAPIClient(profile)
                payload = order._prepare_fulfillment_payload()

                # создаём заказ во внешней системе
                response = client.order.create(payload)
                _logger.info("[FULFILLMENT][ORDER CREATE] Response → %s", response)

                if response and response.get("status") == "success":
                    data = response.get("data", {})
                    order.fulfillment_order_id = data.get("order_id")
                    _logger.info("[FULFILLMENT][ORDER CREATE] Создан во внешней системе → %s",
                                 order.fulfillment_order_id)
                else:
                    _logger.warning("[FULFILLMENT][ORDER CREATE] Ошибка: %s", response)

            except Exception as e:
                _logger.exception("[FULFILLMENT][ORDER CREATE] Ошибка при создании ордера: %s", e)

        return orders

    # ──────────────────────────────
    #  Обновление заказа
    # ──────────────────────────────
    def write(self, vals):
        res = super().write(vals)

        for order in self:
            if not order.fulfillment_order_id:
                continue  # обновляем только если уже есть ID

            try:
                profile = order.env['fulfillment.profile'].search([], limit=1)
                if not profile:
                    continue

                client = FulfillmentAPIClient(profile)
                payload = order._prepare_fulfillment_payload()

                response = client.order.update(order.fulfillment_order_id, payload)
                _logger.info("[FULFILLMENT][ORDER UPDATE] PATCH → %s", response)

                if not response or response.get("status") != "success":
                    _logger.warning("[FULFILLMENT][ORDER UPDATE] Ошибка: %s", response)

            except Exception as e:
                _logger.exception("[FULFILLMENT][ORDER UPDATE] Исключение: %s", e)

        return res

    # ──────────────────────────────
    #  Удаление заказа
    # ──────────────────────────────
    def unlink(self):
        for order in self:
            if not order.fulfillment_order_id:
                continue

            try:
                profile = order.env['fulfillment.profile'].search([], limit=1)
                client = FulfillmentAPIClient(profile)
                _logger.info("[FULFILLMENT][ORDER DELETE] DELETE /orders/%s",
                             order.fulfillment_order_id)
                response = client.order.delete(order.fulfillment_order_id)
                _logger.info("[FULFILLMENT][ORDER DELETE] Response → %s", response)

            except Exception as e:
                _logger.exception("[FULFILLMENT][ORDER DELETE] Ошибка удаления: %s", e)

        return super().unlink()
