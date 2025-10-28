import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

class FulfillmentNotifier:
    """Универсальный класс для отправки уведомлений в Odoo."""

    def __init__(self, env):
        # Если пришёл объект модели — берём env от неё
        if hasattr(env, "env"):
            env = env.env
        # Безопасно применяем sudo
        try:
            self.env = env.sudo()
        except Exception:
            self.env = api.Environment(env.cr, SUPERUSER_ID, env.context)

    def send(self, message, title="Fulfillment", level="info", sticky=False, buttons=None):
        payload = {
            "type": "fulfillment_notification",
            "payload": {
                "message": message,
                "title": title,
                "level": level,
                "sticky": sticky,
                "buttons": buttons or []
            }
        }

        users = self.env["res.users"].sudo().search([])
        for user in users:
            partner = user.partner_id
            if not partner:
                continue
            try:
                self.env["bus.bus"]._sendone(partner, "fulfillment_notification", payload)
            except Exception as e:
                _logger.error("Ошибка при отправке уведомления пользователю %s: %s", user.name, e)
