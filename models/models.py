from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)

class FulfillmentDashboard(models.Model):
    _name = 'fulfillment.dashboard'
    _description = 'Fulfillment Dashboard'

    name = fields.Char(string="Name")
    subscriptions = fields.Text(string="Подписки")

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        headers = {
            "X-Fulfillment-API-Key": "e2vlLo1LM6zFBOnv95jCyZ0jlIib04acYLLL1rXmhlQ"
        }
        try:
            response = requests.get(
                'https://api.fulfillment.software/api/v1/fulfillments',
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json().get("data", [])
            subscriptions = []
            for item in data:
                subscriptions.append(f"{item['name']} ({item['domain']}) - {item['createdAt']}")
            res['subscriptions'] = '\n'.join(subscriptions) if subscriptions else 'Подписок нет'
        except Exception as e:
            _logger.error(f"Ошибка при загрузке подписок: {e}")
            res['subscriptions'] = f"Ошибка загрузки: {e}"
        return res
