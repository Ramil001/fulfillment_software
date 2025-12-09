# -*- coding: utf-8 -*-
import json
from odoo import models, api, fields, _
from odoo.exceptions import UserError, ValidationError
import logging
from datetime import datetime
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)





# ================== Strategy: Transfer Mappers ==================
class BaseTransferMapper:
    def build(self, picking, items, warehouse_out_id, warehouse_in_id):
        raise NotImplementedError


class IncomingTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out_id, warehouse_in_id):
        return {
            "reference": picking.name or picking.origin or "Odoo",
            "partner": picking.partner_id.name if picking.partner_id else None,
            "warehouse_out": warehouse_out_id,
            "warehouse_in": warehouse_in_id,
            "status": picking.state or "draft",
            "items": items,

        }


class OutgoingTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out_id, warehouse_in_id):
        base_payload = {
            "reference": picking.name or picking.origin or "Odoo",
            "partner": picking.partner_id.name if picking.partner_id else None,
            "warehouse_out": warehouse_out_id,
            "warehouse_in": warehouse_in_id,
            "status": picking.state or "draft",
            "items": items,
        }
        
        _logger.info("[Fulfillment][Mapper] Built base payload for outgoing transfer")
        return base_payload

class InternalTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out_id, warehouse_in_id):
        return {
            "reference": picking.name or picking.origin or "Odoo",
            "warehouse_out": warehouse_out_id,
            "warehouse_in": warehouse_in_id,
            "status": picking.state or "draft",
            "items": items,
        }


# ================== Adapter ==================
class PickingAdapter:
    _strategies = {
        "incoming": IncomingTransferMapper(),
        "outgoing": OutgoingTransferMapper(),
        "internal": InternalTransferMapper(),
    }

    @classmethod
    def to_api_payload(cls, picking, items, warehouse_out_id, warehouse_in_id):
        mapper = cls._strategies.get(picking.picking_type_code)
        if not mapper:
            raise ValueError(f"Unsupported picking type {picking.picking_type_code}")
        return mapper.build(picking, items, warehouse_out_id, warehouse_in_id)


# ================== Builder ==================
class FulfillmentItemBuilder:
    def __init__(self, client):
        self.client = client

    def build_items(self, moves):
        items = []
        for move in moves:
            product_tmpl = move.product_id.product_tmpl_id
            fulfillment_id = self._ensure_remote_product(product_tmpl)
            if not fulfillment_id:
                continue

            items.append({
                "name": move.product_id.name,
                "product_id": fulfillment_id,
                "quantity": float(move.product_uom_qty or 0.0),
                "unit": move.product_uom.name if move.product_uom else 'Units',
            })
        return items

    def _ensure_remote_product(self, tmpl):
        """Создаём продукт в API, если его ещё нет"""
        if not getattr(tmpl, "fulfillment_product_id", None):
            product_payload = {
                "name": tmpl.name,
                "sku": tmpl.default_code or f"SKU-{tmpl.id}",
                "barcode": tmpl.barcode or str(tmpl.id).zfill(6),
            }
            try:
                resp = self.client.product.create(product_payload)
                if resp and resp.get('status') == 'success':
                    tmpl.product_variant_id.fulfillment_product_id = resp['data'].get('product_id')
                    _logger.info("[Fulfillment][Create] Remote product %s -> %s",
                                 tmpl.name, tmpl.fulfillment_product_id)
            except Exception as e:
                _logger.error("[Fulfillment][Create] Exception creating product %s: %s",
                              tmpl.name, e)
        return tmpl.fulfillment_product_id


