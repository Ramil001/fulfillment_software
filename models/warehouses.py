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


    # [Update] Обновляет запись.
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
                        
            
            #Если есть и чекбокс и номер склада
            url = f"https://{profile.domain}/api/v1/fulfillments/{self.fulfillment_client_id.fulfillment_id}/warehouses/{self.fulfillment_warehouse_id}"
            try:
                response = requests.patch(url, json=payload, headers=headers, timeout=10)
                if response.status_code == 200:
                    response_json = response.json()
                    data = response_json.get('data', {})
                    fulfillment_warehouse_id = data.get('warehouse_id') 
                    vals['fulfillment_warehouse_id'] = fulfillment_warehouse_id
                _logger.info(f"API response: ::Patch:: | {url} | {response.status_code} | {response.text} | {response}")
            except requests.RequestException as e:
                _logger.error(f"API call failed: {e}")
                

    # [CREATE] Создания новой записи.
    @api.model
    def create(self, vals):
        # 1. Создаём склад стандартным способом
        warehouse = super().create(vals)

        # 2. Берём профиль фулфилмента
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found on create")
            return warehouse

        # 3. Если склад - fulfillment
        if warehouse.is_fulfillment:
            payload = {
                'name': warehouse.name,
                'code': warehouse.code,
                "location": "UKR"
            }
            headers = {
                'Content-Type': 'application/json',
                'X-Fulfillment-API-Key': profile.fulfillment_api_key
            }

            if warehouse.fulfillment_client_id:
                fulfillment_id = warehouse.fulfillment_client_id.fulfillment_id
            else:
                fulfillment_id = profile.fulfillment_profile_id

            url = f"https://{profile.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses"

            try:
                response = requests.post(url, json=payload, headers=headers, timeout=10)
                response.raise_for_status()
                response_json = response.json()
                data = response_json.get('data', {})
                fulfillment_warehouse_id = data.get('warehouse_id')
                if fulfillment_warehouse_id:
                    warehouse.fulfillment_warehouse_id = fulfillment_warehouse_id
                _logger.info(f"[Fulfillment][Create] Created warehouse via API: {warehouse.name} | {response.status_code} | {response.text}")
            except requests.RequestException as e:
                _logger.error(f"[Fulfillment][Create] API call failed: {e}")

        return warehouse
          
            
    # Метод синхронизации складов и обновления складов.
    @api.model
    def reload_warehouses(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.error("[Fulfillment] Profile not found")
            return False
            
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

            # Страна по умолчанию - Украина
            default_country = self.env.ref('base.ua')
            existing_warehouses = self.search([('is_fulfillment', '=', True)])
            external_id_map = {w.fulfillment_warehouse_id: w for w in existing_warehouses}
            processed_ids = set()

            for wh_data in warehouses_data:
                external_id = str(wh_data.get('warehouse_id'))
                warehouse = external_id_map.get(external_id)
                base_name = wh_data.get('name', 'Unknown Warehouse')
                
                # Генерация уникального имени
                unique_name = base_name
                suffix = 1
                while self.search_count([('name', '=', unique_name), ('company_id', '=', self.env.company.id)]):
                    unique_name = f"{base_name} [{wh_data.get('code', '')}-{suffix}]"
                    suffix += 1
                
                # Определение страны
                country_code = wh_data.get('location', 'UA')
                country = self.env['res.country'].search([('code', '=', country_code)], limit=1) or default_country
                
                # Подготовка данных партнера
                partner_vals = {
                    'name': unique_name,
                    'country_id': country.id,
                    'is_company': True,
                }
                
                # Основные данные склада
                vals = {
                    'name': unique_name,
                    'code': wh_data.get('code', ''),
                    'is_fulfillment': True,
                    'fulfillment_warehouse_id': external_id,
                }
                
                # Работа с партнером
                if warehouse:
                    # Обновление существующего партнера
                    if warehouse.partner_id:
                        warehouse.partner_id.write(partner_vals)
                    else:
                        # Создание партнера если отсутствует
                        vals['partner_id'] = self.env['res.partner'].create(partner_vals).id
                        
                    warehouse.write(vals)
                    processed_ids.add(warehouse.id)
                else:
                    # Создание нового склада с партнером
                    partner_vals['type'] = 'delivery'  # Для лучшей идентификации
                    vals['partner_id'] = self.env['res.partner'].create(partner_vals).id
                    
                    try:
                        new_warehouse = self.create(vals)
                        processed_ids.add(new_warehouse.id)
                        _logger.info(f"[Fulfillment] Created warehouse: {new_warehouse.name}")
                    except Exception as e:
                        _logger.error(f"[Fulfillment] Failed to create warehouse: {str(e)}")
                        self.env.cr.rollback()
                        continue

            # Деактивация устаревших складов
            to_deactivate = existing_warehouses.filtered(lambda w: w.id not in processed_ids)
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