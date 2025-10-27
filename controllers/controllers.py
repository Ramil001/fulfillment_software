from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class SimpleNotifyController(http.Controller):

    @http.route('/simple/notify', type='json', auth='public', methods=['POST'], csrf=False)
    def simple_notify(self, **kwargs):
        """Отправка уведомления всем пользователям Odoo через bus.bus"""
        try:
            data = request.get_json_data() or {}
            _logger.info("📨 Получен запрос на уведомление: %s", data)

            message_text = data.get("message", "Сообщение по умолчанию")
            title = data.get("title", "Fulfillment API")
            level = data.get("level", "info")
            sticky = data.get("sticky", False)

            # Формируем payload в соответствии с ожиданиями JS
            payload = {
                "type": "fulfillment_notification",
                "payload": {
                    "message": message_text,
                    "title": title,
                    "level": level,
                    "sticky": sticky,
                }
            }

            _logger.info("🔔 Отправка уведомления: %s", message_text)

            # Отправляем каждому партнеру, связанному с пользователем
            users = request.env["res.users"].sudo().search([])
            sent_count = 0
            
            for user in users:
                partner = user.partner_id
                if not partner:
                    continue
                try:
                    # Отправляем именно тот payload, который ожидает JS
                    request.env["bus.bus"]._sendone(
                        partner, 
                        "fulfillment_notification",  # канал
                        payload                      # сообщение в правильном формате
                    )
                    sent_count += 1
                    _logger.debug("✅ Уведомление отправлено партнеру %s (%s)", partner.id, user.login)
                except Exception as e:
                    _logger.error("❌ Ошибка отправки уведомления пользователю %s: %s", user.login, e)

            _logger.info("🎯 Всего отправлено уведомлений: %d пользователям", sent_count)
            return {"status": "ok", "sent": message_text, "users_count": sent_count}

        except Exception as e:
            _logger.error("❌ Критическая ошибка в контроллере: %s", str(e))
            return {"status": "error", "message": str(e)}