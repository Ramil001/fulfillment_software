# Warehouse 
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

    def _extract_partner_id(self, val):
        """Нормализовать partner_id из vals — handle int, (4, id, 0), (6,0,[id]) и т.п."""
        if not val:
            return False
        # уже int
        if isinstance(val, int):
            return val
        # tuple like (4, id, 0)
        if isinstance(val, (list, tuple)):
            # direct command (4, id, 0) or (6, 0, [ids])
            if len(val) == 3 and isinstance(val[0], int):
                cmd = int(val[0])
                if cmd == 4 and isinstance(val[1], int):
                    return int(val[1])
                if cmd == 6 and isinstance(val[2], list) and len(val[2]) == 1:
                    return int(val[2][0])
            # sometimes client send [(4, id, 0)]
            if len(val) and isinstance(val[0], (list, tuple)):
                inner = val[0]
                if len(inner) >= 2 and inner[0] == 4:
                    return int(inner[1])
        return False


    @api.model
    def _get_or_create_warehouse_contact(self, parent_partner, warehouse_name):
        """Return existing child contact parent->(warehouse_name) or create it."""
        if not parent_partner or not parent_partner.exists():
            return False

        child_name = f"{parent_partner.name} ({warehouse_name})"
        _logger.info(f"[Fulfillment] _get_or_create_warehouse_contact lookup: parent={parent_partner.id} name={child_name}")

        child = self.env['res.partner'].search([
            ('parent_id', '=', parent_partner.id),
            ('name', '=', child_name)
        ], limit=1)

        if child:
            _logger.info(f"[Fulfillment] Found existing child contact {child.id} for warehouse '{warehouse_name}'")
            return child

        vals = {
            'name': child_name,
            'parent_id': parent_partner.id,
            'type': 'delivery',
            'is_company': False,
        }
        # копируем страну у родителя, если есть
        if parent_partner.country_id:
            vals['country_id'] = parent_partner.country_id.id

        _logger.info(f"[Fulfillment] Creating child contact for warehouse: {vals}")
        # создаём в контексте skip_api_sync, чтобы не запускать сторонние обработчики
        child = self.env['res.partner'].with_context(skip_api_sync=True).create(vals)
        _logger.info(f"[Fulfillment] Child contact created: {child.id}")
        return child


    @api.model
    def create(self, vals):
        if vals.get('is_fulfillment') and not self.env.context.get('skip_warehouse_contact') and vals.get('partner_id'):
            parent_id = self._extract_partner_id(vals.get('partner_id'))
            if parent_id:
                parent = self.env['res.partner'].browse(parent_id)
                if parent.exists():
                    warehouse_name = vals.get('name') or 'Warehouse'
                    child = self._get_or_create_warehouse_contact(parent, warehouse_name)
                    if child:
                        vals['partner_id'] = child.id

        vals['last_update'] = datetime.now()
        warehouse = super().create(vals)

        if not warehouse.is_fulfillment:
            return warehouse

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found, API create пропущен")
            return warehouse

        from ..lib.api_client import FulfillmentAPIClient
        client = FulfillmentAPIClient(profile)
        payload = {
            'name': warehouse.name,
            'code': warehouse.code,
            'location': 'UKR',
        }
        fulfillment_id = warehouse.fulfillment_client_id.fulfillment_id or profile.fulfillment_profile_id
        try:
            response = client.warehouse.create(fulfillment_id, payload)
            warehouse.fulfillment_warehouse_id = response.get('data', {}).get('warehouse_id')
        except Exception as e:
            _logger.error(f"[Fulfillment] Create failed: {e}")

        return warehouse



    def write(self, vals):
        if self.env.context.get('skip_warehouse_contact'):
            return super().write(vals)

        for record in self:
            if record.is_fulfillment or vals.get('is_fulfillment'):
                if vals.get('partner_id'):
                    parent_id = self._extract_partner_id(vals.get('partner_id'))
                    if parent_id:
                        parent = self.env['res.partner'].browse(parent_id)
                        if parent.exists() and len(self) == 1:
                            warehouse_name = vals.get('name') or record.name
                            child = self._get_or_create_warehouse_contact(parent, warehouse_name)
                            if child:
                                vals['partner_id'] = child.id

                if 'name' in vals and len(self) == 1:
                    parent = record.partner_id
                    if parent and not parent.parent_id:
                        warehouse_name = vals['name']
                        child = self._get_or_create_warehouse_contact(parent, warehouse_name)
                        if child:
                            vals['partner_id'] = child.id

        for record in self:
            is_fulfillment = vals.get('is_fulfillment', record.is_fulfillment)
            if not is_fulfillment:
                continue

            if self.env.context.get('skip_api_sync'):
                _logger.info(f"[SKIP PATCH] Warehouse {record.id} updated locally without API sync")
                continue

            profile = self.env['fulfillment.profile'].search([], limit=1)
            if not profile:
                _logger.warning("[Fulfillment] Profile not found, API sync skipped")
                continue

            from ..lib.api_client import FulfillmentAPIClient
            client = FulfillmentAPIClient(profile)

            payload = {
                'name': vals.get('name', record.name),
                'code': vals.get('code', record.code),
                'location': 'UKR',
            }

            fulfillment_id = record.fulfillment_client_id.fulfillment_id
            warehouse_id = record.fulfillment_warehouse_id

            try:
                if warehouse_id:
                    response = client.warehouse.update(fulfillment_id, warehouse_id, payload)
                    new_id = response.get('data', {}).get('warehouse_id')
                    if new_id and new_id != warehouse_id:
                        record.with_context(skip_api_sync=True).write({
                            'fulfillment_warehouse_id': new_id
                        })
                else:
                    response = client.warehouse.create(fulfillment_id, payload)
                    record.with_context(skip_api_sync=True).write({
                        'fulfillment_warehouse_id': response.get('data', {}).get('warehouse_id')
                    })

            except Exception as e:
                _logger.warning(f"Warehouse update failed: {e}")

        vals['last_update'] = datetime.now()
        return super().write(vals)

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
