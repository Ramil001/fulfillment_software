# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError
from datetime import datetime

_logger = logging.getLogger(__name__)


class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    is_fulfillment = fields.Boolean(string="Fulfillment storage", compute="_compute_is_fulfillment", store=True)
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Creator fulfillment Id", readonly=True)
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Customer fulfillment Id", readonly=True)
    fulfillment_warehouse_id = fields.Char(string="Fulfillment warehouse Id", readonly=True)
    last_update = fields.Datetime(string='Last Update', readonly=True)

    # ---------------------------
    # CREATE
    # ---------------------------
    @api.model
    def create(self, vals):
        """Создание склада в Odoo + синхронизация с внешним Fulfillment API"""

        _logger.info(f"[WAREHOUSE][CREATE][START] vals={vals}")

        # 1. Создаём запись склада в Odoo (локально)
        warehouse = super().create(vals)

        try:
            # 2. Получаем активный профиль Fulfillment (API ключ и настройки)
            profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            if not profile or not profile.fulfillment_api_key:
                _logger.warning("⚠️ Нет активного профиля с API ключом — склад %s не отправлен во Fulfillment", warehouse.name)
                return warehouse

            # 3. Проверяем, есть ли у склада связанный партнёр
            partner = warehouse.partner_id
            if partner and not partner.fulfillment_partner_id and partner.parent_id:
                partner = partner.parent_id

            if not partner or not partner.fulfillment_partner_id:
                _logger.warning("❌ У склада %s нет партнёра с fulfillment_partner_id", warehouse.name)
                return warehouse

            # 4. Готовим payload для запроса
            payload = {
                "name": warehouse.name,                       # Название склада
                "code": warehouse.code,                       # Код склада
                "location": warehouse.partner_id.city or "",  # Локация (берём из города партнёра)
                "short_name": warehouse.code.upper(),         # Короткое имя (например W11)
                "warehouse_customer_fulfillment_id": partner.fulfillment_partner_id,

            }

            # 5. Создаём клиент для API
            client = FulfillmentAPIClient(profile)

            _logger.info(f"[WAREHOUSE][CREATE][API] POST → fulfillment_id={partner.fulfillment_partner_id} payload={payload}")

            # 6. Отправляем запрос на создание склада в API
            response = client.warehouse.create(
                fulfillment_id=partner.fulfillment_partner_id,
                payload=payload
            )

            # 7. Обрабатываем успешный ответ API
            if response.get("status") == "success" and "data" in response:
                data = response["data"]

                owner_partner = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('fulfillment_id'))], limit=1)
                client_partner = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('warehouse_customer_fulfillment_id'))], limit=1)

                warehouse.write({
                    'fulfillment_owner_id': owner_partner.id if owner_partner else False,
                    'fulfillment_client_id': client_partner.id if client_partner else False,
                    'fulfillment_warehouse_id': data.get('warehouse_id'),
                })

                # Также обновляем связанного партнёра (привязываем склад)
                partner.write({
                    "fulfillment_warehouse_id": data.get("warehouse_id"),
                    "linked_warehouse_id": warehouse.id
                })

                _logger.info("✅ Warehouse %s создан в API с ID %s", warehouse.name, data.get("warehouse_id"))

            else:
                _logger.warning("[WAREHOUSE][CREATE][API] неожиданный ответ: %s", response)

        # 8. Обработка ошибок
        except FulfillmentAPIError as e:
            _logger.error("❌ Ошибка API при создании склада: %s", str(e))
        except Exception as e:
            _logger.error("❌ Неожиданная ошибка при создании склада: %s", str(e))

        # 9. Возвращаем объект склада в Odoo
        return warehouse

    # ---------------------------
    # WRITE
    # ---------------------------
    def write(self, vals):
        _logger.info(f"[WAREHOUSE][WRITE][START] ids={self.ids}, vals={vals}, context={self.env.context}")

        if self.env.context.get('skip_import_warehouses'):
            vals['last_update'] = datetime.now()
            res = super().write(vals)
            _logger.info(f"[WAREHOUSE][WRITE][SKIP_IMPORT] ids={self.ids}")
            return res

        for record in self:
            partner_id = vals.get('partner_id') or record.partner_id.id
            partner_id = self._extract_partner_id(partner_id)
            parent = self.env['res.partner'].browse(partner_id) if partner_id else None

            if parent and self._is_fulfillment_partner(parent):
                if len(self) == 1:
                    warehouse_name = vals.get('name') or record.name
                    child, fulfillment_partner = self._get_or_create_warehouse_contact(parent, warehouse_name)
                    if child:
                        _logger.info(f"[WAREHOUSE][WRITE] Relinking to child contact {child.id}")
                        vals['partner_id'] = child.id

                    if 'name' in vals and record.partner_id and record.partner_id.parent_id:
                        new_name = f"{record.partner_id.parent_id.name} ({vals['name']})"
                        _logger.info(f"[WAREHOUSE][WRITE] Renaming child partner {record.partner_id.id} → {new_name}")
                        record.partner_id.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'name': new_name
                        })

            if record.is_fulfillment and not self.env.context.get('skip_api_sync'):
                _logger.info(f"[WAREHOUSE][WRITE] Syncing {record.id} to API with vals={vals}")
                self._sync_update(record, vals)

        vals['last_update'] = datetime.now()
        res = super().write(vals)
        _logger.info(f"[WAREHOUSE][WRITE][DONE] ids={self.ids}")
        return res

    # ---------------------------
    # SYNC HELPERS
    # ---------------------------
    def _prepare_payload(self, warehouse, vals=None):
        """Готовит payload для API"""
        customer_fulfillment_id = None
        if warehouse.partner_id and warehouse.partner_id.fulfillment_partner_id:
            customer_fulfillment_id = warehouse.partner_id.fulfillment_partner_id

        payload = {
            'name': vals.get('name', warehouse.name) if vals else warehouse.name,
            'code': vals.get('code', warehouse.code) if vals else warehouse.code,
            'location': 'UKR',
            'warehouse_customer_fulfillment_id': customer_fulfillment_id,
        }
        return payload

    def _sync_create(self, warehouse, fulfillment_partner=None):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[SYNC][CREATE] Profile not found, skip API sync")
            return

        client = FulfillmentAPIClient(profile)
        payload = self._prepare_payload(warehouse)
        fulfillment_id = (
            fulfillment_partner.fulfillment_id if fulfillment_partner else
            warehouse.fulfillment_client_id.fulfillment_id or
            warehouse.fulfillment_owner_id.fulfillment_id or
            profile.fulfillment_profile_id
        )

        if not fulfillment_id:
            _logger.warning("[SYNC][CREATE] No fulfillment_id, skip")
            return

        try:
            _logger.info(f"[SYNC][CREATE][START] warehouse={warehouse.name}, payload={payload}")
            response = client.warehouse.create(fulfillment_id, payload)
            wh_api_id = response.get('data', {}).get('warehouse_id')
            if wh_api_id:
                warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                    'fulfillment_warehouse_id': wh_api_id
                })
                if warehouse.partner_id:
                    warehouse.partner_id.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                        'fulfillment_contact_warehouse_id': wh_api_id
                    })
            _logger.info(f"[SYNC][CREATE][DONE] warehouse={warehouse.name}")
        except Exception as e:
            _logger.warning(f"[SYNC][CREATE][FAILED] warehouse={warehouse.name}, error={e}")

    def _sync_update(self, warehouse, vals=None):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[SYNC][UPDATE] Profile not found, skip API sync")
            return

        client = FulfillmentAPIClient(profile)
        payload = self._prepare_payload(warehouse, vals)
        fulfillment_id = (
            warehouse.fulfillment_client_id.fulfillment_id or
            warehouse.fulfillment_owner_id.fulfillment_id or
            profile.fulfillment_profile_id
        )
        warehouse_id = warehouse.fulfillment_warehouse_id

        if not fulfillment_id or not warehouse_id:
            _logger.warning("[SYNC][UPDATE] Missing IDs, skip")
            return

        try:
            _logger.info(f"[SYNC][UPDATE][START] warehouse={warehouse.name}, payload={payload}")
            response = client.warehouse.update(fulfillment_id, warehouse_id, payload)
            _logger.info(f"[SYNC][UPDATE][DONE] warehouse={warehouse.name}, response={response}")
        except Exception as e:
            _logger.warning(f"[SYNC][UPDATE][FAILED] warehouse={warehouse.name}, error={e}")

    # ---------------------------
    # SYNC
    # ---------------------------
    def _sync_warehouse_with_api(self, warehouse, fulfillment_partner, operation, vals=None):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[SYNC] Profile not found, skip API sync")
            return

        client = FulfillmentAPIClient(profile)

        # Получаем warehouse_customer_fulfillment_id
        customer_fulfillment_id = None
        if warehouse.partner_id and warehouse.partner_id.fulfillment_partner_id:
            customer_fulfillment_id = warehouse.partner_id.fulfillment_partner_id

        payload = {
            'name': vals.get('name', warehouse.name) if vals else warehouse.name,
            'code': vals.get('code', warehouse.code) if vals else warehouse.code,
            'location': 'UKR',
            'warehouse_customer_fulfillment_id': customer_fulfillment_id,
        }

        fulfillment_id = (
            fulfillment_partner.fulfillment_id if fulfillment_partner else
            warehouse.fulfillment_client_id.fulfillment_id or
            warehouse.fulfillment_owner_id.fulfillment_id or
            profile.fulfillment_profile_id
        )
        warehouse_id = warehouse.fulfillment_warehouse_id

        try:
            _logger.info(f"[SYNC][{operation.upper()}][START] warehouse={warehouse.name}, id={warehouse.id}, payload={payload}")

            if fulfillment_id:
                wh_api_id = None
                if warehouse_id and operation == 'update':
                    response = client.warehouse.update(fulfillment_id, warehouse_id, payload)
                    wh_api_id = response.get('data', {}).get('warehouse_id')
                    _logger.info(f"[SYNC][UPDATE][RESPONSE] {response}")
                elif operation == 'create':
                    response = client.warehouse.create(fulfillment_id, payload)
                    wh_api_id = response.get('data', {}).get('warehouse_id')
                    _logger.info(f"[SYNC][CREATE][RESPONSE] {response}")

                if wh_api_id:
                    warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                        'fulfillment_warehouse_id': wh_api_id
                    })
                    if warehouse.partner_id:
                        warehouse.partner_id.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'fulfillment_contact_warehouse_id': wh_api_id
                        })
                        _logger.info(f"[SYNC] Linked partner {warehouse.partner_id.id} with warehouse_id={wh_api_id}")

            _logger.info(f"[SYNC][{operation.upper()}][DONE] warehouse={warehouse.name}, id={warehouse.id}")

        except Exception as e:
            _logger.warning(f"[SYNC][{operation.upper()}][FAILED] warehouse={warehouse.name}, id={warehouse.id}, error={e}")

    # ---------------------------
    # IMPORT
    # ---------------------------
    @api.model
    def import_warehouses(self, fulfillment_partner):
        _logger.info("[IMPORT][WAREHOUSES][START] for partner %s", fulfillment_partner.name)
        client = fulfillment_partner._get_fulfillment_client()

        try:
            _logger.info("[IMPORT][WAREHOUSES] Requesting API for partner_id=%s", fulfillment_partner.fulfillment_id)
            response = client.get(f"/api/v1/fulfillments/{fulfillment_partner.fulfillment_id}/warehouses")
            _logger.info("[IMPORT][WAREHOUSES] API response received: %s", response)

            if response.get("status") != "success":
                _logger.error("[IMPORT][WAREHOUSES] API call failed: %s", response)
                return

            warehouses = response.get("data", [])
            _logger.info("[IMPORT][WAREHOUSES] Total received: %s", len(warehouses))

            # собрать карту уже существующих складов
            existing = self.search([("fulfillment_id", "=", fulfillment_partner.fulfillment_id)])
            existing_map = {w.fulfillment_warehouse_id: w for w in existing}
            _logger.info("[IMPORT][WAREHOUSES] Existing in DB: %s", len(existing_map))

            for wh in warehouses:
                try:
                    wh_id = wh.get("warehouse_id")
                    _logger.info("[IMPORT][WAREHOUSE] >>> Start %s (%s)", wh.get("name"), wh_id)

                    # Поиск существующего
                    warehouse = existing_map.get(wh_id)
                    _logger.info("[IMPORT][WAREHOUSE] Found existing? %s", bool(warehouse))

                    # Проверка уникальности кода
                    code = wh.get("code") or wh.get("name") or "WH"
                    original_code = code
                    suffix = 1
                    while self.search_count([("code", "=", code), ("id", "!=", warehouse.id if warehouse else 0)]):
                        _logger.warning("[IMPORT][WAREHOUSE] Code %s already exists, trying new", code)
                        code = f"{original_code}_{suffix}"
                        suffix += 1
                    _logger.info("[IMPORT][WAREHOUSE] Final code: %s", code)

                    # Проверка уникальности имени
                    base_name = wh.get("name") or "Warehouse"
                    unique_name = base_name
                    suffix = 1
                    while self.search_count([("name", "=", unique_name), ("id", "!=", warehouse.id if warehouse else 0)]):
                        _logger.warning("[IMPORT][WAREHOUSE] Name %s already exists, trying new", unique_name)
                        unique_name = f"{base_name} ({suffix})"
                        suffix += 1
                    _logger.info("[IMPORT][WAREHOUSE] Final name: %s", unique_name)

                    # Данные для записи
                    vals = {
                        "name": unique_name,
                        "code": code,
                        "location": wh.get("location"),
                        "fulfillment_id": wh.get("fulfillment_id"),
                        "fulfillment_warehouse_id": wh_id,
                        "fulfillment_partner_id": fulfillment_partner.id,
                        "active": True,
                    }
                    _logger.info("[IMPORT][WAREHOUSE] Prepared vals: %s", vals)

                    if warehouse:
                        _logger.info("[IMPORT][WAREHOUSE] Updating existing warehouse %s", warehouse.id)
                        warehouse.write(vals)
                    else:
                        _logger.info("[IMPORT][WAREHOUSE] Creating new warehouse")
                        warehouse = self.create(vals)

                    # Создать/привязать contact
                    _logger.info("[IMPORT][WAREHOUSE] Ensuring contact for warehouse %s", warehouse.id)
                    warehouse._get_or_create_warehouse_contact()
                    _logger.info("[IMPORT][WAREHOUSE] Contact ensured for %s", warehouse.id)

                except Exception as e:
                    _logger.exception("[IMPORT][WAREHOUSE] Error while processing warehouse %s: %s", wh, str(e))
                    self.env.cr.rollback()

            _logger.info("[IMPORT][WAREHOUSES][DONE] Imported: %s", len(warehouses))

        except Exception as e:
            _logger.exception("[IMPORT][WAREHOUSES] Fatal error: %s", str(e))
            self.env.cr.rollback()

    # ---------------------------
    # HELPERS
    # ---------------------------
    def _extract_partner_id(self, val):
        if not val:
            return False
        if isinstance(val, int):
            return val
        if isinstance(val, (list, tuple)):
            if len(val) == 3 and isinstance(val[0], int):
                cmd = int(val[0])
                if cmd == 4 and isinstance(val[1], int):
                    return int(val[1])
                if cmd == 6 and isinstance(val[2], list) and len(val[2]) == 1:
                    return int(val[2][0])
            if len(val) and isinstance(val[0], (list, tuple)):
                inner = val[0]
                if len(inner) >= 2 and inner[0] == 4:
                    return int(inner[1])
        return False

    @api.model
    def _get_or_create_warehouse_contact(self, parent_partner, warehouse_name):
        if not parent_partner or not parent_partner.exists():
            return False, None

        child_name = f"{parent_partner.name} ({warehouse_name})"
        _logger.info(f"[CONTACT][LOOKUP] parent={parent_partner.id}, name={child_name}")

        child = self.env['res.partner'].search([
            ('parent_id', '=', parent_partner.id),
            ('name', '=', child_name)
        ], limit=1)

        if child:
            _logger.info(f"[CONTACT][FOUND] child {child.id} for {child_name}")
            fulfillment_partner = self.env['fulfillment.partners'].search([
                ('partner_id', '=', child.id)
            ], limit=1)
            return child, fulfillment_partner

        tag = self.env['res.partner.category'].search([('name', '=', 'Warehouse')], limit=1)
        if not tag:
            tag = self.env['res.partner.category'].create({'name': 'Warehouse'})

        vals = {
            'name': child_name,
            'parent_id': parent_partner.id,
            'type': 'delivery',
            'is_company': False,
            'category_id': [(6, 0, [tag.id])]
        }
        if parent_partner.country_id:
            vals['country_id'] = parent_partner.country_id.id

        _logger.info(f"[CONTACT][CREATE] {vals}")
        child = self.env['res.partner'].with_context(skip_api_sync=True, skip_warehouse_contact=True).create(vals)

        fulfillment_partner = self.env['fulfillment.partners'].search([
            ('partner_id', '=', child.id)
        ], limit=1)

        return child, fulfillment_partner

    @api.depends("partner_id", "partner_id.parent_id", "partner_id.category_id")
    def _compute_is_fulfillment(self):
        for warehouse in self:
            partner = warehouse.partner_id
            is_fulfillment = False
            if partner:
                parent = partner.parent_id or partner
                if getattr(parent, "fulfillment_contact_warehouse_id", False):
                    is_fulfillment = True
                elif parent.category_id.filtered(lambda c: c.name == "Fulfillment"):
                    is_fulfillment = True
            warehouse.is_fulfillment = is_fulfillment
 
 
    def _is_fulfillment_partner(self, partner):
        """Проверка, является ли партнёр фулфилментом"""
        if not partner:
            return False
        # партнёр считается фулфилментом, если есть fulfillment_partner_id
        if getattr(partner, 'fulfillment_partner_id', False):
            return True
        # или если у партнёра есть категория Fulfillment
        if partner.category_id.filtered(lambda c: c.name == 'Fulfillment'):
            return True
        return False
