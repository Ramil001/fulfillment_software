# Warehouse 
import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient
from datetime import datetime

_logger = logging.getLogger(__name__)

class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    is_fulfillment = fields.Boolean(string="Fulfillment storage", compute="_compute_is_fulfillment", store=True)
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Fulfillment owner", readonly=True)
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Fulfillment client", readonly=True)
    fulfillment_warehouse_id = fields.Char(string="Fulfillment Software Warehouse Id", readonly=True)
    last_update = fields.Datetime(string='Last Update', readonly=True)
    
    @api.model
    def create(self, vals):
        # Устанавливаем контекст чтобы избежать рекурсии при создании контакта
        if self.env.context.get('skip_warehouse_contact'):
            return super().create(vals)
            
        child = None
        fulfillment_partner = None

        # Проверяем, fulfillment ли это партнер
        parent_id = self._extract_partner_id(vals.get('partner_id'))
        parent = self.env['res.partner'].browse(parent_id) if parent_id else None

        if parent and self._is_fulfillment_partner(parent):
            warehouse_name = vals.get('name') or 'Warehouse'
            # создаём или находим дочерний контакт
            child, fulfillment_partner = self._get_or_create_warehouse_contact(parent, warehouse_name)
            if child:
                vals['partner_id'] = child.id

        vals['last_update'] = datetime.now()
        
        # Создаем склад с контекстом skip_warehouse_contact чтобы избежать рекурсии
        warehouse = super().with_context(skip_warehouse_contact=True).create(vals)

        if child and warehouse:
            try:
                # Обновляем контакт с контекстом skip_api_sync
                child.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                    'linked_warehouse_id': warehouse.id
                })
            except Exception as e:
                _logger.warning("[Fulfillment] Could not link child contact to warehouse: %s", e)

        # Синхронизация с API
        if warehouse.is_fulfillment:
            self._sync_warehouse_with_api(warehouse, fulfillment_partner, 'create')

        return warehouse

    def write(self, vals):
        if self.env.context.get('skip_warehouse_contact'):
            return super().write(vals)

        for record in self:
            # Определяем, fulfillment ли партнёр
            partner_id = vals.get('partner_id') or record.partner_id.id
            partner_id = self._extract_partner_id(partner_id)
            parent = self.env['res.partner'].browse(partner_id) if partner_id else None

            if parent and self._is_fulfillment_partner(parent):
                # создаём/находим child
                if len(self) == 1:
                    warehouse_name = vals.get('name') or record.name
                    child, fulfillment_partner = self._get_or_create_warehouse_contact(parent, warehouse_name)
                    if child:
                        vals['partner_id'] = child.id

                    # обновление имени child при смене имени склада
                    if 'name' in vals and record.partner_id and record.partner_id.parent_id:
                        new_name = f"{record.partner_id.parent_id.name} ({vals['name']})"
                        record.partner_id.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'name': new_name
                        })

            # Работа с API
            if record.is_fulfillment and not self.env.context.get('skip_api_sync'):
                self._sync_warehouse_with_api(record, None, 'update', vals)

        vals['last_update'] = datetime.now()
        return super().write(vals)

    def _sync_warehouse_with_api(self, warehouse, fulfillment_partner, operation, vals=None):
        """Вспомогательный метод для синхронизации с API"""
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found, API sync skipped")
            return

        client = FulfillmentAPIClient(profile)
        payload = {
            'name': vals.get('name', warehouse.name) if vals else warehouse.name,
            'code': vals.get('code', warehouse.code) if vals else warehouse.code,
            'location': 'UKR',
        }

        fulfillment_id = (
            fulfillment_partner.fulfillment_id if fulfillment_partner else
            warehouse.fulfillment_client_id.fulfillment_id or
            warehouse.fulfillment_owner_id.fulfillment_id or
            profile.fulfillment_profile_id
        )
        warehouse_id = warehouse.fulfillment_warehouse_id

        try:
            if fulfillment_id:
                wh_api_id = None
                if warehouse_id and operation == 'update':
                    # update existing
                    response = client.warehouse.update(fulfillment_id, warehouse_id, payload)
                    wh_api_id = response.get('data', {}).get('warehouse_id')
                elif operation == 'create':
                    # create new
                    response = client.warehouse.create(fulfillment_id, payload)
                    wh_api_id = response.get('data', {}).get('warehouse_id')

                if wh_api_id:
                    warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                        'fulfillment_warehouse_id': wh_api_id
                    })
                    if warehouse.partner_id:
                        try:
                            warehouse.partner_id.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                                'fulfillment_contact_warehouse_id': wh_api_id
                            })
                        except Exception as e:
                            _logger.warning(
                                f"[Fulfillment] Could not update partner {warehouse.partner_id.id} "
                                f"with warehouse_id: {e}"
                            )
        except Exception as e:
            _logger.warning(f"[Fulfillment] Warehouse API sync failed: {e}")

    @api.depends("partner_id", "partner_id.parent_id", "partner_id.category_id")
    def _compute_is_fulfillment(self):
        """Определяет, является ли склад fulfillment на основе родительского партнёра"""
        for warehouse in self:
            partner = warehouse.partner_id
            is_fulfillment = False
            if partner:
                parent = partner.parent_id or partner
                # 1. Если у родителя заполнен fulfillment_contact_warehouse_id → это fulfillment
                if getattr(parent, "fulfillment_contact_warehouse_id", False):
                    is_fulfillment = True
                # 2. Дополнительно проверяем по тегу "Fulfillment"
                elif parent.category_id.filtered(lambda c: c.name == "Fulfillment"):
                    is_fulfillment = True
            warehouse.is_fulfillment = is_fulfillment

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
            return False, None

        child_name = f"{parent_partner.name} ({warehouse_name})"
        _logger.info(f"[Fulfillment] _get_or_create_warehouse_contact lookup: parent={parent_partner.id} name={child_name}")

        child = self.env['res.partner'].search([
            ('parent_id', '=', parent_partner.id),
            ('name', '=', child_name)
        ], limit=1)

        if child:
            # Подтягиваем fulfillment_partner если есть
            fulfillment_partner = self.env['fulfillment.partners'].search([
                ('partner_id', '=', child.id)
            ], limit=1)
            return child, fulfillment_partner
        
        # Создаем новый контакт с контекстом skip_warehouse_contact
        tag = self.env['res.partner.category'].search([('name', '=', 'Warehouse')], limit=1)
        if not tag:
            tag = self.env['res.partner.category'].create({'name': 'Warehouse'})
            
        vals = {
            'name': child_name,
            'parent_id': parent_partner.id,
            'type': 'delivery',
            'is_company': False,
            'category_id': [(6, 0, [tag.id])],
            # НЕ устанавливаем linked_warehouse_id здесь - это будет сделано позже
        }
        # копируем страну у родителя, если есть
        if parent_partner.country_id:
            vals['country_id'] = parent_partner.country_id.id

        _logger.info(f"[Fulfillment] Creating child contact for warehouse: {vals}")
        # создаём в контексте skip_api_sync и skip_warehouse_contact
        child = self.env['res.partner'].with_context(
            skip_api_sync=True, 
            skip_warehouse_contact=True
        ).create(vals)
        
        fulfillment_partner = self.env['fulfillment.partners'].search([
            ('partner_id', '=', child.id)
        ], limit=1)
        return child, fulfillment_partner


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

                    parent = self.env['res.partner'].with_context(skip_api_sync=True).create(partner_vals)

                    vals['partner_id'] = parent.id

                    
                    try:
                        new_warehouse = self.with_context(skip_api_sync=True).create(vals)
                        processed_ids.add(new_warehouse.id)
                        _logger.info(f"[Fulfillment] Created warehouse: {new_warehouse.name}")
                        # если child был создан — обновим его linked_warehouse_id (на случай race)
                        if child and new_warehouse:
                            try:
                                child.with_context(skip_api_sync=True).write({'linked_warehouse_id': new_warehouse.id})
                            except Exception:
                                _logger.warning("[Fulfillment] Failed to write linked_warehouse_id for contact %s", child.id)
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
        fulfillment_id = None

        # 1. сначала пробуем взять с владельца/клиента
        if self.fulfillment_owner_id:
            fulfillment_id = self.fulfillment_owner_id.fulfillment_id
        elif self.fulfillment_client_id:
            fulfillment_id = self.fulfillment_client_id.fulfillment_id

        # 2. если не нашли — смотрим контакт склада
        if not fulfillment_id and self.partner_id and self.partner_id.linked_warehouse_id:
            linked_wh = self.partner_id.linked_warehouse_id
            if linked_wh.fulfillment_owner_id:
                fulfillment_id = linked_wh.fulfillment_owner_id.fulfillment_id
            elif linked_wh.fulfillment_client_id:
                fulfillment_id = linked_wh.fulfillment_client_id.fulfillment_id

        if not warehouse_api_id:
            _logger.error(f"[Fulfillment] Склад {self.name} (ID={self.id}) не имеет fulfillment_warehouse_id")

        if not fulfillment_id:
            _logger.error(f"[Fulfillment] Склад {self.name} (ID={self.id}) не имеет связанного fulfillment_id")

        return warehouse_api_id, fulfillment_id



    def _is_fulfillment_partner(self, partner):
        """Проверка, является ли партнёр fulfillment"""
        if not partner or not partner.exists():
            return False
        if getattr(partner, "fulfillment_contact_warehouse_id", False):
            return True
        if partner.category_id.filtered(lambda c: c.name == "Fulfillment"):
            return True
        return False
