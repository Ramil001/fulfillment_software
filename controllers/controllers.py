import json
from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class SimpleNotifyController(http.Controller):

    @http.route('/fulfillment/notify', type='json', auth='public', methods=['POST'], csrf=False)
    def simple_notify(self, **kwargs):
        data = request.get_json_data() or {}

        message_text = data.get("message", "Сообщение по умолчанию")
        title = data.get("title", "Fulfillment API")
        level = data.get("level", "info")
        sticky = data.get("sticky", False)

        payload = {
            "type": "fulfillment_notification",
            "payload": {
                "message": message_text,
                "title": title,
                "level": level,
                "sticky": sticky,
            }
        }

        _logger.info("📤 Отправляемый payload: %s", json.dumps(payload, ensure_ascii=False))

        users = request.env["res.users"].sudo().search([])
        for user in users:
            partner = user.partner_id
            if not partner:
                continue
            try:
                request.env["bus.bus"]._sendone(
                    partner, 
                    "fulfillment_notification", 
                    payload
                )
            except Exception as e:
                _logger.error("Error: %s", e)

        return {"status": "ok", "sent": message_text}