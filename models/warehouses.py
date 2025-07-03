import logging
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    # Чекбокс, является ли склад клиентским
    is_fulfillment = fields.Boolean(string="Is this for client storage?")
    # Id владельца склада
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Linked fulfillment partner")
    # Id клиента, который будет пользоваться складом.
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Linked fulfillment partner")
    # Id склада внутри API
    fulfillment_warehouse_id = fields.Char(string="Fulfillment Software Warehouse Id", readonly=True) 
    
    fulfillment_api_key = fields.Char(
        string="API Key",
        related='fulfillment_owner_id.profile_id.fulfillment_api_key',
        readonly=True
    )

    @api.model
    def create(self, vals):
        rec = super().create(vals)
        try:
            if not rec.fulfillment_warehouse_id and rec.is_fulfillment:
                api_key = rec.fulfillment_api_key
                fulfillment_id = rec.fulfillment_owner_id.fulfillment_id  # если поле есть

                url = f"https://api.fulfillment.software/api/v1/fulfillments/{fulfillment_id}/warehouses"
                headers = {
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                }
                payload = {
                    "name": rec.name,
                    "code": rec.code or "DEFAULT_CODE",
                    "location": rec.partner_id.country_id.code if rec.partner_id and rec.partner_id.country_id else "UKR"
                }

                response = requests.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json().get('data', {})
                    rec.fulfillment_warehouse_id = str(data.get('id'))
                    _logger.info(f"[Fulfillment] 🗂️ Внешний склад создан, ID: {rec.fulfillment_warehouse_id}")
                else:
                    _logger.error(f"[Fulfillment] ❌ Ошибка внешнего API при создании: {response.text}")

            return rec
        except Exception as e:
            _logger.error(f"[Fulfillment] ❌ Ошибка создания склада: {str(e)}")
            raise


    def write(self, vals):
        res = super().write(vals)
        try:
            for rec in self:
                if rec.fulfillment_warehouse_id and rec.is_fulfillment:
                    api_key = rec.fulfillment_api_key
                    fulfillment_id = rec.fulfillment_owner_id.fulfillment_id

                    url = f"https://api.fulfillment.software/api/v1/fulfillments/{fulfillment_id}/warehouses/{rec.fulfillment_warehouse_id}"
                    headers = {
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json'
                    }
                    payload = {
                        "name": rec.name,
                        "code": rec.code or "DEFAULT_CODE",
                        "location": rec.partner_id.country_id.code if rec.partner_id and rec.partner_id.country_id else "UKR"
                    }

                    response = requests.patch(url, json=payload, headers=headers)
                    if response.status_code == 200:
                        _logger.info(f"[Fulfillment] 🔄 Внешний склад обновлён (ID {rec.fulfillment_warehouse_id})")
                    else:
                        _logger.error(f"[Fulfillment] ❌ Ошибка обновления склада во внешнем API: {response.text}")

            return res
        except Exception as e:
            _logger.error(f"[Fulfillment] ❌ Ошибка обновления склада: {str(e)}")
            raise