# ================== Mapper ==================
class WarehouseMapper:
    def __init__(self, env):
        self.env = env

    def resolve(self, picking):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        my_fulfillment_id = profile.fulfillment_profile_id if profile else None

        if picking.picking_type_code == 'incoming':
            return (
                picking.partner_id.fulfillment_contact_warehouse_id,
                self._ext_id_from_location(picking.location_dest_id),
                picking.partner_id.fulfillment_profile_id,
                my_fulfillment_id
            )
        if picking.picking_type_code == 'outgoing':
            return (
                self._ext_id_from_location(picking.location_id),
                picking.partner_id.fulfillment_contact_warehouse_id,
                my_fulfillment_id,
                picking.partner_id.fulfillment_profile_id
            )
        if picking.picking_type_code == 'internal':
            return (
                self._ext_id_from_location(picking.location_id),
                self._ext_id_from_location(picking.location_dest_id),
                my_fulfillment_id,
                my_fulfillment_id
            )
        return None, None, None, None


    def _ext_id_from_location(self, location):
        if not location:
            return None
        warehouse = self.env['stock.warehouse'].search([
            ('lot_stock_id', '=', location.id)
        ], limit=1)
        return warehouse.fulfillment_warehouse_id if warehouse else None


# ================== Main Model ==================
class FulfillmentTransfers(models.Model):
    _inherit = 'stock.picking'


                
    # ===== fields =====
    fulfillment_partner_id = fields.Many2one(
        'fulfillment.partners',
        string="Fulfillment Partner",
        index=True,
        ondelete='set null'
    )
    
    fulfillment_transfer_id = fields.Char(
        string="Transfer ID",
        default="Empty",
        help="Fulfillment transfer ID",
        readonly=True
    )
    
    fulfillment_transfer_owner_id = fields.Char(
        string="Resource owner",
        default="Empty",
        help="Fulfillment owner ID",
        readonly=True
    )
    
    fulfillment_warehouse_id = fields.Char(
        string="Fulfillment Warehouse ID",
        help="External Warehouses ID",
        copy=False
    )

    # ===== Onchange handler ===== 
    
    @api.onchange('state')
    def _onchange_state(self):

        old_state = self._origin.state if self._origin else 'draft (NEW)'        
        new_state = self.state
        
        if old_state != new_state:
            _logger.info(
                "[STOCK.PICKING][STATE_CHANGE] Трансфер '%s' изменил состояние: %s → %s", 
                self.name or "Новый трансфер",
                old_state, 
                new_state
            )
        return {}
    
    
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

        _logger.info("[STOCK.PICKING][ONCHANGE] partner_id → %s", self.partner_id.display_name)

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
                    
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info("[Fulfillment][CREATE] Creating %d new stock.picking records", len(vals_list))

        # создание записей
        records = super(FulfillmentTransfers, self).create(vals_list)

        # проверка контекста — пропустить push
        if not self.env.context.get("skip_fulfillment_push"):
            for rec in records:
                _logger.info("[Fulfillment][CREATE] Record created: %s (id=%s, name=%s, transfer_id=%s)",
                            rec.picking_type_code, rec.id, rec.name, rec.fulfillment_transfer_id)

                if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
                    existing = self.search([
                        ("name", "=", rec.name),
                        ("fulfillment_transfer_id", "!=", "Empty")
                    ], limit=1)

                    if existing:
                        _logger.warning("[Fulfillment][CREATE] Skipping push for %s — already has transfer_id=%s",
                                        rec.name, existing.fulfillment_transfer_id)
                    else:
                        _logger.info("[Fulfillment][CREATE] Pushing %s to Fulfillment API...", rec.name)
                        rec._push_to_fulfillment_api()
                else:
                    _logger.info("[Fulfillment][CREATE] Skip push — already linked to Fulfillment (%s)",
                                rec.fulfillment_transfer_id)

        return records


    def _log_state_transition(self, rec, old_state, new_state, source):
        if old_state == new_state:
            return

        # ЛОГ
        _logger.info(
            "[Fulfillment][STATE CHANGE][%s] %s: %s → %s",
            source or "write",
            rec.name or ("id=%s" % rec.id),
            old_state,
            new_state
        )

        # ЕСЛИ НЕТ transfer_id → пуш невозможен
        if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
            _logger.warning(
                "[Fulfillment][STATE CHANGE] Skipped API push — no fulfillment_transfer_id"
            )
            return

        # ОТПРАВЛЯЕМ СТАТУС В API
        rec._push_status_update(new_state)


    # правка write (оставляем твою логику, добавляем debug и сравнение old/new)
    def write(self, vals):
        
        _logger.debug("[Fulfillment][WRITE CALLED] ids=%s vals=%s model=%s", self.ids, vals, self._name)


        if self.env.context.get("skip_fulfillment_push"):
            return super().write(vals)
        
        
        old_states = {rec.id: rec.state for rec in self}

        res = super(FulfillmentTransfers, self).write(vals)

        for rec in self:
            old_state = old_states.get(rec.id)
            new_state = rec.state
            # единая логика логирования
            self._log_state_transition(rec, old_state, new_state, "write")

            # --- твоя существующая логика (копируй как есть) ---
            _logger.info("[Fulfillment][WRITE] Updated %s with vals=%s", rec.name, vals)

            trigger_fields = {'move_ids', 'state', 'partner_id', 'location_id', 'location_dest_id'}
            has_contact = rec.partner_id and getattr(rec.partner_id, "fulfillment_contact_id", False)

            if rec.picking_type_code == 'outgoing' and has_contact:
                if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
                    _logger.info("[Fulfillment][WRITE] Outgoing picking with contact, creating transfer in API")
                    # возможно, желаешь асинхронно — но оставляем синхронно
                    rec._push_to_fulfillment_api()
                elif trigger_fields.intersection(vals.keys()):
                    _logger.info("[Fulfillment][WRITE] Detected relevant change — pushing update to API")
                    rec._push_to_fulfillment_api()
            else:
                _logger.debug("[Fulfillment][WRITE] Skipping push — no contact or not outgoing")

        return res

    # Перехватываем action методы — паттерн: save old state, вызвать super(), залогировать
    def action_confirm(self):
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_confirm()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_confirm")
        return res

    def action_assign(self):
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_assign()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_assign")
        return res

    def button_validate(self):
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).button_validate()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "button_validate")
        return res

    # Иногда используется action_done / _action_done — пробуем переопределить безопасно
    def action_done(self):
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_done()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_done")
        return res

    def action_cancel(self):
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_cancel()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_cancel")
        return res

    def _push_status_update(self, new_state):
        if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
            _logger.warning("Skip status push – no transfer_id yet")
            return

        client = self.fulfillment_api
        if not client:
            return

        payload = {"status": new_state}

        try:
            client.transfer.update(self.fulfillment_transfer_id, payload)
            _logger.info("[Fulfillment][API] Status pushed: %s -> %s",
                        self.fulfillment_transfer_id, new_state)
        except Exception as e:
            _logger.error("[Fulfillment][API ERROR] Failed status push: %s", e)




    @api.onchange('state')
    def _onchange_state(self):
        # 1. Получаем старое состояние из self._origin
        # self._origin содержит значения полей до их изменения в форме.
        # Проверяем на None, если это новая (еще не сохраненная) запись.
        old_state = self._origin.state if self._origin else 'draft (NEW)'
        
        # 2. Получаем новое состояние из текущего объекта self
        new_state = self.state
        
        # 3. Выводим информацию в лог, если состояние изменилось
        if old_state != new_state:
            _logger.info(
                "[STOCK.PICKING][STATE_CHANGE] Трансфер '%s' изменил состояние: %s → %s", 
                self.name or "Новый трансфер",
                old_state, 
                new_state
            )
        
        # onchange должен вернуть словарь (или None), а не False, для корректной работы
        return {}
    
    
    # ===== helpers =====
    def _get_partner_fulfillment_profile_id(self, partner):
        """
        Возвращает fulfillment_profile_id партнёра (строка) или None.
        Логика:
         - если у партнёра есть linked_warehouse_id -> берём склад -> fulfillment_owner_id
         - иначе ищем запись в fulfillment.partners по partner_id
         - если всё это не даёт результата, возвращаем None
        """
        
        _logger.error("🔥🔥🔥 PUSH CALLED FOR: %s", self.name)

        if not partner:
            return None

        # 1) linked warehouse -> fulfillment owner (fulfillment.partners)
        try:
            linked_wh = getattr(partner, "linked_warehouse_id", None)
            if linked_wh:
                owner = getattr(linked_wh, "fulfillment_owner_id", None)
                if owner and getattr(owner, "profile_id", None):
                    return owner.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment][_get_partner_fulfillment_profile_id] linked_warehouse check failed: %s", e)

        # 2) direct fulfillment.partners record (partner_id)
        try:
            fp = self.env['fulfillment.partners'].search([('partner_id', '=', partner.id)], limit=1)
            if fp and getattr(fp, "profile_id", None):
                return fp.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment][_get_partner_fulfillment_profile_id] search failed: %s", e)

        # 3) fallback: maybe partner stores external id of contact warehouse (fulfillment_contact_warehouse_id)
        try:
            contact_wh_ext = getattr(partner, "fulfillment_contact_warehouse_id", None)
            if contact_wh_ext:
                wh = self.env['stock.warehouse'].search([('fulfillment_warehouse_id', '=', contact_wh_ext)], limit=1)
                if wh and getattr(wh, "fulfillment_owner_id", None):
                    owner = wh.fulfillment_owner_id
                    if owner and getattr(owner, "profile_id", None):
                        return owner.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment][_get_partner_fulfillment_profile_id] contact_warehouse check failed: %s", e)

        return None


    # ----- Rewritten _push_to_fulfillment_api -----
    def _push_to_fulfillment_api(self):
        self.ensure_one()
        _logger.info(
            "[Fulfillment][PUSH] Called for picking: %s (id=%s, transfer_id=%s, type=%s, state=%s, partner=%s)",
            self.name, self.id, self.fulfillment_transfer_id, self.picking_type_code, self.state,
            self.partner_id.name if self.partner_id else "None"
        )

        # Проверяем move_ids - если пустые, возможно нужно подождать
        if not self.move_ids:
            _logger.warning("[Fulfillment][PUSH] Skip — no move_ids for %s", self.name)
            # Но возможно это временно, проверяем есть ли связанные заказы
            if self.sale_id and self.sale_id.order_line:
                _logger.info("[Fulfillment][PUSH] But has sale order with lines, moves may be created later")
            return

        # Проверим наличие API клиента
        client = self.fulfillment_api
        if not client:
            _logger.error("[Fulfillment][PUSH] API client unavailable for %s", self.name)
            return

        # Items
        items = FulfillmentItemBuilder(client).build_items(self.move_ids)
        if not items:
            _logger.debug("[Fulfillment] No items to sync for %s", self.name)
            return

        # Profile (наш)
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found in _push_to_fulfillment_api")
            return
        my_fulfillment_id = getattr(profile, "fulfillment_profile_id", None)

        # Default placeholders
        warehouse_out_id = None
        warehouse_in_id = None
        fulfillment_out = None
        fulfillment_in = None

        # Safely get partner
        partner = self.partner_id if getattr(self, "partner_id", False) else None

        # --- Determine per picking type ---
        if self.picking_type_code == 'incoming':
            _logger.info("[Fulfillment] Processing INCOMING for %s", self.name)
            warehouse_out_id = getattr(partner, "fulfillment_warehouse_id", None)
            
            dest_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_dest_id.id)], limit=1)
            warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None) if dest_wh else None

            fulfillment_out = self._get_partner_fulfillment_profile_id(partner)
            fulfillment_in = my_fulfillment_id

        elif self.picking_type_code == 'outgoing':
            _logger.info("[Fulfillment] Processing OUTGOING for %s", self.name)
            # откуда
            src_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_id.id)], limit=1)
            warehouse_out_id = getattr(src_wh, "fulfillment_warehouse_id", None) if src_wh else None
            
            # кому - ВАЖНО: берем fulfillment_warehouse_id, а не имя!
            warehouse_in_id = getattr(self.partner_id, "fulfillment_warehouse_id", None)

            fulfillment_out = my_fulfillment_id
            fulfillment_in = self._get_partner_fulfillment_profile_id(self.partner_id)

            # Детальная отладка
            _logger.info("[Fulfillment][DEBUG] Outgoing transfer details:")
            _logger.info("  - partner: %s", self.partner_id.name)
            _logger.info("  - fulfillment_contact_id: %s", getattr(self.partner_id, "fulfillment_contact_id", None))
            _logger.info("  - fulfillment_warehouse_id: %s", warehouse_in_id)
            _logger.info("  - fulfillment_profile_id: %s", fulfillment_in)

        elif self.picking_type_code == 'internal':
            _logger.info("[Fulfillment] Processing INTERNAL for %s", self.name)
            src_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_id.id)], limit=1)
            dest_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_dest_id.id)], limit=1)

            warehouse_out_id = getattr(src_wh, "fulfillment_warehouse_id", None) if src_wh else None
            warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None) if dest_wh else None

            fulfillment_out = my_fulfillment_id
            fulfillment_in = my_fulfillment_id

        else:
            _logger.warning("[Fulfillment] Unknown picking_type_code=%s for %s", self.picking_type_code, self.name)

        # --- Build payload через адаптер ---
        payload = PickingAdapter.to_api_payload(self, items, warehouse_out_id, warehouse_in_id)

        # --- ВАЖНО: Добавляем контакты для outgoing трансферов ---
        if self.picking_type_code == 'outgoing' and self.partner_id:
            contact_id = getattr(self.partner_id, "fulfillment_contact_id", None)
            if contact_id:
                payload["contacts"] = [{"contactId": str(contact_id), "role": "DELIVERY"}]
                _logger.info("[Fulfillment] Added contact %s to payload for partner %s", contact_id, self.partner_id.name)
            else:
                _logger.warning("[Fulfillment] No fulfillment_contact_id for partner %s", self.partner_id.name)
                self._debug_partner_contacts(self.partner_id)

        # --- Далее пушим transfer ---
        if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
            response = client.transfer.create(payload)
        else:
            response = client.transfer.update(self.fulfillment_transfer_id, payload)

        # Добавляем fulfillment профили
        if fulfillment_out:
            payload['fulfillment_out'] = fulfillment_out
        if fulfillment_in:
            payload['fulfillment_in'] = fulfillment_in

        # Детальное логирование финального payload
        _logger.info("[Fulfillment][Final Payload for %s]", self.picking_type_code)
        _logger.info("  - reference: %s", payload.get("reference"))
        _logger.info("  - warehouse_out: %s", payload.get("warehouse_out"))
        _logger.info("  - warehouse_in: %s", payload.get("warehouse_in"))
        _logger.info("  - contacts: %s", payload.get("contacts", []))
        _logger.info("  - fulfillment_out: %s", payload.get("fulfillment_out"))
        _logger.info("  - fulfillment_in: %s", payload.get("fulfillment_in"))
        _logger.info("  - items count: %s", len(payload.get("items", [])))

        # --- Sync ---
        try:
            # Создаём трансфер только если ещё нет связанного ID
            if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
                _logger.info("[Fulfillment][CREATE] Sending transfer to API with %d contacts", 
                            len(payload.get("contacts", [])))
                response = client.transfer.create(payload)
                
                _logger.info("[Fulfillment][CREATE] API response received, contacts in response: %s", 
                            response.get('contacts', []) if isinstance(response, dict) else 'Unknown')

                if isinstance(response, dict):
                    vals = {
                        "fulfillment_transfer_id": response.get("transfer_id"),
                        "name": response.get("reference") or self.name,
                        "state": response.get("status") or "draft",
                    }

                    if response.get("fulfillment_in"):
                        vals["fulfillment_transfer_owner_id"] = response["fulfillment_in"]

                    self.write(vals)
                    _logger.info(
                        "[Fulfillment][CREATE] Updated transfer %s with fulfillment_transfer_id=%s",
                        self.name, response.get("transfer_id")
                    )
                else:
                    _logger.warning("[Fulfillment][CREATE] Unexpected API response type: %s", type(response))

            else:
                # Если есть ID — обновляем на API
                response = client.transfer.update(self.fulfillment_transfer_id, payload)
                _logger.info("[Fulfillment][UPDATE] Pushed transfer update for %s", self.fulfillment_transfer_id)

        except Exception as e:
            _logger.error("[Fulfillment][ERROR] Failed to sync transfer %s: %s", self.name, e, exc_info=True)

    def _debug_partner_contacts(self, partner):
        """Метод для отладки контактов партнера"""
        _logger.info("[Fulfillment][DEBUG] Checking contacts for partner %s:", partner.name)
        
        # 1. Прямо из партнера
        contact_id = getattr(partner, 'fulfillment_contact_id', None)
        _logger.info("  - Direct fulfillment_contact_id: %s", contact_id)
        
        # 2. Через linked_warehouse
        if hasattr(partner, 'linked_warehouse_id') and partner.linked_warehouse_id:
            wh_contact = getattr(partner.linked_warehouse_id, 'fulfillment_contact_id', None)
            _logger.info("  - From linked_warehouse: %s", wh_contact)
        
        # 3. Через fulfillment.partners
        try:
            fp = self.env['fulfillment.partners'].search([('partner_id', '=', partner.id)], limit=1)
            if fp:
                fp_contact = getattr(fp, 'fulfillment_contact_id', None)
                _logger.info("  - From fulfillment.partners: %s", fp_contact)
        except Exception as e:
            _logger.debug("  - Error searching fulfillment.partners: %s", e)
            
            
    # ----- API client -----
    @property
    def fulfillment_api(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment][Profile not found]")
            return None
        return FulfillmentAPIClient(profile)



    # ----- Загрузка трансферов -----
    @api.model
    def import_transfers(self, fulfillment_id=None, page=1, limit=50):
        """Загружает трансферы из Fulfillment API"""
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found, load_transfers пропущен")
            return False

        client = FulfillmentAPIClient(profile)

        try:
            response = client.transfer.list(
                fulfillment_id=fulfillment_id,
                page=page,
                limit=limit
            )
        except Exception as e:
            _logger.error(f"[Fulfillment] Ошибка при запросе transfer.list: {e}")
            return False

        if not response or response.get("status") != "success":
            _logger.warning("[Fulfillment] Некорректный ответ при загрузке transfers: %s", response)
            return False

        transfers = response.get("data", [])
        for transfer in transfers:
            self._import_transfer(transfer)

        return True

    def _import_transfer(self, transfer):
        """Импорт одного transfer в Odoo с корректным применением статуса."""
        remote_id = str(transfer.get("transfer_id"))
        reference = transfer.get("reference")
        wh_in_ext = transfer.get("warehouse_in")
        wh_out_ext = transfer.get("warehouse_out")
        status = transfer.get("status")
        contacts = transfer.get("contacts") or []

        if not remote_id:
            _logger.warning("[Fulfillment] Transfer без ID пропущен: %s", transfer)
            return False

        # 🔍 Ищем склады по внешнему ID
        warehouse_in = self.env["stock.warehouse"].search(
            [("fulfillment_warehouse_id", "=", wh_in_ext)], limit=1
        )
        warehouse_out = self.env["stock.warehouse"].search(
            [("fulfillment_warehouse_id", "=", wh_out_ext)], limit=1
        )

        location_id = warehouse_out.lot_stock_id.id if warehouse_out else False
        location_dest_id = warehouse_in.lot_stock_id.id if warehouse_in else False

        # ============================================================
        # Обработка контактов
        # ============================================================
        partner_id = False
        contact_data = next(
            (c for c in contacts if c.get("role") == "CUSTOMER"),
            (contacts[0] if contacts else None)
        )

        if contact_data:
            cid = contact_data.get("contactId")
            contact_info = contact_data.get("contact") or {}
            cname = contact_info.get("name") or "NoName"
            cphone = contact_info.get("phone") or ""
            cemail = contact_info.get("email") or ""

            partner = self.env["res.partner"].search(
                [("fulfillment_contact_id", "=", cid)], limit=1
            )

            if not partner:
                domain = [("name", "=", cname)]
                if cphone:
                    domain.append(("phone", "=", cphone))
                partner = self.env["res.partner"].search(domain, limit=1)

            if not partner:
                partner = self.env["res.partner"].create({
                    "name": cname,
                    "phone": cphone,
                    "email": cemail,
                    "fulfillment_contact_id": cid,
                })
                _logger.info("[Fulfillment][Import] Создан новый контакт: %s (%s)", cname, cid)
            else:
                if not partner.fulfillment_contact_id and cid:
                    partner.fulfillment_contact_id = cid

            partner_id = partner.id

        # fallback partner
        if not partner_id:
            partner_fallback = self.env["res.partner"].search(
                [("fulfillment_warehouse_id", "=", wh_in_ext)], limit=1
            )
            partner_id = partner_fallback.id if partner_fallback else False

        # ============================================================
        # Создание/обновление picking
        # ============================================================
        picking = self.search([("fulfillment_transfer_id", "=", remote_id)], limit=1)
        picking_type_id = self._map_type(transfer)
        picking_type = self.env["stock.picking.type"].browse(picking_type_id)
        type_code = picking_type.code if picking_type else "unknown"

        wh_code = warehouse_out.code or warehouse_in.code or "WH"
        type_short = {"incoming": "IN", "outgoing": "OUT", "internal": "INT"}.get(type_code, "UNK")
        hash_part = str(remote_id)[:8]
        name = f"[F] {wh_code}/{type_short}/{hash_part}"

        vals = {
            "name": name,
            "fulfillment_transfer_id": remote_id,
            "picking_type_id": picking_type_id,
            "partner_id": partner_id,
            "location_id": location_id,
            "location_dest_id": location_dest_id,
        }

        if picking:
            picking.write(vals)
            _logger.info("[Fulfillment] Обновлён picking %s из transfer %s", picking.name, remote_id)
        else:
            picking = self.create(vals)
            _logger.info("[Fulfillment] Создан новый picking %s из transfer %s", picking.name, remote_id)
            
        # ============================================================
        # Создание/обновление товарных позиций
        # ============================================================
        items = transfer.get("items", [])
        if items:
            Move = self.env["stock.move"]
            ProductTmpl = self.env["product.template"]

            for item in items:
                product_data = item.get("product") or {}
                fulfillment_product_id = item.get("product_id") or product_data.get("product_id")

                prod_name = product_data.get("name") or "Unnamed Product"
                sku = product_data.get("sku") or f"F-{fulfillment_product_id}"
                barcode = product_data.get("barcode")

                product_tmpl = False
                if fulfillment_product_id:
                    product_tmpl = ProductTmpl.search(
                        [("fulfillment_product_id", "=", fulfillment_product_id)], limit=1
                    )
                if not product_tmpl and sku:
                    product_tmpl = ProductTmpl.search([("default_code", "=", sku)], limit=1)

                if not product_tmpl:
                    create_vals = {
                        "name": prod_name,
                        "type": "consu",
                        "is_storable": True,
                        "uom_id": self.env.ref("uom.product_uom_unit").id,
                        "uom_po_id": self.env.ref("uom.product_uom_unit").id,
                        "default_code": sku,
                        "fulfillment_product_id": fulfillment_product_id,
                    }
                    if barcode:
                        create_vals["barcode"] = barcode

                    product_tmpl = ProductTmpl.create(create_vals)
                    if not product_tmpl.product_variant_ids:
                        product_tmpl._create_variant_ids()
                        product_tmpl.flush_recordset()

                    _logger.info("[Fulfillment][Import] Создан продукт '%s' (fulfillment_product_id=%s)", prod_name, fulfillment_product_id)
                else:
                    if not product_tmpl.fulfillment_product_id and fulfillment_product_id:
                        product_tmpl.fulfillment_product_id = fulfillment_product_id

                product_id = product_tmpl.product_variant_id.id
                quantity = float(item.get("quantity") or 0.0)

                move_vals = {
                    "name": prod_name,
                    "product_id": product_id,
                    "product_uom_qty": quantity,
                    "product_uom": product_tmpl.uom_id.id,
                    "picking_id": picking.id,
                    "location_id": location_id,
                    "location_dest_id": location_dest_id,
                    "state": "draft",
                }

                existing_move = Move.search([("picking_id", "=", picking.id), ("product_id", "=", product_id)], limit=1)
                if existing_move:
                    existing_move.write(move_vals)
                else:
                    Move.create(move_vals)

   
        try:
            picking._apply_status(status)
            _logger.info("[Fulfillment] Статус picking %s обновлён → %s", picking.name, status)
        except Exception as e:
            _logger.warning("[Fulfillment] Ошибка установки статуса %s для %s: %s", status, picking.name, e)

                        

        return picking


    def _apply_status(self, status):
        self.ensure_one()

        if status in ("confirmed", "assigned", "done") and self.state == "draft":
            self.action_confirm()

        if status in ("assigned", "done") and self.state in ("draft", "confirmed"):
            self.action_assign()

        if status == "done" and self.state != "done":
            # Лучше button_validate(), так как оно вызывает проверки
            self.button_validate()

        if status == "cancel" and self.state != "cancel":
            self.action_cancel()



    def _import_fulfillment_contact(self, contact_data):
        """
        Создаёт или обновляет контакт по данным Fulfillment.
        Возвращает partner_id.
        """
        if not contact_data:
            return False

        cid = contact_data.get("contactId")
        info = contact_data.get("contact") or {}

        name = info.get("name") or "NoName"
        phone = info.get("phone") or ""
        email = info.get("email") or ""

        Partner = self.env["res.partner"]

        # 1) Ищем по fulfillment_contact_id
        partner = Partner.search([("fulfillment_contact_id", "=", cid)], limit=1)

        # 2) fallback: ищем по имени+телефону
        if not partner:
            domain = [("name", "=", name)]
            if phone:
                domain.append(("phone", "=", phone))
            partner = Partner.search(domain, limit=1)

        # 3) Создаём нового
        if not partner:
            partner = Partner.create({
                "name": name,
                "phone": phone,
                "email": email,
                "fulfillment_contact_id": cid,
            })
            _logger.info(
                "[Fulfillment][Import] Создан контакт %s (%s)",
                name, cid
            )
            return partner.id

        # 4) Если нашли — обновляем внешний ID (если пустой)
        if not partner.fulfillment_contact_id and cid:
            partner.fulfillment_contact_id = cid

        # 5) Можно обновлять имя / телефон / email, если хочешь:
        # partner.write({"phone": phone, "email": email})

        return partner.id


    def _map_type(self, transfer):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            return False

        my_fulfillment_id = profile.fulfillment_profile_id
        wh_in = transfer.get("fulfillment_in")
        wh_out = transfer.get("fulfillment_out")

        if wh_in == wh_out:
            op_code = "internal"
        elif wh_in == my_fulfillment_id:
            op_code = "incoming"
        elif wh_out == my_fulfillment_id:
            op_code = "outgoing"
        else:
            op_code = transfer.get("type") or "internal"

        _logger.info(
            "[Fulfillment][_map_type] transfer=%s wh_in=%s wh_out=%s my=%s => %s",
            transfer.get("id"), wh_in, wh_out, my_fulfillment_id, op_code
        )

        op_type = self.env["stock.picking.type"].search([("code", "=", op_code)], limit=1)
        return op_type.id if op_type else False

    
    

    # ----- Определение типа -----
    def _get_operation_type(self, picking):
        if not picking.picking_type_id:
            _logger.warning("[FULFILLMENT] Picking %s has no picking_type_id", picking.name)
            return None

        op_type = picking.picking_type_id
        _logger.info(
            "[FULFILLMENT] Picking %s -> operation_type_id=%s (%s, code=%s)",
            picking.name,
            op_type.id,
            op_type.name,
            op_type.code,
        )
        return op_type