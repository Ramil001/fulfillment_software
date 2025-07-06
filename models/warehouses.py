import logging
import requests
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    # checkbox, является ли склад клиентским
    is_fulfillment = fields.Boolean(string="Is this for client storage?")
    # ID того кто создал склад.
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Fulfillment client ID")

    # ID конечного пользователя скалада.
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Fulfillment client ID")
    # Внуренний ID склада fulfillment software
    fulfillment_warehouse_id = fields.Char(string="Fulfillment Software Warehouse Id", readonly=True) 


    
    @api.model
    def write(self, vals):
        super().write(vals)
        for record in self:
            profile = self.env['fulfillment.profile'].search([], limit=1)
            if profile:
                _logger.info(f"[Fulfillment Profile]: {profile.name}")
            else:
                _logger.info("[Fulfillment Profile]: Not found")

            if record.fulfillment_client_id:
                _logger.info(f"[Fulfillment Warehouse Client ID]: {record.fulfillment_client_id.name}")
            else:
                _logger.info("[Fulfillment Warehouse Client ID]: No client")

            _logger.info(f"[Fulfillment Software][DEBGU]: {record.name} | {record.code} | {profile.fulfillment_api_key}")
            
            # Подготовка запроса
            payload = {
                'name': record.name,
                'code': record.code,
                "location": "UKR"
            }
            
            # Подготовка авторизации
            
            headers = {
                'Content-Type': 'application/json',
                'X-Fulfillment-API-Key': profile.fulfillment_api_key
            }
                        
            
            # Проверяем есть ли фулфиллмент ид и является ли запись.
            if record.is_fulfillment and not record.fulfillment_warehouse_id:
                url = f"https://{profile.domain}/api/v1/fulfillments/{self.fulfillment_client_id.fulfillment_id}/warehouses"
            
                try:
                    response = requests.post(url, json=payload, headers=headers, timeout=10)
                    if response.status_code == 200:
                        response_json = response.json()
                        data = response_json.get('data', {})
                        fulfillment_warehouse_id = data.get('id') 
                        vals['fulfillment_warehouse_id'] = fulfillment_warehouse_id
                    _logger.info(f"API response: | {url} | {response.status_code} | {response.text} | {response}")
                except requests.RequestException as e:
                    _logger.error(f"API call failed: {e}")
            #Еслі есть и чекбокс и номер склада
            elif record.is_fulfillment and record.fulfillment_warehouse_id:
                url = f"https://{profile.domain}/api/v1/fulfillments/{self.fulfillment_client_id.fulfillment_id}/warehouses/{self.fulfillment_warehouse_id}"
                try:
                    response = requests.patch(url, json=payload, headers=headers, timeout=10)
                    if response.status_code == 200:
                        response_json = response.json()
                        data = response_json.get('data', {})
                        fulfillment_warehouse_id = data.get('id') 
                        vals['fulfillment_warehouse_id'] = fulfillment_warehouse_id
                    _logger.info(f"API response: ::Patch:: | {url} | {response.status_code} | {response.text} | {response}")
                except requests.RequestException as e:
                    _logger.error(f"API call failed: {e}")


    # Метод синхронизации складов и обновления складов.
    @api.model
    def reload_warehouses(self):
        return True