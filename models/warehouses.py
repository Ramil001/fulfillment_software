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
    fulfillment_warehouse_id = fields.Char(string="Fulfillment warehouse Id")
    last_update = fields.Datetime(string='Last Update', readonly=True)
    
    
    # ===== Onchange handler ===== 
    @api.onchange('partner_id')
    def _onchange_partner(self):
        """Срабатывает при изменении партнёра в stock.picking"""
        if not self.partner_id:
            return

        # Пример действия в onchange
        self.name = f"{self.partner_id.name}"

        # Формируем текст уведомления
        record_name = self.name or "(новый документ)"
        message = f"В документе {record_name} изменён партнёр на {self.partner_id.display_name}."
        title = "Изменение партнёра"


        # === Отправляем уведомление через bus ===
        payload = {
            "type": "fulfillment_notification",
            "payload": {
                "message": message,
                "title": title,
                "level": "info",
                "sticky": False,
            },
        }

        bus = self.env["bus.bus"].sudo()
        users = self.env["res.users"].sudo().search([])

        for user in users:
            partner = user.partner_id
            if not partner:
                continue
            try:
                bus._sendone(partner, "fulfillment_notification", payload)
            except Exception as e:
                _logger.error("[BUS][ERROR] Не удалось отправить уведомление %s: %s", partner.name, e)
    
    
    
    # ---------------------------
    # CREATE
    # ---------------------------
    @api.model_create_multi
    def create(self, vals_list):
        """
        Создание(множественное) склада(ов) в Odoo + синхронизация с внешним Fulfillment API.

        Логика:
        1) Создаём записи в Odoo (super).
        2) Для каждой записи:
        - Получаем parent_partner (parent_id или сам partner).
        - Если нужно — создаём/находим дочерний контакт (warehouse contact) и перекрепляем partner_id.
        - Определяем owner_fulfillment_id (владелец / создатель) — берем из профиля fulfillment.profile.
        - Определяем customer_fulfillment_id (id клиента) — в первую очередь берем у дочернего контакта,
            затем у fulfillment_partner (если связана запись fulfillment.partners для контакта), затем у parent.
        - Если owner и customer известны — посылаем запрос create в API.
        - По успешному ответу записываем fulfillment_warehouse_id и связываем fulfillment_owner/client в Odoo,
            а также дублируем fulfillment_warehouse_id в дочернем контакте.
        """
        _logger.info("[WAREHOUSE][CREATE][START] batch size=%s", len(vals_list))

        # 1) Создаём записи в Odoo локально (может быть множественный create).
        # super().create для model_create_multi принимает список словарей и возвращает recordset созданных записей.
        created_warehouses = super().create(vals_list)
        _logger.info("[WAREHOUSE][CREATE] Created %s warehouses locally", len(created_warehouses))

        # 2) Подготовим профиль (API ключ и настройки) один раз.
        # Используем sudo(), чтобы иметь доступ к сохранённым настройкам независимо от пользователя.
        profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            _logger.warning("[WAREHOUSE][CREATE] No active fulfillment.profile with API key found — skipping API sync for created warehouses")
            return created_warehouses

        # owner_fulfillment_id — это id профиля (владелец/создатель склада в API).
        # По спецификации, это должен быть fulfillment_profile_id из модели fulfillment.profile.
        owner_fulfillment_id = getattr(profile, 'fulfillment_profile_id', False)
        if not owner_fulfillment_id:
            _logger.warning("[WAREHOUSE][CREATE] fulfillment_profile_id missing in profile — skipping API sync")
            return created_warehouses

        # Создаём экземпляр API-клиента на основе профиля.
        client = FulfillmentAPIClient(profile)

        # 3) Обрабатываем каждую созданную запись по отдельности.
        for warehouse in created_warehouses:
            try:
                _logger.info("[WAREHOUSE][CREATE][PROCESS] id=%s name=%s partner=%s", warehouse.id, warehouse.name, bool(warehouse.partner_id))

                # a) Определяем parent_partner: если partner является child — берем его parent, иначе сам partner.
                parent_partner = warehouse.partner_id.parent_id or warehouse.partner_id
                child_contact = None
                fulfillment_partner_obj = None  # запись fulfillment.partners, соответствующая child (если есть)

                # b) Если есть parent_partner — обеспечим существование дочернего контактa для склада
                #    (логика: если partner — фулфилмент-профиль, склад привязывается к дочернему контакту).
                if parent_partner:
                    # _get_or_create_warehouse_contact возвращает (child_contact, fulfillment_partner_obj)
                    child_contact, fulfillment_partner_obj = self._get_or_create_warehouse_contact(parent_partner, warehouse.name)

                    if child_contact:
                        # Перепривязываем warehouse.partner_id на дочерний контакт.
                        # Используем контекст, чтобы избежать рекурсивной синхронизации в write().
                        try:
                            warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({'partner_id': child_contact.id})
                            _logger.info("[WAREHOUSE][CREATE] Relinked warehouse %s → child partner %s", warehouse.id, child_contact.id)
                        except Exception as e:
                            _logger.exception("[WAREHOUSE][CREATE] Failed to relink partner for warehouse %s: %s", warehouse.id, e)

                # c) Определяем customer_fulfillment_id (ID клиента — тот, для кого создаётся склад).
                #    Приоритет:
                #      1) fulfillment_partner_id на дочернем контакте (child_contact.fulfillment_partner_id)
                #      2) если _get_or_create_warehouse_contact вернул объект fulfillment.partners — его fulfillment_id
                #      3) parent_partner.fulfillment_partner_id
                customer_fulfillment_id = None
                if child_contact and getattr(child_contact, 'fulfillment_partner_id', False):
                    customer_fulfillment_id = child_contact.fulfillment_partner_id
                    _logger.debug("[WAREHOUSE][CREATE] Using customer_fulfillment_id from child_contact: %s", customer_fulfillment_id)
                elif fulfillment_partner_obj and getattr(fulfillment_partner_obj, 'fulfillment_id', False):
                    customer_fulfillment_id = fulfillment_partner_obj.fulfillment_id
                    _logger.debug("[WAREHOUSE][CREATE] Using customer_fulfillment_id from fulfillment.partners object: %s", customer_fulfillment_id)
                elif parent_partner and getattr(parent_partner, 'fulfillment_partner_id', False):
                    customer_fulfillment_id = parent_partner.fulfillment_partner_id
                    _logger.debug("[WAREHOUSE][CREATE] Using customer_fulfillment_id from parent_partner: %s", customer_fulfillment_id)

                # d) Если нет customer_fulfillment_id — API требует клиента, пропускаем синхронизацию.
                if not customer_fulfillment_id:
                    _logger.warning("[WAREHOUSE][CREATE] No customer_fulfillment_id for warehouse %s (partner=%s) — skipping API create", warehouse.name, parent_partner.id if parent_partner else None)
                    continue

                # e) Формируем payload для API: owner (fulfillment_id) — мы (profile.fulfillment_profile_id),
                #    а warehouse_customer_fulfillment_id — клиент (customer_fulfillment_id).
                payload = {
                    "name": warehouse.name,
                    "code": warehouse.code,
                    "location": (warehouse.partner_id.city or "") if warehouse.partner_id else "",
                    "short_name": (warehouse.code or warehouse.name or "")[:50].upper(),  # короткое имя, безопасно усечь
                    "warehouse_customer_fulfillment_id": customer_fulfillment_id,
                }

                _logger.info("[WAREHOUSE][CREATE][API] POST → fulfillment_id=%s payload=%s", owner_fulfillment_id, payload)

                # f) Запрос к API — создаём склад под owner_fulfillment_id, указав customer_fulfillment_id
                try:
                    response = client.warehouse.create(
                        fulfillment_id=owner_fulfillment_id,
                        payload=payload
                    )
                except FulfillmentAPIError as e:
                    _logger.error("❌ Fulfillment API error on create for warehouse %s: %s", warehouse.name, e)
                    continue
                except Exception as e:
                    _logger.exception("❌ Unexpected error calling API for warehouse %s: %s", warehouse.name, e)
                    continue

                # g) Обработка ответа API
                if response.get("status") == "success" and "data" in response:
                    data = response["data"]
                    # API вернул параметры: fulfillment_id (создатель), warehouse_customer_fulfillment_id (клиент), warehouse_id (uuid)
                    # Найдём / свяжем локальные записи fulfillment.partners
                    owner_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('fulfillment_id'))], limit=1)
                    client_fp = None
                    # если API вернул разные fulfillment_id и warehouse_customer_fulfillment_id — свяжем client
                    if data.get('warehouse_customer_fulfillment_id') and data.get('warehouse_customer_fulfillment_id') != data.get('fulfillment_id'):
                        client_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('warehouse_customer_fulfillment_id'))], limit=1)
                    else:
                        # Если они совпадают — логируем предупреждение: API вернул одинаковые id для owner и client
                        _logger.warning("[WAREHOUSE][CREATE][API] owner_fulfillment_id == warehouse_customer_fulfillment_id for warehouse %s (api returned same id)", warehouse.name)

                    # h) Запишем результаты в Odoo — используем контекст чтобы не триггерить повторный sync
                    try:
                        warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'fulfillment_owner_id': owner_fp.id if owner_fp else False,
                            'fulfillment_client_id': client_fp.id if client_fp else False,
                            'fulfillment_warehouse_id': data.get('warehouse_id'),
                            'last_update': datetime.now(),
                        })
                    except Exception as e:
                        _logger.exception("[WAREHOUSE][CREATE] Failed to write API IDs to warehouse %s: %s", warehouse.id, e)

                    # i) Если есть дочерний контакт — запишем туда fulfillment_warehouse_id и linked_warehouse_id
                    if child_contact:
                        try:
                            child_contact.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                                'fulfillment_warehouse_id': data.get('warehouse_id'),
                                'linked_warehouse_id': warehouse.id,
                            })
                            _logger.info("[WAREHOUSE][CREATE] Child contact %s updated with fulfillment_warehouse_id=%s", child_contact.id, data.get('warehouse_id'))
                        except Exception as e:
                            _logger.exception("[WAREHOUSE][CREATE] Failed to update child contact %s with warehouse id: %s", child_contact.id if child_contact else None, e)

                    # j) Опционально: можно сохранять fulfillment_warehouse_id на parent_partner (если нужно)
                    try:
                        if parent_partner:
                            # Если parent_partner сам не является фулфилмент-«контактом клиента» (т.е. не child),
                            # всё равно можно записать информацию о наличии warehouse у компании.
                            parent_partner.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                                'fulfillment_warehouse_id': data.get('warehouse_id'),
                            })
                    except Exception as e:
                        _logger.exception("[WAREHOUSE][CREATE] Failed to update parent partner %s with warehouse id: %s", parent_partner.id if parent_partner else None, e)

                    _logger.info("✅ Warehouse %s created in API with id %s", warehouse.name, data.get('warehouse_id'))

                else:
                    _logger.warning("[WAREHOUSE][CREATE][API] unexpected response for %s: %s", warehouse.name, response)

            except Exception as e:
                # Защищаемся от ошибок по одной записи — продолжаем обрабатывать другие
                _logger.exception("[WAREHOUSE][CREATE] Unexpected error processing warehouse %s: %s", getattr(warehouse, 'id', None), e)
                # не делаем self.env.cr.rollback() — чтобы не аннулировать все созданные записи автоматически

        _logger.info("[WAREHOUSE][CREATE][DONE] processed %s warehouses", len(created_warehouses))
        return created_warehouses


    # ---------------------------
    # WRITE
    # ---------------------------
    def write(self, vals):
        _logger.info(f"[WAREHOUSE][WRITE][START] ids={self.ids}, vals={vals}, context={self.env.context}")

        if self.env.context.get('skip_api_sync'):
            _logger.info(f"[WAREHOUSE][WRITE][SKIP_API_SYNC] ids={self.ids}")
            vals['last_update'] = datetime.now()
            res = super().write(vals)
            return res

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
                    "short_name": vals.get("short_name", record.code.upper() if record.code else record.name),
                    "warehouse_customer_fulfillment_id": partner.fulfillment_partner_id,
                }
                _logger.info(f"[WAREHOUSE][WRITE][API] PUT → warehouse_id={record.fulfillment_warehouse_id} payload={payload}")

                response = client.warehouse.update(
                    fulfillment_id=record.fulfillment_owner_id.fulfillment_id,
                    warehouse_id=record.fulfillment_warehouse_id,
                    payload=payload
                )

                if response.get("status") == "success" and "data" in response:
                    data = response["data"]

                    owner_partner = self.env['fulfillment.partners'].search(
                        [('fulfillment_id', '=', data.get('fulfillment_id'))], limit=1
                    )
                    client_partner = None
                    if data.get('warehouse_customer_fulfillment_id') != data.get('fulfillment_id'):
                        client_partner = self.env['fulfillment.partners'].search(
                            [('fulfillment_id', '=', data.get('warehouse_customer_fulfillment_id'))], limit=1
                        )
                    else:
                        _logger.warning(f"⚠️ API вернул одинаковые fulfillment_id и warehouse_customer_fulfillment_id для склада {record.name}")


                    record.with_context(skip_import_warehouses=True).write({
                        'fulfillment_owner_id': owner_partner.id if owner_partner else False,
                        'fulfillment_client_id': client_partner.id if client_partner else False,
                        'fulfillment_warehouse_id': data.get('warehouse_id'),
                        'last_update': datetime.now(),
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
        """Импорт складов из Fulfillment API для конкретного партнёра.

        Поведение:
        1) Запросить список складов у API.
        2) Для каждого склада — найти или создать запись stock.warehouse.
        3) Проверить уникальность кода и имени (как в твоём коде).
        4) Создать/обновить дочерний контакт для склада.
        5) Записать fulfillment_owner_id, fulfillment_client_id, fulfillment_warehouse_id.
        """
        _logger.info("[IMPORT][WAREHOUSES][START] for partner %s (%s)", fulfillment_partner.name, fulfillment_partner.fulfillment_id)

        try:
            profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            if not profile or not profile.fulfillment_api_key:
                _logger.error("[IMPORT][WAREHOUSES] Нет профиля с API ключом")
                return
            client = FulfillmentAPIClient(profile)
            response = client.fulfillment.list_warehouses(fulfillment_partner.fulfillment_id)
            _logger.info("[IMPORT][WAREHOUSES] API response: %s", response)

            if response.get("status") != "success":
                _logger.error("[IMPORT][WAREHOUSES] API call failed: %s", response)
                return

            warehouses = response.get("data", [])
            _logger.info("[IMPORT][WAREHOUSES] Total received: %s", len(warehouses))

            # карта существующих складов
            existing = self.search([("fulfillment_warehouse_id", "in", [w.get("warehouse_id") for w in warehouses])])
            existing_map = {w.fulfillment_warehouse_id: w for w in existing}

            for wh in warehouses:
                try:
                    wh_id = wh.get("warehouse_id")
                    _logger.info("[IMPORT][WAREHOUSE] >>> Processing %s (%s)", wh.get("name"), wh_id)

                    warehouse = existing_map.get(wh_id)

                    # уникальный код
                    code = wh.get("code") or wh.get("name") or "WH"
                    original_code = code
                    suffix = 1
                    while self.search_count([("code", "=", code), ("id", "!=", warehouse.id if warehouse else 0)]):
                        code = f"{original_code}_{suffix}"
                        suffix += 1

                    # уникальное имя
                    base_name = wh.get("name") or "Warehouse"
                    unique_name = base_name
                    suffix = 1
                    while self.search_count([("name", "=", unique_name), ("id", "!=", warehouse.id if warehouse else 0)]):
                        unique_name = f"{base_name} ({suffix})"
                        suffix += 1

                    vals = {
                        "name": unique_name,
                        "code": code,
                        "fulfillment_warehouse_id": wh_id,
                        "active": True,
                    }

                    if warehouse:
                        _logger.info("[IMPORT][WAREHOUSE] Updating existing %s", warehouse.id)
                        warehouse.with_context(skip_api_sync=True).write(vals)
                    else:
                        _logger.info("[IMPORT][WAREHOUSE] Creating new warehouse")
                        warehouse = self.with_context(skip_api_sync=True).create(vals)

                    # --- Контакт (child) ---
                    parent_partner = fulfillment_partner.partner_id
                    child_contact, _ = warehouse._get_or_create_warehouse_contact(parent_partner, warehouse.name)

                    if child_contact:
                        warehouse.with_context(skip_api_sync=True).write({"partner_id": child_contact.id})

                    # --- Связь с fulfillment.partners ---
                    owner_fp = self.env["fulfillment.partners"].search([("fulfillment_id", "=", wh.get("fulfillment_id"))], limit=1)
                    client_fp = None
                    if wh.get("warehouse_customer_fulfillment_id") != wh.get("fulfillment_id"):
                        client_fp = self.env["fulfillment.partners"].search([("fulfillment_id", "=", wh.get("warehouse_customer_fulfillment_id"))], limit=1)
                    else:
                        _logger.warning(f"⚠️ API вернул одинаковые fulfillment_id и warehouse_customer_fulfillment_id для склада {wh.get('name')}")

                    warehouse.with_context(skip_api_sync=True).write({
                        "fulfillment_owner_id": owner_fp.id if owner_fp else False,
                        "fulfillment_client_id": client_fp.id if client_fp else False,
                    })

                    # --- Запишем warehouse_id в контакт ---
                    if child_contact:
                        child_contact.with_context(skip_api_sync=True).write({
                            "fulfillment_warehouse_id": wh_id,
                            "linked_warehouse_id": warehouse.id,
                        })

                    _logger.info("✅ Imported warehouse %s (%s)", warehouse.name, wh_id)

                except Exception as e:
                    _logger.exception("[IMPORT][WAREHOUSE] Error while processing %s: %s", wh, str(e))
                    self.env.cr.rollback()

            _logger.info("[IMPORT][WAREHOUSES][DONE] Imported: %s", len(warehouses))

        except Exception as e:
            _logger.exception("[IMPORT][WAREHOUSES] Fatal error: %s", str(e))
            self.env.cr.rollback()

    # ---------------------------
    # HELPERS
    # ---------------------------


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
            try:
                partner = warehouse.partner_id
                is_fulfillment = False

                if not partner:
                    warehouse.is_fulfillment = False
                    continue

                # Берём родителя, если он есть
                parent = partner.parent_id or partner

                # Проверяем, связан ли партнёр с fulfillment-складом
                if getattr(parent, "fulfillment_contact_warehouse_id", False):
                    is_fulfillment = True

                # Проверяем категории, если они есть
                elif getattr(parent, "category_id", False):
                    if any(c.name == "Fulfillment" for c in parent.category_id):
                        is_fulfillment = True

                warehouse.is_fulfillment = is_fulfillment

            except Exception as e:
                # Безопасный fallback, чтобы транзакция не падала
                warehouse.is_fulfillment = False
                _logger.error(
                    "[Fulfillment] Ошибка при вычислении is_fulfillment для склада '%s': %s",
                    warehouse.display_name or warehouse.name, e,
                )
