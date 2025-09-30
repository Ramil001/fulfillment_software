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
        if picking.picking_type_code == 'incoming':
            return (
                picking.partner_id.fulfillment_contact_warehouse_id if picking.partner_id else None,
                self._ext_id_from_location(picking.location_dest_id)
            )
        if picking.picking_type_code == 'outgoing':
            return (
                self._ext_id_from_location(picking.location_id),
                picking.partner_id.fulfillment_contact_warehouse_id if picking.partner_id else None
            )
        if picking.picking_type_code == 'internal':
            return (
                self._ext_id_from_location(picking.location_id),
                self._ext_id_from_location(picking.location_dest_id)
            )
        return None, None

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



    # @api.model
    # def create(self, vals):
    #     raise UserError("Тестовая ошибка при создании трансфера — документ не сохранён!")

    #     record = super(FulfillmentTransfers, self).create(vals)
    #     record._push_to_fulfillment_api()
    #     return record

    # def write(self, vals):
    #     raise UserError("Тестовая ошибка при обновлении трансфера — изменения не записаны!")

    #     res = super(FulfillmentTransfers, self).write(vals)
    #     for picking in self:
    #         picking._push_to_fulfillment_api()
    #     return res

    
    
    # ===== helpers =====
    def _push_to_fulfillment_api(self):
        self.ensure_one()
        self._get_operation_type(self)

        if not self.move_ids:
            _logger.debug("[Fulfillment] skip %s - no move_ids", self.name)
            return

        client = self.fulfillment_api
        if not client:
            _logger.warning("[Fulfillment] API client not available for %s", self.name)
            return

        # --- Items ---
        items = FulfillmentItemBuilder(client).build_items(self.move_ids)
        if not items:
            _logger.debug("[Fulfillment] No items to sync for %s", self.name)
            return

        # --- Определяем склад/партнера в зависимости от типа ---
        warehouse_out_id = None
        warehouse_in_id = None
        partner_fulfillment_id = None

        if self.picking_type_code == 'incoming':
            # ДИАГНОСТИКА: Логируем что ищем
            _logger.info("[Fulfillment] Incoming transfer diagnostic:")
            _logger.info("  - Partner: %s (ID: %s)", self.partner_id.name, self.partner_id.id)
            _logger.info("  - Partner linked_warehouse_id: %s", self.partner_id.linked_warehouse_id.id if self.partner_id and self.partner_id.linked_warehouse_id else "None")
            _logger.info("  - Partner fulfillment_contact_warehouse_id: %s", self.partner_id.fulfillment_contact_warehouse_id if self.partner_id else "None")
            _logger.info("  - Location dest: %s (ID: %s)", self.location_dest_id.name, self.location_dest_id.id)
            
            # Для входящего: warehouse_out_id - от партнера
            if self.partner_id and self.partner_id.linked_warehouse_id:
                linked_warehouse = self.partner_id.linked_warehouse_id
                warehouse_out_id = linked_warehouse.fulfillment_warehouse_id
                _logger.info("  - Using linked warehouse: %s -> fulfillment_id: %s", linked_warehouse.name, warehouse_out_id)
            elif self.partner_id:
                warehouse_out_id = self.partner_id.fulfillment_contact_warehouse_id
                _logger.info("  - Using partner contact warehouse_id: %s", warehouse_out_id)
            else:
                _logger.warning("  - No partner found for incoming transfer")
            
            # warehouse_in_id - наш локальный склад
            dest_warehouse = self.env['stock.warehouse'].search([
                ('lot_stock_id', '=', self.location_dest_id.id)
            ], limit=1)
            
            if dest_warehouse:
                warehouse_in_id = dest_warehouse.fulfillment_warehouse_id
                _logger.info("  - Destination warehouse: %s -> fulfillment_id: %s", dest_warehouse.name, warehouse_in_id)
                
                if not warehouse_in_id:
                    _logger.warning("  - Destination warehouse %s has no fulfillment_warehouse_id, syncing...", dest_warehouse.name)
                    warehouse_in_id = dest_warehouse.sync_warehouse_to_api()
            else:
                _logger.warning("  - No warehouse found for location %s", self.location_dest_id.name)
            
        elif self.picking_type_code == 'outgoing':
            partner_fulfillment_id = self.partner_id.fulfillment_contact_warehouse_id if self.partner_id else None
            warehouse_out_id = self.env['stock.warehouse'].search([('lot_stock_id','=',self.location_id.id)], limit=1).fulfillment_warehouse_id
            warehouse_in_id = partner_fulfillment_id
            
        elif self.picking_type_code == 'internal':
            warehouse_out_id = self.env['stock.warehouse'].search([('lot_stock_id','=',self.location_id.id)], limit=1).fulfillment_warehouse_id
            warehouse_in_id = self.env['stock.warehouse'].search([('lot_stock_id','=',self.location_dest_id.id)], limit=1).fulfillment_warehouse_id

        # --- Payload ---
        payload = PickingAdapter.to_api_payload(self, items, warehouse_out_id, warehouse_in_id)
        
        _logger.info(
            "[Fulfillment][Payload][%s]\n%s",
            self.picking_type_code,
            json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        )

        # --- Sync ---
        try:
            if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
                response = client.transfer.create(payload)
                if response and response.get('status') == 'success':
                    self.fulfillment_transfer_id = (
                        response.get('transfer_id')
                        or response.get('data', {}).get('transfer_id')
                        or response.get('data', {}).get('id')
                        or "Empty"
                    )
                    _logger.info("[Fulfillment][Create] Created remote transfer %s -> %s",
                                self.name, self.fulfillment_transfer_id)
            else:
                client.transfer.update(self.fulfillment_transfer_id, payload)
                _logger.info("[Fulfillment][Update] Updated remote transfer %s for %s",
                            self.fulfillment_transfer_id, self.name)
        except Exception as e:
            _logger.error("[Fulfillment][Sync] API error for %s: %s", self.name, e)

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
            # здесь создаёшь/обновляешь stock.picking
            self._import_transfer(transfer)

        return True

    def _import_transfer(self, transfer):
        """Импорт одного transfer в Odoo"""
        picking_type = transfer.get("type")
        reference = transfer.get("reference")

        picking = self.search([("name", "=", reference)], limit=1)
        if not picking:
            picking = self.create({
                "name": reference,
                "picking_type_id": self._map_type(picking_type),
                # дополняешь нужные поля
            })
            _logger.info("[Fulfillment] Создан новый picking %s из transfer %s", picking.name, transfer.get("id"))
        else:
            picking.write({
                "state": transfer.get("status"),
                # обновить данные
            })
            _logger.info("[Fulfillment] Обновлён picking %s из transfer %s", picking.name, transfer.get("id"))

    def _map_type(self, remote_type):
        """Маппинг типов transfer -> Odoo picking_type_id"""
        op_type = self.env["stock.picking.type"].search([("code", "=", remote_type)], limit=1)
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
