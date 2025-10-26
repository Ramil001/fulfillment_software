from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class SimpleNotifyController(http.Controller):

    @http.route('/simple/notify', type='json', auth='public', methods=['POST'], csrf=False)
    def simple_notify(self, **kwargs):
        """Отправка уведомления всем пользователям Odoo через bus.bus"""
        data = request.get_json_data() or {}

        message_text = data.get("message", "Сообщение по умолчанию")
        title = data.get("title", "Fulfillment API")
        level = data.get("level", "info")
        sticky = data.get("sticky", False)

        payload = {
            "payload": {
                "type": "fulfillment_notification",  # чтобы JS понял
                "message": message_text,
                "title": title,
                "level": level,
                "sticky": sticky,
            }
        }

        users = request.env["res.users"].sudo().search([])
        for user in users:
            try:
                request.env["bus.bus"]._sendone(
                    "fulfillment_notification",  # канал
                    payload,                     # сообщение
                    user.id                       # кому
                )
                _logger.info("🔔 Уведомление отправлено пользователю %s: %s", user.id, payload)
            except Exception as e:
                _logger.error("❌ Ошибка отправки уведомления пользователю %s: %s", user.id, e)

        return {"status": "ok", "sent": message_text}
