import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient
from datetime import datetime

_logger = logging.getLogger(__name__)

class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    is_fulfillment = fields.Boolean(string="Is this for client storage?")
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Fulfillment owner")
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Fulfillment client")
    fulfillment_warehouse_id = fields.Char(string="Fulfillment Software Warehouse Id", readonly=True)
    last_update = fields.Datetime(string='Last Update', readonly=True)

    @api.model
    def write(self, vals):
        for record in self:
            is_fulfillment = vals.get('is_fulfillment', record.is_fulfillment)
            if not is_fulfillment:
                continue

            # Пропускаем API, если контекст skip_api_sync
            if self.env.context.get('skip_api_sync'):
                _logger.info(f"[SKIP PATCH] Warehouse {record.id} updated locally without API sync")
                continue

            profile = self.env['fulfillment.profile'].search([], limit=1)
            if not profile:
                _logger.warning("[Fulfillment] Profile not found, API sync skipped")
                continue

            client = FulfillmentAPIClient(profile)

            payload = {
                'name': record.name,
                'code': record.code,
                'location': 'UKR',
            }

            fulfillment_id = record.fulfillment_client_id.fulfillment_id
            warehouse_id = record.fulfillment_warehouse_id

            try:
                # Проверяем, есть ли уже ID
                if warehouse_id:
                    # создаём контекст skip_api_sync, чтобы PATCH не триггерил write снова
                    with self.env.context({'skip_api_sync': True}):
                        response = client.warehouse.update(fulfillment_id, warehouse_id, payload)
                        new_id = response['data'].get('warehouse_id')
                        if new_id and new_id != warehouse_id:
                            record.with_context(skip_api_sync=True).write({
                                'fulfillment_warehouse_id': new_id
                            })
                else:
                    # Если нет warehouse_id — создаём склад в API
                    with self.env.context({'skip_api_sync': True}):
                        response = client.warehouse.create(fulfillment_id, payload)
                        record.with_context(skip_api_sync=True).write({
                            'fulfillment_warehouse_id': response['data'].get('warehouse_id')
                        })

            except Exception as e:
                _logger.warning(f"Warehouse update failed: {e}")

        vals['last_update'] = datetime.now()
        return super().write(vals)




    @api.model
    def create(self, vals):
        # Если контекст содержит skip_api_sync, пропускаем вызов API
        if self.env.context.get('skip_api_sync'):
            _logger.info(f"[SKIP CREATE] Warehouse {vals.get('name')} создан локально без API sync")
            vals['last_update'] = datetime.now()
            return super().create(vals)

        vals['last_update'] = datetime.now()
        warehouse = super().create(vals)

        if not warehouse.is_fulfillment:
            return warehouse

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found, API create пропущен")
            return warehouse

        client = FulfillmentAPIClient(profile)

        payload = {
            'name': warehouse.name,
            'code': warehouse.code,
            'location': 'UKR',
        }
        fulfillment_id = warehouse.fulfillment_client_id.fulfillment_id or profile.fulfillment_profile_id
        try:
            response = client.warehouse.create(fulfillment_id, payload)
            warehouse.fulfillment_warehouse_id = response['data'].get('warehouse_id')
        except Exception as e:
            _logger.error(f"[Fulfillment] Create failed: {e}")

        return warehouse


    @api.model
    def reload_warehouses(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.error("[Fulfillment] Profile not found")
            return False

        client = FulfillmentAPIClient(profile)

        try:
            response = client.warehouse.get()
            data = response.get('data', [])
            _logger.info(f"[Fulfillment][response = client.warehouse.get()]: {response}")
            if not data:
                _logger.info("[Fulfillment] No warehouses received from API")
                return True

            # страна по умолчанию (UA)
            default_country = self.env.ref('base.ua')

            # ищем ВСЕ склады (активные и архивные)
            existing = self.with_context(active_test=False).search([('is_fulfillment', '=', True)])
            existing_map = {w.fulfillment_warehouse_id: w for w in existing}
            processed_ids = set()

            for wh in data:
                ext_id = str(wh['warehouse_id'])
                warehouse = existing_map.get(ext_id)
                base_name = wh.get('name', 'Unnamed')
                code = wh.get('code', '')
                unique_name = base_name
                suffix = 1

                # защита от дублей по имени
                while self.with_context(active_test=False).search_count([
                    ('name', '=', unique_name),
                    ('company_id', '=', self.env.company.id)
                ]):
                    unique_name = f"{base_name} [{code}-{suffix}]"
                    suffix += 1

                country = self.env['res.country'].search([('code', '=', wh.get('location', 'UA'))], limit=1) or default_country
                partner_vals = {
                    'name': unique_name,
                    'country_id': country.id,
                    'is_company': True,
                    'type': 'delivery'
                }

                vals = {
                    'name': unique_name,
                    'code': code,
                    'is_fulfillment': True,
                    'fulfillment_warehouse_id': ext_id,
                    'company_id': self.env.company.id,
                    'active': True,
                }

                if warehouse:
                    # обновляем существующий (даже если он архивный → станет активным)
                    if warehouse.partner_id:
                        warehouse.partner_id.with_context(skip_api_sync=True).write(partner_vals)
                    else:
                        vals['partner_id'] = self.env['res.partner'].create(partner_vals).id

                    warehouse.with_context(skip_api_sync=True).write(vals)
                    processed_ids.add(warehouse.id)

                else:
                    # пробуем найти архивный склад по имени
                    archived_wh = self.with_context(active_test=False).search([
                        ('name', '=', base_name),
                        ('company_id', '=', self.env.company.id)
                    ], limit=1)

                    if archived_wh:
                        _logger.info(f"[Fulfillment] Found archived warehouse {base_name}, reactivating it")
                        archived_wh.with_context(skip_api_sync=True).write(vals)
                        processed_ids.add(archived_wh.id)
                        continue

                    # создаём новый склад
                    vals['partner_id'] = self.env['res.partner'].create(partner_vals).id
                    try:
                        new_warehouse = self.with_context(skip_api_sync=True).create(vals)
                        processed_ids.add(new_warehouse.id)
                        _logger.info(f"[Fulfillment] Created warehouse: {new_warehouse.name}")
                    except Exception as e:
                        _logger.error(f"[Fulfillment] Failed to create: {e}")
                        self.env.cr.rollback()

            # деактивируем те, которых нет в API
            to_deactivate = existing.filtered(lambda w: w.id not in processed_ids)
            to_deactivate.write({'active': False})
            _logger.info(f"[Fulfillment] Deactivated {len(to_deactivate)} obsolete warehouses")

        except Exception as e:
            _logger.error(f"[Fulfillment] Sync error: {e}")
            return False

        return True


    def get_fulfillment_info(self):
        """
        Возвращает (warehouse_api_id, fulfillment_id) для склада.
        Если не найден fulfillment_id → пишет в лог ошибку.
        """
        self.ensure_one()

        warehouse_api_id = self.fulfillment_warehouse_id


        if not warehouse_api_id:
            _logger.error(f"[Fulfillment] Склад {self.name} (ID={self.id}) не имеет fulfillment_warehouse_id")


        return warehouse_api_id
