# -*- coding: utf-8 -*-
import json
from odoo import models, api, fields, _
import logging
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)


# ================== Strategy: Transfer Mappers ==================
class BaseTransferMapper:
    def build(self, picking, items, from_warehouse_id, to_warehouse_id, 
              fulfillment_out, fulfillment_in, contacts=None):
        raise NotImplementedError


class IncomingTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts=None):
        return {
            "transfer_type": "incoming",
            "warehouse_out": warehouse_out,
            "warehouse_in": warehouse_in,
            "fulfillment_out": fulfillment_out,
            "fulfillment_in": fulfillment_in,
            "reference": picking.name or picking.origin or "Odoo",
            "items": items,
            "contacts": contacts or [],
        }


class OutgoingTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts=None):
        return {
            "transfer_type": "outgoing",
            "warehouse_out": warehouse_out,
            "warehouse_in": warehouse_in,
            "fulfillment_out": fulfillment_out,
            "fulfillment_in": fulfillment_in,
            "reference": picking.name or picking.origin or "Odoo",
            "items": items,
            "contacts": contacts or [],
        }


class InternalTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts=None):
        return {
            "transfer_type": "internal",
            "warehouse_out": warehouse_out,
            "warehouse_in": warehouse_in,
            "fulfillment_out": fulfillment_out,
            "fulfillment_in": fulfillment_in,
            "reference": picking.name or "00000",
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
    def to_api_payload(cls, picking, items, warehouse_out, warehouse_in, 
                       fulfillment_out, fulfillment_in, contacts=None):
        _logger.info("[to_api_payload]")
        mapper = cls._strategies.get(picking.picking_type_code)
        if not mapper:
            raise ValueError(f"Unsupported picking type {picking.picking_type_code}")
        return mapper.build(picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts)

class FulfillmentItemBuilder:
    def __init__(self, client):
        self.client = client

    def build_items(self, moves):
        _logger.info("[build_items]")
        items = []
        for move in moves:
            product_tmpl = move.product_id.product_tmpl_id
            fulfillment_id = self._ensure_remote_product(product_tmpl)
            if not fulfillment_id:
                continue

            items.append({
                "product_id": fulfillment_id,
                "quantity": float(move.product_uom_qty or 0.0),
                "unit": move.product_uom.name if move.product_uom else 'Units',
            })
        return items

    def _ensure_remote_product(self, tmpl):
        _logger.info("[_ensure_remote_product]")
        if not getattr(tmpl, "fulfillment_product_id", None):
            product_payload = {
                "name": tmpl.name,
                "sku": tmpl.default_code or f"SKU-{tmpl.id}",
                "barcode": tmpl.barcode or str(tmpl.id).zfill(6),
            }
            try:
                resp = self.client.product.create(product_payload)
                if resp and resp.get("data", {}).get("id"):
                    tmpl.product_variant_id.fulfillment_product_id = resp["data"].get("id")
                    _logger.info("[Fulfillment][Create] Remote product %s -> %s",
                                 tmpl.name, tmpl.fulfillment_product_id)
            except Exception as e:
                _logger.error("[Fulfillment][Create] Exception creating product %s: %s", tmpl.name, e)
        return tmpl.fulfillment_product_id


class FulfillmentTransfers(models.Model):
    _inherit = 'stock.picking'

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



    def _fetch_product_from_api(self, fulfillment_product_id):
        """Загружает продукт из API по ID"""
        _logger.info("[_fetch_product_from_api]")
        if not fulfillment_product_id:
            return None

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found for product fetch")
            return None

        client = FulfillmentAPIClient(profile)
        try:
            response = client.product.get(fulfillment_product_id)
            return response.get("data")
        except Exception as e:
            _logger.error("[Fulfillment] Failed to fetch product %s: %s", fulfillment_product_id, e)
            return None


    @api.onchange('partner_id')
    def _onchange_partner(self):
        _logger.info("[_onchange_partner]")
        """Срабатывает при изменении партнёра в stock.picking"""
        if not self.partner_id:
            return


        message = f"В документе {self.name or '(новый документ)'} изменён партнёр на {self.partner_id.display_name}."
        payload = {
            "type": "fulfillment_notification",
            "payload": {
                "message": message,
                "title": "Изменение партнёра",
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
        _logger.info("[create]")
        _logger.info(f"[Fulfillment][Transfer][Create]: [self]: {self} | [vals_list]: {vals_list}")

        try:
            records = super(FulfillmentTransfers, self).create(vals_list)
            _logger.info("[Fulfillment] Records created: %s", records.ids)
        except Exception as e:
            _logger.error("[Fulfillment] CRASH during super().create: %s", str(e), exc_info=True)
            raise

        if not self.env.context.get("skip_fulfillment_push"):
            for rec in records:
                fulfillment_transfer_id = rec.fulfillment_transfer_id
                _logger.info("[Fulfillment] Processing %s (current f_id: '%s')", rec.name, fulfillment_transfer_id)

                
                if not fulfillment_transfer_id or fulfillment_transfer_id == "Empty":
                    _logger.info("[Fulfillment] New record detected. Triggering API Push for %s", rec.name)
                    try:
                        rec._push_to_fulfillment_api()
                    except Exception as e:
                        _logger.error("[Fulfillment] Error during push for %s: %s", rec.name, str(e), exc_info=True)
                
            
                else:
                    existing = self.search([
                        ("fulfillment_transfer_id", "=", fulfillment_transfer_id),
                        ("id", "!=", rec.id)
                    ], limit=1)
                    
                    if not existing:
                        _logger.info("[Fulfillment] Unique UUID detected. Updating %s in API", rec.name)
                        rec._push_to_fulfillment_api()
                    else:
                        _logger.warning("[Fulfillment] SKIP: UUID '%s' is already used by record %s", fulfillment_transfer_id, existing.id)

        return records

    def _log_state_transition(self, rec, old_state, new_state, source):
        """Логирование изменения статуса"""
        _logger.info("[_log_state_transition]")
        if old_state == new_state:
            return

        _logger.info(
            "[Fulfillment][STATE CHANGE][%s] %s: %s → %s",
            source, rec.name or f"id={rec.id}", old_state, new_state
        )

        if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
            _logger.warning("[Fulfillment][STATE CHANGE] Skipped API push — no fulfillment_transfer_id")
            return

        rec._push_status_update(new_state)

    def write(self, vals):
        _logger.info("[write]")
        if self.env.context.get("skip_fulfillment_push"):
            return super().write(vals)
        
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).write(vals)

        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "write")

            trigger_fields = {'move_ids', 'state', 'partner_id', 'location_id', 'location_dest_id'}
            has_contact = rec.partner_id and getattr(rec.partner_id, "fulfillment_contact_id", False)

            if rec.picking_type_code == 'outgoing' and has_contact:
                if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
                    rec._push_to_fulfillment_api()
                elif trigger_fields.intersection(vals.keys()):
                    rec._push_to_fulfillment_api()

        return res

    def action_confirm(self):
        _logger.info("[action_confirm]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_confirm()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_confirm")
        return res

    def action_assign(self):
        _logger.info("[action_assign]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_assign()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_assign")
        return res

    def button_validate(self):
        _logger.info("[button_validate]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).button_validate()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "button_validate")
        return res

    def action_done(self):
        _logger.info("[action_done]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_done()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_done")
        return res

    def action_cancel(self):
        _logger.info("[action_cancel]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_cancel()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_cancel")
        return res

    def _push_status_update(self, new_state):
        """Отправка обновления статуса в API"""
        _logger.info("[_push_status_update]")
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

    def _get_partner_fulfillment_profile_id(self, partner):
        """Возвращает fulfillment_profile_id партнёра"""
        _logger.info("[_get_partner_fulfillment_profile_id]")
        if not partner:
            return None

       
        try:
            linked_wh = getattr(partner, "linked_warehouse_id", None)
            if linked_wh:
                owner = getattr(linked_wh, "fulfillment_owner_id", None)
                if owner and getattr(owner, "profile_id", None):
                    return owner.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment] linked_warehouse check failed: %s", e)

        
        try:
            fp = self.env['fulfillment.partners'].search([('partner_id', '=', partner.id)], limit=1)
            if fp and getattr(fp, "profile_id", None):
                return fp.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment] fulfillment.partners check failed: %s", e)

        
        try:
            contact_wh_ext = getattr(partner, "fulfillment_contact_warehouse_id", None)
            if contact_wh_ext:
                wh = self.env['stock.warehouse'].search(
                    [('fulfillment_warehouse_id', '=', contact_wh_ext)], limit=1
                )
                if wh and getattr(wh, "fulfillment_owner_id", None):
                    owner = wh.fulfillment_owner_id
                    if owner and getattr(owner, "profile_id", None):
                        return owner.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment] contact_warehouse check failed: %s", e)

        return None

    def _push_to_fulfillment_api(self):
        """Отправка трансфера в Fulfillment API"""
        
        _logger.info("[_push_to_fulfillment_api]")
        
        self.ensure_one()
        _logger.info(
            "[Fulfillment][PUSH] Called for picking: %s (id=%s, transfer_id=%s, type=%s)",
            self.name, self.id, self.fulfillment_transfer_id, self.picking_type_code
        )

        if not self.move_ids:
            _logger.warning("[Fulfillment][PUSH] Skip — no move_ids for %s", self.name)
            return

        client = self.fulfillment_api
        if not client:
            _logger.error("[Fulfillment][PUSH] API client unavailable")
            return

        items = FulfillmentItemBuilder(client).build_items(self.move_ids)
        if not items:
            _logger.debug("[Fulfillment] No items to sync for %s", self.name)
            return

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found")
            return
        
        my_fulfillment_id = getattr(profile, "fulfillment_profile_id", None)
        warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in = self._resolve_warehouses_and_profiles(
            my_fulfillment_id
        )
        

        payload = PickingAdapter.to_api_payload(
            self, items, warehouse_out_id, warehouse_in_id,
            fulfillment_out, fulfillment_in, []
        )

        if self.picking_type_code == 'outgoing' and self.partner_id:
            contact_id = getattr(self.partner_id, "fulfillment_contact_id", None)
            if contact_id:
                payload["contacts"] = [{"contact_id": str(contact_id), "role": "DELIVERY"}]
                _logger.info("[Fulfillment] Added contact %s", contact_id)
        _logger.info("[Fulfillment][PAYLOAD] Full data sent to API: %s", json.dumps(payload, indent=2))
        self._sync_transfer(client, payload)




    def _resolve_warehouses_and_profiles(self, my_fulfillment_id):
        """Определяет склады и профили на основе типа трансфера"""
        _logger.info("[_resolve_warehouses_and_profiles]")
        warehouse_out_id = None
        warehouse_in_id = None
        fulfillment_out = None
        fulfillment_in = None
        partner = self.partner_id if getattr(self, "partner_id", False) else None

        if self.picking_type_code == 'incoming':
            warehouse_out_id = getattr(partner, "fulfillment_warehouse_id", None)
            dest_wh = self.env['stock.warehouse'].search(
                [('lot_stock_id', '=', self.location_dest_id.id)], limit=1
            )
            warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None) if dest_wh else None
            fulfillment_out = self._get_partner_fulfillment_profile_id(partner)
            fulfillment_in = my_fulfillment_id

        elif self.picking_type_code == 'outgoing':
            src_wh = self.env['stock.warehouse'].search(
                [('lot_stock_id', '=', self.location_id.id)], limit=1
            )
            warehouse_out_id = getattr(src_wh, "fulfillment_warehouse_id", None) if src_wh else None
            warehouse_in_id = getattr(self.partner_id, "fulfillment_warehouse_id", None)
            fulfillment_out = my_fulfillment_id
            fulfillment_in = self._get_partner_fulfillment_profile_id(self.partner_id)

        elif self.picking_type_code == 'internal':
            src_wh = self.env['stock.warehouse'].search(
                [('lot_stock_id', '=', self.location_id.id)], limit=1
            )
            dest_wh = self.env['stock.warehouse'].search(
                [('lot_stock_id', '=', self.location_dest_id.id)], limit=1
            )
            warehouse_out_id = getattr(src_wh, "fulfillment_warehouse_id", None) if src_wh else None
            warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None) if dest_wh else None
            fulfillment_out = my_fulfillment_id
            fulfillment_in = my_fulfillment_id

        return warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in

    def _sync_transfer(self, client, payload):
        _logger.info("[_sync_transfer]")
        try:
            if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
                _logger.info("[Fulfillment][CREATE] Calling API for %s", self.name)
                response = client.transfer.create(payload)
                
                data_list = response.get("data")
                if not data_list:
                    _logger.error("[Fulfillment] API returned empty data list: %s", response)
                    return

                data_list = response.get('data')

                if isinstance(data_list, dict):
                    data_list = [data_list]
                api_data = data_list[0]

                remote_id = api_data.get("id")
                owner_id = api_data.get("fulfillment_in")

                if not remote_id:
                    _logger.error("[Fulfillment] Could not find 'id' in API response data")
                    return

                
                self.with_context(skip_fulfillment_push=True).write({
                    "fulfillment_transfer_id": remote_id,
                    "fulfillment_transfer_owner_id": owner_id,
                })
                _logger.info("[Fulfillment] Success! Linked %s to API ID: %s", self.name, remote_id)

            else:
                _logger.info("[Fulfillment][UPDATE] Updating existing transfer %s", self.fulfillment_transfer_id)
                client.transfer.update(self.fulfillment_transfer_id, payload)

        except Exception as e:
            _logger.error("[Fulfillment][ERROR] Sync failed for %s: %s", self.name, str(e), exc_info=True)

    @property
    def fulfillment_api(self):
        _logger.info("[fulfillment_api]")
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found")
            return None
        return FulfillmentAPIClient(profile)

    @api.model
    def import_transfers(self, fulfillment_id=None, page=1, limit=50):
        _logger.info("[import_transfers]")
        """Загружает трансферы из Fulfillment API"""
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found")
            return False

        client = FulfillmentAPIClient(profile)

        try:
            response = client.transfer.list(
                fulfillment_id=fulfillment_id,
                page=page,
                limit=limit
            )
        except Exception as e:
            _logger.error("[Fulfillment] Error fetching transfers: %s", e)
            return False

        data = response.get("data")
        if not isinstance(data, list):
            _logger.warning("[Fulfillment] Invalid response format: %s", response)
            return False



        transfers = response.get("data", [])
        for transfer in transfers:
            try:
                self._import_transfer(transfer)
            except Exception as e:
                _logger.error(
                    "[Fulfillment][IMPORT] Failed transfer %s: %s",
                    transfer.get("id"), e, exc_info=True
                )
                self.env.cr.rollback()

        return True

    def _import_transfer(self, transfer):
        _logger.info("[_import_transfer]")
        """Импорт одного transfer в Odoo"""
        remote_id = str(transfer.get("id"))
        if not remote_id:
            _logger.warning("[Fulfillment] Transfer without ID skipped")
            return False

        wh_in_ext = transfer.get("warehouse_in")
        wh_out_ext = transfer.get("warehouse_out")
        status = transfer.get("status")
        contacts = transfer.get("contacts") or []

        warehouse_in = self.env["stock.warehouse"].search(
            [("fulfillment_warehouse_id", "=", wh_in_ext)], limit=1
        )
        warehouse_out = self.env["stock.warehouse"].search(
            [("fulfillment_warehouse_id", "=", wh_out_ext)], limit=1
        )

        location_id = warehouse_out.lot_stock_id.id if warehouse_out else False
        location_dest_id = warehouse_in.lot_stock_id.id if warehouse_in else False

        partner_id = self._find_or_create_partner(contacts, wh_in_ext)

        picking = self._create_or_update_picking(
            remote_id, transfer, location_id, location_dest_id, partner_id, warehouse_out, warehouse_in
        )

        self._create_transfer_items(transfer, picking, location_id, location_dest_id)

        try:
            picking._apply_status(status)
            _logger.info("[Fulfillment] Status applied: %s -> %s", picking.name, status)
        except Exception as e:
            _logger.warning("[Fulfillment] Error applying status %s: %s", status, e)

        return picking

    def _find_or_create_partner(self, contacts, wh_in_ext):
        _logger.info("[_find_or_create_partner]")
        partner_id = False
        contact_data = next(
            (c for c in contacts if c.get("role") == "CUSTOMER"),
            (contacts[0] if contacts else None)
        )

        if contact_data:
            cid = contact_data.get("contact_id")
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
                _logger.info("[Fulfillment] Created partner: %s (%s)", cname, cid)
            else:
                if not partner.fulfillment_contact_id and cid:
                    partner.fulfillment_contact_id = cid

            partner_id = partner.id

        if not partner_id:
            partner_fallback = self.env["res.partner"].search(
                [("fulfillment_warehouse_id", "=", wh_in_ext)], limit=1
            )
            partner_id = partner_fallback.id if partner_fallback else False

        return partner_id

    def _create_or_update_picking(self, remote_id, transfer, location_id, 
                                  location_dest_id, partner_id, warehouse_out, warehouse_in):
        """Создание или обновление picking"""
        _logger.info("[_create_or_update_picking]")
        picking = self.search([
            ("fulfillment_transfer_id", "=", remote_id),
            ("company_id", "=", self.env.company.id),
        ], limit=1)
        picking_type_id = self._map_type(transfer)
        picking_type = self.env["stock.picking.type"].browse(picking_type_id)
        type_code = picking_type.code if picking_type else "unknown"

        wh_code = warehouse_out.code or warehouse_in.code or "WH"
        type_short = {"incoming": "IN", "outgoing": "OUT", "internal": "INT"}.get(type_code, "UNK")
        hash_part = str(remote_id)[:8]
        name = f"[F] {wh_code}/{type_short}/{hash_part}"

        vals = {
            "fulfillment_transfer_id": remote_id,
            "picking_type_id": picking_type_id,
            "partner_id": partner_id,
            "location_id": location_id,
            "location_dest_id": location_dest_id,
        }

        if picking:
            picking.write(vals)
            _logger.info("[Fulfillment] Updated picking %s", picking.name)
        else:
            vals["name"] = name
            picking = self.with_context(skip_fulfillment_push=True).create(vals)
            _logger.info("[Fulfillment] Created picking %s", picking.name)

        return picking

    def _create_transfer_items(self, transfer, picking, location_id, location_dest_id):
        """Создание товарных позиций из трансфера"""
        _logger.info("[_create_transfer_items]")
        items = transfer.get("items", [])
        if not items:
            return

        Move = self.env["stock.move"]

        for item in items:
            product_data = item.get("product") or {}
            fulfillment_product_id = item.get("product_id") or product_data.get("product_id")

            if fulfillment_product_id and not product_data:
                fetched = self._fetch_product_from_api(fulfillment_product_id)
                if fetched:
                    product_data = fetched

            prod_name = product_data.get("name") or "Unnamed Product"
            sku = product_data.get("sku") or (f"F-{fulfillment_product_id}" if fulfillment_product_id else None)
            barcode = product_data.get("barcode")

            product_tmpl = self._find_or_create_product(
                fulfillment_product_id, sku, prod_name, barcode
            )

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

            existing_move = Move.search(
                [("picking_id", "=", picking.id), ("product_id", "=", product_id)], limit=1
            )
            if existing_move:
                existing_move.write(move_vals)
            else:
                Move.create(move_vals)


    def _find_or_create_product(self, fulfillment_product_id, sku, prod_name, barcode):
        _logger.info("[_find_or_create_product]")
        ProductTmpl = self.env["product.template"]
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

            _logger.info("[Fulfillment] Created product '%s'", prod_name)
        else:
            if not product_tmpl.fulfillment_product_id and fulfillment_product_id:
                product_tmpl.fulfillment_product_id = fulfillment_product_id

        return product_tmpl

    def _apply_status(self, target_status):
        _logger.info("[_apply_status]")
        
        self.ensure_one()
        state = self.state

        sequence = ["draft", "confirmed", "assigned", "done"]
        if target_status not in sequence:
            _logger.warning("[Fulfillment] Unknown status: %s", target_status)
            return

        current_i = sequence.index(state)
        target_i = sequence.index(target_status)

        if current_i >= target_i:
            return

        if current_i < sequence.index("confirmed") and target_i >= sequence.index("confirmed"):
            self.action_confirm()
            state = self.state

        if state == "confirmed" and target_i >= sequence.index("assigned"):
            self.action_assign()
            state = self.state

        if state == "assigned" and target_i >= sequence.index("done"):
            self.button_validate()

    def _map_type(self, transfer, current_picking=None):
        _logger.info("[_map_type]")
        
        tr_type = transfer.get("transfer_type") or transfer.get("type")

        type_map = {
            "incoming": "incoming",
            "outgoing": "outgoing",
            "internal": "internal",
        }

        if tr_type in type_map:
            op_code = type_map[tr_type]
        else:
            if current_picking:
                return current_picking.picking_type_id.id
            return False

        op_type = self.env["stock.picking.type"].search([("code", "=", op_code)], limit=1)
        return op_type.id if op_type else (current_picking.picking_type_id.id if current_picking else False)