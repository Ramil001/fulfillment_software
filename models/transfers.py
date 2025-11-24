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

        records = super(FulfillmentTransfers, self).create(vals_list)
        for rec in records:
            _logger.info("[Fulfillment][CREATE] Record created: %s (id=%s, name=%s, transfer_id=%s)",
                        rec.picking_type_code, rec.id, rec.name, rec.fulfillment_transfer_id)

            # Проверяем, был ли уже создан трансфер в Fulfillment
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

    def write(self, vals):
        res = super(FulfillmentTransfers, self).write(vals)

        for rec in self:
            _logger.info("[Fulfillment][WRITE] Updated %s with vals=%s", rec.name, vals)

            trigger_fields = {'move_ids', 'state', 'partner_id', 'location_id', 'location_dest_id'}

            # Проверяем, есть ли партнер и fulfillment_contact_id
            has_contact = rec.partner_id and getattr(rec.partner_id, "fulfillment_contact_id", False)

            # Если есть внешний ID или партнёр теперь готов для outgoing
            if rec.picking_type_code == 'outgoing' and has_contact:
                # Для новых записей без transfer_id — пушим создание
                if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
                    _logger.info("[Fulfillment][WRITE] Outgoing picking with contact, creating transfer in API")
                    rec._push_to_fulfillment_api()
                # Для существующих — пушим обновление
                elif trigger_fields.intersection(vals.keys()):
                    _logger.info("[Fulfillment][WRITE] Detected relevant change — pushing update to API")
                    rec._push_to_fulfillment_api()
            else:
                _logger.debug("[Fulfillment][WRITE] Skipping push — no contact or not outgoing")

        return res


    
    
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
        """Импорт входящего Transfer из Fulfillment в Odoo."""

        picking_obj = self.env["stock.picking"]
        partner_obj = self.env["res.partner"]
        product_obj = self.env["product.product"]

        external_id = transfer.get("id")
        transfer_type = transfer.get("type")
        contacts = transfer.get("contacts") or []
        items = transfer.get("items") or []

        if not external_id:
            _logger.error("[Fulfillment][IMPORT] Transfer без ID — пропуск")
            return False

        # Проверяем существующий
        picking = picking_obj.search([("fulfillment_transfer_id", "=", external_id)], limit=1)
        if picking:
            _logger.info(f"[Fulfillment][IMPORT] Transfer {external_id} уже существует → {picking.name}")
            return picking

        # ============================================================
        #               ИМПОРТ КОНТАКТА CUSTOMER
        # ============================================================
        customer_contact = next(
            (c for c in contacts if c.get("role") == "CUSTOMER"),
            None
        )

        partner_id = False
        if customer_contact:
            partner_id = self._import_fulfillment_contact(customer_contact)

        # ============================================================
        #         Определяем тип операции и Picking Type
        # ============================================================

        picking_type_code = "incoming" if transfer_type == "INBOUND" else "outgoing"

        picking_type = self.env["stock.picking.type"].search(
            [("code", "=", picking_type_code)],
            limit=1
        )

        if not picking_type:
            raise UserError(f"Не найден picking type для типа {picking_type_code}")

        # ============================================================
        #          Создаём stock.picking (пустой, без move)
        # ============================================================

        vals = {
            "picking_type_id": picking_type.id,
            "origin": transfer.get("reference") or external_id,
            "scheduled_date": transfer.get("dateCreated") or fields.Datetime.now(),
            "fulfillment_transfer_id": external_id,
        }

        if partner_id:
            vals["partner_id"] = partner_id

        picking = picking_obj.create(vals)

        _logger.info(f"[Fulfillment][IMPORT] Создан Transfer {external_id} → {picking.name}")

        # ============================================================
        #                 Создаём Stock Moves
        # ============================================================

        for line in items:
            sku = line.get("sku")
            qty = line.get("quantity") or 0

            if not sku:
                _logger.error(f"[Fulfillment][IMPORT] Пропуск строки — нет SKU")
                continue

            product = product_obj.search([("default_code", "=", sku)], limit=1)

            if not product:
                _logger.error(f"[Fulfillment][IMPORT] Пропуск — SKU {sku} не найден в Odoo")
                continue

            move_vals = {
                "name": product.name,
                "product_id": product.id,
                "product_uom": product.uom_id.id,
                "product_uom_qty": qty,
                "picking_id": picking.id,
                "location_id": picking.location_id.id,
                "location_dest_id": picking.location_dest_id.id,
            }

            self.env["stock.move"].create(move_vals)

        picking.action_confirm()

        _logger.info(f"[Fulfillment][IMPORT] Transfer {external_id} полностью импортирован")

        return picking




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