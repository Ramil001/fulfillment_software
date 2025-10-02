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
        return {
            "reference": picking.name or picking.origin or "Odoo",
            "partner": picking.partner_id.name if picking.partner_id else None,
            "warehouse_out": warehouse_out_id,
            "warehouse_in": warehouse_in_id,
            "status": picking.state or "draft",
            "items": items,
        }


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
                    tmpl.fulfillment_product_id = resp['data'].get('product_id')
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

    # ===== ORM overrides =====
    @api.model
    def create(self, vals):
        _logger.info("[Fulfillment][Create] stock.picking.create vals=%s", vals)
        record = super(FulfillmentTransfers, self).create(vals)
        record._push_to_fulfillment_api()
        return record

    def write(self, vals):
        _logger.info("[Fulfillment][Update] stock.picking.write ids=%s vals=%s", self.ids, vals)
        res = super(FulfillmentTransfers, self).write(vals)
        for picking in self:
            picking._push_to_fulfillment_api()
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

        # диагностика
        try:
            self._get_operation_type(self)
        except Exception:
            # не критично — продолжаем, но логируем
            _logger.exception("[Fulfillment] error while getting operation type")

        if not self.move_ids:
            _logger.debug("[Fulfillment] skip %s - no move_ids", self.name)
            return

        client = self.fulfillment_api
        if not client:
            _logger.warning("[Fulfillment] API client not available for %s", self.name)
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
            # товар идёт ОТ партнёра К нам
            _logger.info("[Fulfillment] Processing INCOMING for %s", self.name)

            # warehouse_out: partner's warehouse (linked or contact)
            if partner and getattr(partner, "linked_warehouse_id", False):
                linked_wh = partner.linked_warehouse_id
                warehouse_out_id = getattr(linked_wh, "fulfillment_warehouse_id", None)
                _logger.info("  - partner linked warehouse: %s -> %s", getattr(linked_wh, "name", ""), warehouse_out_id)
            elif partner:
                warehouse_out_id = getattr(partner, "fulfillment_contact_warehouse_id", None)
                _logger.info("  - partner contact warehouse external id: %s", warehouse_out_id)
            else:
                _logger.warning("  - incoming: no partner on picking %s", self.name)

            # warehouse_in: our local warehouse (location_dest)
            try:
                dest_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_dest_id.id)], limit=1)
                if dest_wh:
                    warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None)
                    _logger.info("  - dest warehouse: %s -> %s", getattr(dest_wh, "name", ""), warehouse_in_id)
                    if not warehouse_in_id:
                        # sync attempt if needed
                        _logger.info("  - dest warehouse has no fulfillment_warehouse_id, attempting sync")
                        try:
                            warehouse_in_id = dest_wh.sync_warehouse_to_api()
                        except Exception as e:
                            _logger.error("  - sync_warehouse_to_api failed for %s: %s", dest_wh.name, e)
                else:
                    _logger.warning("  - no dest warehouse found for location %s", getattr(self.location_dest_id, "name", "None"))
            except Exception as e:
                _logger.exception("[Fulfillment] error resolving destination warehouse: %s", e)

            # fulfillment sides
            fulfillment_out = self._get_partner_fulfillment_profile_id(partner)
            fulfillment_in = my_fulfillment_id

        elif self.picking_type_code == 'outgoing':
            # товар идёт ОТ нас К партнёру
            _logger.info("[Fulfillment] Processing OUTGOING for %s", self.name)

            try:
                src_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_id.id)], limit=1)
                warehouse_out_id = getattr(src_wh, "fulfillment_warehouse_id", None) if src_wh else None
                _logger.info("  - source warehouse: %s -> %s", getattr(src_wh, "name", ""), warehouse_out_id)
            except Exception as e:
                _logger.exception("[Fulfillment] error resolving source warehouse: %s", e)

            # warehouse_in: partner's contact/linked
            if partner and getattr(partner, "linked_warehouse_id", False):
                warehouse_in_id = getattr(partner.linked_warehouse_id, "fulfillment_warehouse_id", None)
                _logger.info("  - partner linked warehouse in: %s", warehouse_in_id)
            elif partner:
                warehouse_in_id = getattr(partner, "fulfillment_contact_warehouse_id", None)
                _logger.info("  - partner contact warehouse in ext id: %s", warehouse_in_id)
            else:
                _logger.warning("  - outgoing: no partner on picking %s", self.name)

            # fulfillment sides
            fulfillment_out = my_fulfillment_id
            fulfillment_in = self._get_partner_fulfillment_profile_id(partner)

        elif self.picking_type_code == 'internal':
            _logger.info("[Fulfillment] Processing INTERNAL for %s", self.name)
            try:
                src_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_id.id)], limit=1)
                dest_wh = self.env['stock.warehouse'].search([('lot_stock_id', '=', self.location_dest_id.id)], limit=1)
                warehouse_out_id = getattr(src_wh, "fulfillment_warehouse_id", None) if src_wh else None
                warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None) if dest_wh else None
                fulfillment_out = fulfillment_in = my_fulfillment_id
                _logger.info("  - internal: out=%s in=%s", warehouse_out_id, warehouse_in_id)
            except Exception as e:
                _logger.exception("[Fulfillment] error resolving internal warehouses: %s", e)
        else:
            _logger.warning("[Fulfillment] Unknown picking_type_code=%s for %s", self.picking_type_code, self.name)

        # --- Build payload and explicitly include fulfillment owners ---
        payload = PickingAdapter.to_api_payload(self, items, warehouse_out_id, warehouse_in_id)
        # explicitly add fulfillment ownership info so remote system knows кто чей
        if fulfillment_out:
            payload['fulfillment_out'] = fulfillment_out
        if fulfillment_in:
            payload['fulfillment_in'] = fulfillment_in

        _logger.info("[Fulfillment][Payload][%s]\n%s", self.picking_type_code,
                     json.dumps(payload, ensure_ascii=False, indent=2, default=str))

        # --- Sync ---
        try:
            if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
                response = client.transfer.create(payload)
                if response and response.get('status') == 'success':
                    self.fulfillment_transfer_id = (
                        response.get('transfer_id')                               
                        or response.get('data', {}).get('transfer_id')
                        or "Empty"
                    )

                    _logger.info(
                        "[Fulfillment][Create] Created remote transfer %s -> %s",
                        self.name, self.fulfillment_transfer_id
                    )
        except Exception as e:
            _logger.error("[Fulfillment][Create] Failed to create transfer %s: %s", self.name, e)


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
    def load_transfers(self, fulfillment_id=None, page=1, limit=50):
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
        """Импорт одного transfer в Odoo"""
        remote_id = transfer.get("transfer_id")
        reference = transfer.get("reference")

        if not remote_id:
            _logger.warning("[Fulfillment] Transfer без ID пропущен: %s", transfer)
            return False

        # 🔑 ищем именно по transfer_id
        picking = self.search([("fulfillment_transfer_id", "=", remote_id)], limit=1)

        vals = {
            "name": reference,
            "fulfillment_transfer_id": remote_id,
            "state": transfer.get("status"),
            "picking_type_id": self._map_type(transfer),
        }

        if picking:
            picking.write(vals)
            _logger.info("[Fulfillment] Обновлён picking %s из transfer %s", picking.name, remote_id)
        else:
            picking = self.create(vals)
            _logger.info("[Fulfillment] Создан новый picking %s из transfer %s", picking.name, remote_id)

        return picking



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
