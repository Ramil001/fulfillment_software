# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class BusUtils(models.AbstractModel):
    _name = "bus.utils"
    _description = "Bus notification utilities"

    def send_notification(self, title, message, level="info", sticky=False, user_ids=None, to_current_user=False):
        """
        Отправляет уведомление через Odoo bus:
          - либо всем пользователям,
          - либо конкретным user_ids,
          - либо только текущему пользователю (если to_current_user=True).

        :param title: Заголовок уведомления
        :param message: Текст уведомления
        :param level: info | warning | error
        :param sticky: Закрепить уведомление
        :param user_ids: список ID пользователей (по умолчанию — всем)
        :param to_current_user: если True — отправляет только текущему пользователю
        """
        env = self.env
        try:
            bus = env["bus.bus"].sudo()
            users_model = env["res.users"].sudo()

            if to_current_user:
                users = users_model.browse([env.uid])
            elif user_ids:
                users = users_model.browse(user_ids)
            else:
                users = users_model.search([])

            payload = {
                "type": "fulfillment_notification",
                "payload": {
                    "title": title,
                    "message": message,
                    "level": level,
                    "sticky": sticky,
                },
            }

            for user in users:
                partner = user.partner_id
                if not partner:
                    continue
                try:
                    bus._sendone(partner, "fulfillment_notification", payload)
                    _logger.debug(f"[BUS] Notification sent to {user.name}: {title}")
                except Exception as e:
                    _logger.error("[BUS][ERROR] Не удалось отправить уведомление %s: %s", user.name, e)

        except Exception as e:
            _logger.exception("[BUS][FATAL] Ошибка при отправке уведомлений: %s", e)
