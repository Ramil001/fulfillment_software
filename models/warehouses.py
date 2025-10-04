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
        """Создание склада в Odoo + синхронизация с внешним Fulfillment API.

        Поведение:
        1) Создаём запись склада в Odoo (super).
        2) Создаём / находи́м дочерний контакт склада (child contact) и подставляем его
        в warehouse.partner_id (делаем write с контекстом skip_api_sync чтобы не вызывать рекурсивный sync).
        3) Берём fulfillment_id (owner) у родителя/партнёра и warehouse_customer_fulfillment_id
        у дочернего контакта (если есть) — и отправляем POST в API.
        4) При успешном ответе записываем в warehouse поля и дублируем warehouse_id в дочерний контакт.
        """
        _logger.info("[WAREHOUSE][CREATE][START] vals=%s", vals)

        # 1) Создаём запись в Odoo
        warehouse = super().create(vals)

        # 2) Подготовка и создание/получение дочернего контакта (parent может быть company)
        parent_partner = warehouse.partner_id.parent_id or warehouse.partner_id
        child_contact = None
        fulfillment_partner_obj = None

        if parent_partner:
            # возвращает (child, fulfillment_partner) — child может быть уже существующим
            child_contact, fulfillment_partner_obj = self._get_or_create_warehouse_contact(parent_partner, warehouse.name)
            if child_contact:
                # Привязываем склад к дочернему контакту, но пропускаем API синхронизацию во время этого write
                try:
                    warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({'partner_id': child_contact.id})
                    _logger.info("[WAREHOUSE][CREATE] Partner relinked to child contact %s for warehouse %s", child_contact.id, warehouse.id)
                except Exception as e:
                    _logger.exception("[WAREHOUSE][CREATE] Failed to relink partner to child: %s", e)

        # 3) Профиль и проверка API ключа
        try:
            profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            if not profile or not profile.fulfillment_api_key:
                _logger.warning("⚠️ Нет активного профиля с API ключом — склад %s не отправлен во Fulfillment", warehouse.name)
                return warehouse

            # Определяем, кто будет owner fulfillment_id (создатель склада).
            # Обычно это parent_partner (company). Если нет — используем профиль.
            owner_fulfillment_id = False
            if parent_partner and getattr(parent_partner, 'fulfillment_partner_id', False):
                owner_fulfillment_id = parent_partner.fulfillment_partner_id
            elif fulfillment_partner_obj and getattr(fulfillment_partner_obj, 'fulfillment_id', False):
                # если _get_or_create_warehouse_contact вернул fulfillment_partner привязанный к дочернему
                owner_fulfillment_id = fulfillment_partner_obj.fulfillment_id
            else:
                owner_fulfillment_id = profile.fulfillment_profile_id

            # Для warehouse_customer_fulfillment_id предпочтение — у дочернего контакта, иначе у родителя
            customer_fulfillment_id = None
            if child_contact and getattr(child_contact, 'fulfillment_partner_id', False):
                customer_fulfillment_id = child_contact.fulfillment_partner_id
            elif parent_partner and getattr(parent_partner, 'fulfillment_partner_id', False):
                customer_fulfillment_id = parent_partner.fulfillment_partner_id

            if not owner_fulfillment_id:
                _logger.warning("[WAREHOUSE][CREATE] No owner fulfillment_id found, skipping API sync for %s", warehouse.name)
                return warehouse

            if not customer_fulfillment_id:
                _logger.warning("[WAREHOUSE][CREATE] No customer_fulfillment_id found for warehouse %s — API requires it, skipping", warehouse.name)
                return warehouse

            # 4) Подготовка payload
            payload = {
                "name": warehouse.name,
                "code": warehouse.code,
                "location": (child_contact.city or child_contact.parent_id.city or "") if child_contact else (parent_partner.city or ""),
                "short_name": (warehouse.code or "").upper(),
                # обязательно передаём warehouse_customer_fulfillment_id (ID клиента)
                "warehouse_customer_fulfillment_id": customer_fulfillment_id,
            }

            _logger.info("[WAREHOUSE][CREATE][API] POST → fulfillment_id=%s payload=%s", owner_fulfillment_id, payload)

            client = FulfillmentAPIClient(profile)
            response = client.warehouse.create(
                fulfillment_id=owner_fulfillment_id,
                payload=payload
            )

            # 5) Обработка успешного ответа
            if response.get("status") == "success" and "data" in response:
                data = response["data"]

                # Найдём/свяжем fulfillment.partners записи (owner и client) по возвращённым fulfillment_id
                owner_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('fulfillment_id'))], limit=1)
                client_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('warehouse_customer_fulfillment_id'))], limit=1)

                # Пишем в warehouse (через контекст, чтобы избежать повторной синхронизации)
                try:
                    warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                        'fulfillment_owner_id': owner_fp.id if owner_fp else False,
                        'fulfillment_client_id': client_fp.id if client_fp else False,
                        'fulfillment_warehouse_id': data.get('warehouse_id'),
                    })
                except Exception as e:
                    _logger.exception("[WAREHOUSE][CREATE] Failed to write API IDs to warehouse: %s", e)

                # Дублируем warehouse_id и ставим связь linked_warehouse_id у дочернего контакта
                if child_contact:
                    try:
                        child_contact.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'fulfillment_warehouse_id': data.get('warehouse_id'),
                            'linked_warehouse_id': warehouse.id,
                        })
                        _logger.info("[WAREHOUSE][CREATE] Child contact %s updated with fulfillment_warehouse_id=%s", child_contact.id, data.get('warehouse_id'))
                    except Exception as e:
                        _logger.exception("[WAREHOUSE][CREATE] Failed to update child contact with warehouse id: %s", e)

                # Также убедимся, что у (возможно) родителя есть ссылка на warehouse (опционально)
                try:
                    if parent_partner:
                        parent_partner.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'fulfillment_warehouse_id': data.get('warehouse_id'),
                        })
                except Exception as e:
                    _logger.exception("[WAREHOUSE][CREATE] Failed to update parent partner with warehouse id: %s", e)

                _logger.info("✅ Warehouse %s created in API with id %s", warehouse.name, data.get('warehouse_id'))
            else:
                _logger.warning("[WAREHOUSE][CREATE][API] unexpected response: %s", response)

        except FulfillmentAPIError as e:
            _logger.error("❌ Fulfillment API error creating warehouse %s: %s", warehouse.name, e)
        except Exception as e:
            _logger.exception("❌ Unexpected error during warehouse create sync for %s: %s", warehouse.name, e)

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

        res = super().write(vals)

        try:
            profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            if not profile or not profile.fulfillment_api_key:
                _logger.warning("⚠️ Нет активного профиля — пропускаем API write")
                return res

            client = FulfillmentAPIClient(profile)

            for record in self:
                if not record.fulfillment_warehouse_id:
                    _logger.warning("❌ У склада %s нет fulfillment_warehouse_id — обновление в API невозможно", record.name)
                    continue

                partner = record.partner_id
                if partner and not partner.fulfillment_partner_id and partner.parent_id:
                    partner = partner.parent_id

                if not partner or not partner.fulfillment_partner_id:
                    _logger.warning("❌ У склада %s нет партнёра с fulfillment_partner_id", record.name)
                    continue

                # payload для обновления
                payload = {
                    "name": vals.get("name", record.name),
                    "code": vals.get("code", record.code),
                    "location": vals.get("location", record.partner_id.city or ""),
                    "short_name": vals.get("short_name", record.short_name or record.code.upper()),
                    "warehouse_customer_fulfillment_id": partner.fulfillment_partner_id,
                }

                _logger.info(f"[WAREHOUSE][WRITE][API] PUT → warehouse_id={record.fulfillment_warehouse_id} payload={payload}")

                response = client.warehouse.update(
                    warehouse_id=record.fulfillment_warehouse_id,
                    payload=payload
                )

                if response.get("status") == "success" and "data" in response:
                    data = response["data"]

                    owner_partner = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('fulfillment_id'))], limit=1)
                    client_partner = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('warehouse_customer_fulfillment_id'))], limit=1)

                    record.write({
                        'fulfillment_owner_id': owner_partner.id if owner_partner else False,
                        'fulfillment_client_id': client_partner.id if client_partner else False,
                        'fulfillment_warehouse_id': data.get('warehouse_id'),
                    })

                    _logger.info("✅ Warehouse %s обновлён в API (ID %s)", record.name, data.get("warehouse_id"))
                else:
                    _logger.warning("[WAREHOUSE][WRITE][API] неожиданный ответ: %s", response)

        except FulfillmentAPIError as e:
            _logger.error("❌ Ошибка API при обновлении склада: %s", str(e))
        except Exception as e:
            _logger.error("❌ Неожиданная ошибка при обновлении склада: %s", str(e))

        vals['last_update'] = datetime.now()
        _logger.info(f"[WAREHOUSE][WRITE][DONE] ids={self.ids}")
        return res


  
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
