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
            #Если есть и чекбокс и номер склада
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
        profile = self.env['fulfillment.profile'].search([], limit=1)
        headers = {
            'Content-Type': 'application/json',
            'X-Fulfillment-API-Key': profile.fulfillment_api_key
        }
        
        url = f"https://{profile.domain}/api/v1/fulfillments/{profile.fulfillment_profile_id}/warehouses"
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            response_data = response.json()
            
            if response_data.get('status') != 'OK':
                _logger.error(f"[Fulfillment] API error: {response_data}")
                return False
                
            warehouses_data = response_data.get('data', [])
            if not warehouses_data:
                _logger.info("[Fulfillment] No warehouses received from API")
                return True

            # Решение для проблемы дублирования имен складов
            existing_names = {
                name: id for name, id in 
                self.search_read([('company_id', '=', self.env.company.id)], ['name'])
            }

            # Получаем все fulfillment-склады для сравнения
            existing_warehouses = self.search([('is_fulfillment', '=', True)])
            external_id_map = {w.fulfillment_warehouse_id: w for w in existing_warehouses}
            processed_warehouses = set()

            for wh_data in warehouses_data:
                external_id = str(wh_data.get('id'))
                warehouse = external_id_map.get(external_id)
                
                # Генерация уникального имени склада
                base_name = wh_data.get('name', 'Unknown Warehouse')
                unique_name = base_name
                suffix = 1
                
                # Проверка на уникальность имени в рамках компании
                while unique_name in existing_names and existing_names[unique_name] != (warehouse.id if warehouse else False):
                    unique_name = f"{base_name} [{wh_data.get('code', '')}-{suffix}]"
                    suffix += 1
                
                # Подготовка значений для создания/обновления
                vals = {
                    'name': unique_name,
                    'code': wh_data.get('code', ''),
                    'is_fulfillment': True,
                    'fulfillment_warehouse_id': external_id,
                }
                
                # Решение для проблемы внешнего ключа - добавляем только необходимые поля
                location_fields = ['location']
                for field in location_fields:
                    if field in wh_data:
                        vals[field] = wh_data[field]
                
                if warehouse:
                    warehouse.write(vals)
                    processed_warehouses.add(warehouse.id)
                else:
                    try:
                        new_warehouse = self.create(vals)
                        processed_warehouses.add(new_warehouse.id)
                        existing_names[unique_name] = new_warehouse.id
                        _logger.info(f"[Fulfillment] Created warehouse: {new_warehouse.name}")
                    except Exception as e:
                        _logger.error(f"[Fulfillment] Failed to create warehouse: {str(e)}")
                        # Откат изменений для этой конкретной записи
                        self.env.cr.rollback()
                        continue

            # Деактивировать склады, которые больше не существуют в API
            to_deactivate = existing_warehouses.filtered(lambda w: w.id not in processed_warehouses)
            if to_deactivate:
                to_deactivate.write({'active': False})
                _logger.info(f"[Fulfillment] Deactivated {len(to_deactivate)} obsolete warehouses")

        except requests.exceptions.RequestException as e:
            _logger.error(f"[Fulfillment] API connection error: {str(e)}")
            return False
        except Exception as e:
            _logger.error(f"[Fulfillment] Unexpected error: {str(e)}")
            return False

        _logger.info(f"[Fulfillment] Warehouse sync completed: {len(warehouses_data)} processed")
        return True