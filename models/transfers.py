# -*- coding: utf-8 -*-
from odoo import models, api, fields, _
from odoo.exceptions import ValidationError
import logging
from datetime import datetime
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


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

    # ----- helper to create API client when needed -----
    @property
    def fulfillment_api(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment][Profile not found]")
            return None
        return FulfillmentAPIClient(profile)

    # ===== public methods / overrides =====
    @api.model
    def write(self, vals):
        """
        Override write: after local write - push changes to Fulfillment API for pickings that have moves.
        """
        _logger.info("[Fulfillment][Update] stock.picking.write called ids=%s vals=%s", self.ids, vals)
        res = super(FulfillmentTransfers, self).write(vals)

        for picking in self:
            # only operate for pickings that have moves (items)
            if not picking.move_ids:
                _logger.debug("[Fulfillment] skipping picking %s - no move_ids", picking.name)
                continue

            try:
                client = self.fulfillment_api
                if not client:
                    _logger.warning("[Fulfillment][Update] API client not available, skipping picking %s", picking.name)
                    continue

                # Build items payload (product must be linked by tmpl.fulfillment_product_id)
                items = []
                for move in picking.move_ids:
                    tmpl = move.product_id.product_tmpl_id
                    if 'fulfillment_product_id' not in tmpl._fields:
                        _logger.error(
                            "[Fulfillment][Check] product.template has no field 'fulfillment_product_id' for product %s (tmpl_id=%s)",
                            tmpl.name, tmpl.id
                        )
                        continue

                    if not tmpl.fulfillment_product_id:
                        # Try to create product in remote API
                        product_payload = {
                            "name": tmpl.name,
                            "sku": tmpl.default_code or f"SKU-{tmpl.id}",
                            "barcode": tmpl.barcode or str(tmpl.id).zfill(6)
                        }
                        try:
                            resp = client.product.create(product_payload)
                            if resp and resp.get('status') == 'success':
                                remote_pid = resp['data'].get('product_id')
                                if remote_pid:
                                    tmpl.fulfillment_product_id = remote_pid
                                    _logger.info("[Fulfillment][Create] Created remote product %s -> %s", tmpl.name, remote_pid)
                                else:
                                    _logger.warning("[Fulfillment][Create] API returned success but no product_id for %s: %s", tmpl.name, resp)
                            else:
                                _logger.warning("[Fulfillment][Create] Failed to create product %s: %s", tmpl.name, resp)
                        except Exception as e:
                            _logger.error("[Fulfillment][Create] Exception creating product %s: %s", tmpl.name, e)

                    # skip if still missing mapping
                    if not tmpl.fulfillment_product_id:
                        _logger.warning("[Fulfillment][Skip] product %s has no fulfillment_product_id after attempt -> skipping", tmpl.name)
                        continue

                    items.append({
                        "name": move.product_id.name,
                        "product_id": tmpl.fulfillment_product_id,
                        "quantity": float(move.product_uom_qty or 0.0),
                        "unit": move.product_uom.name if move.product_uom else (move.product_uom_id.name if getattr(move, 'product_uom_id', None) else 'Units')
                    })

                if not items:
                    _logger.debug("[Fulfillment] No items to sync for picking %s -> skipping API sync", picking.name)
                    continue

                # Determine warehouses external ids
                warehouse_out_id, warehouse_in_id = self._get_transfer_warehouses(picking)

                payload = {
                    "reference": picking.name or picking.origin or "Odoo",
                    "warehouse_out": warehouse_out_id,
                    "warehouse_in": warehouse_in_id,
                    "status": picking.state or "draft",
                    "items": items,
                }

                # create or update remote transfer
                if not picking.fulfillment_transfer_id or picking.fulfillment_transfer_id == "Empty":
                    try:
                        response = client.transfer.create(payload)
                        if response and response.get('status') == 'success':
                            picking.fulfillment_transfer_id = response.get('transfer_id') or response.get('data', {}).get('transfer_id') or response.get('data', {}).get('id') or "Empty"
                            _logger.info("[Fulfillment][Create] Created remote transfer for %s -> %s", picking.name, picking.fulfillment_transfer_id)
                        else:
                            _logger.warning("[Fulfillment][Create] Unexpected API response when creating transfer for %s: %s", picking.name, response)
                    except Exception as e:
                        _logger.error("[Fulfillment][Create] API create failed for %s: %s", picking.name, e)
                else:
                    try:
                        client.transfer.update(picking.fulfillment_transfer_id, payload)
                        _logger.info("[Fulfillment][Update] Updated remote transfer %s for picking %s", picking.fulfillment_transfer_id, picking.name)
                    except Exception as e:
                        _logger.error("[Fulfillment][Update] API update failed for transfer %s: %s", picking.fulfillment_transfer_id, e)

            except Exception as e:
                _logger.exception("[Fulfillment][Update] Unexpected error while syncing picking %s: %s", picking.name, e)

        return res

    # -------------------------
    @api.model
    def create_fulfillment_receipt(self):
        """
        Получает покупки из Fulfillment API и создаёт приходные pickings.
        Устанавливает fulfillment_partner_id если найдёт партнёра (по fulfillment_id в payload),
        иначе оставляет пустым.
        """
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.error("[Fulfillment] Profile not found")
            return False

        client = FulfillmentAPIClient(profile)
        try:
            purchases = client.purchase.get()
            _logger.info("[Fulfillment] Purchases fetched: %s", bool(purchases))
            if not purchases:
                _logger.warning("[Fulfillment] No purchases returned from API")
                return False
        except FulfillmentAPIError as e:
            raise ValidationError(_("Fulfillment API error: %s") % str(e))
        except Exception as e:
            raise ValidationError(_("Fulfillment API error: %s") % str(e))

        # Safe refs
        picking_type_in = self.env.ref('stock.picking_type_in', raise_if_not_found=False)
        uom_unit = self.env.ref("uom.product_uom_unit", raise_if_not_found=False)

        created_count = 0
        for purchase in purchases:
            if not purchase:
                continue

            # Try to find fulfillment partner by payload (if present)
            fp = False
            fulfid = purchase.get('fulfillment_id') or purchase.get('fulfillment') or purchase.get('owner_id')
            if fulfid:
                fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', fulfid)], limit=1)

            # fallback: try default partner from module (if any)
            partner_res = False
            if fp and fp.partner_id:
                partner_res = fp.partner_id
            else:
                # try to find any res.partner (previous behavior) - keep for compatibility
                partner_res = self.env['res.partner'].search([], limit=1)
                if not partner_res:
                    partner_res = self.env['res.partner'].create({'name': 'Fulfillment Partner'})

            # Build picking vals
            vals = {
                'partner_id': partner_res.id if partner_res else False,
                'picking_type_id': picking_type_in.id if picking_type_in else False,
                'location_id': picking_type_in.default_location_src_id.id if picking_type_in and picking_type_in.default_location_src_id else False,
                'location_dest_id': picking_type_in.default_location_dest_id.id if picking_type_in and picking_type_in.default_location_dest_id else False,
                'origin': purchase.get('name') or purchase.get('reference') or 'Fulfillment Purchase',
                'fulfillment_partner_id': fp.id if fp else False,
            }

            try:
                picking = self.env['stock.picking'].create(vals)
                created_count += 1
                _logger.info("[Fulfillment] Created receipt picking %s (partner=%s, fp=%s)", picking.name, partner_res and partner_res.name, fp and fp.name)

                # create moves
                for order_line in purchase.get('orders', []) or []:
                    product_info = order_line.get('product')
                    if not product_info:
                        _logger.warning("[Fulfillment] purchase order line without product -> skip")
                        continue

                    # detect or create product template / variant
                    product_code = f"FULFILL-[{product_info.get('id')}]"
                    product_template = self.env['product.template'].search([('default_code', '=', product_code)], limit=1)
                    if not product_template:
                        product_template = self.env['product.template'].create({
                            'name': product_info.get('name') or product_code,
                            'default_code': product_code,
                            'type': 'consu',
                            'uom_id': uom_unit.id if uom_unit else False,
                            'uom_po_id': uom_unit.id if uom_unit else False,
                        })
                        _logger.info("[Fulfillment] Created local product template %s", product_template.name)

                    product_variant = product_template.product_variant_id
                    qty = float(order_line.get('quantity') or 0.0)

                    self.env['stock.move'].create({
                        'product_id': product_variant.id,
                        'name': vals.get('origin'),
                        'product_uom_qty': qty,
                        'product_uom': product_variant.uom_id.id,
                        'picking_id': picking.id,
                        'location_id': picking.location_id.id,
                        'location_dest_id': picking.location_dest_id.id,
                    })

                # confirm picking
                try:
                    picking.action_confirm()
                except Exception as e:
                    _logger.warning("[Fulfillment] Could not confirm picking %s: %s", picking.name, e)

            except Exception as e:
                _logger.error("[Fulfillment] Failed to create receipt picking for purchase %s: %s", purchase.get('name'), e)
                # do not break whole loop

        _logger.info("[Fulfillment] create_fulfillment_receipt finished, created=%s", created_count)
        return True

    # -------------------------
    @api.model
    def load_transfers(self, fulfillment_id=None, page=1, limit=100):
        """
        Loads transfers from Fulfillment API and creates/updates local stock.picking records.
        Sets fulfillment_partner_id when possible.
        Returns True / False (or number of transferred records)
        """
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            raise ValidationError(_("Fulfillment profile not found"))

        client = FulfillmentAPIClient(profile)
        fulfillment_id = fulfillment_id or profile.fulfillment_profile_id

        try:
            response = client.fulfillment.get_transfers_by_fulfillment(
                fulfillment_id, params={"page": page, "limit": limit}
            )
        except Exception as e:
            raise ValidationError(_("Fulfillment API error: %s") % str(e))

        if response.get("status") != "success":
            _logger.warning("[Fulfillment] API returned non-success for transfers: %s", response)
            return False

        transfers = response.get("data", []) or []
        if not transfers:
            _logger.info("[Fulfillment] No transfers to load")
            return True

        # Default picking type for created transfers (internal), fallback if not found
        picking_type_internal = self.env.ref("stock.picking_type_internal", raise_if_not_found=False)
        uom_unit = self.env.ref("uom.product_uom_unit", raise_if_not_found=False)

        created = 0
        for transfer in transfers:
            try:
                remote_tid = transfer.get("transfer_id") or transfer.get("id")
                picking = self.search([("fulfillment_transfer_id", "=", remote_tid)], limit=1)

                # Ensure warehouses exist (or create simple ones)
                warehouse_out = self.env["stock.warehouse"].search([
                    ("fulfillment_warehouse_id", "=", transfer.get("warehouse_out"))
                ], limit=1)
                if not warehouse_out and transfer.get("warehouse_out"):
                    warehouse_out = self.env["stock.warehouse"].create({
                        "name": f"WH OUT {transfer['warehouse_out'][:6]}",
                        "code": f"OUT-{transfer['warehouse_out'][:3]}",
                        "fulfillment_warehouse_id": transfer["warehouse_out"],
                    })
                    _logger.info("[Fulfillment] Created warehouse_out %s", warehouse_out.name)

                warehouse_in = self.env["stock.warehouse"].search([
                    ("fulfillment_warehouse_id", "=", transfer.get("warehouse_in"))
                ], limit=1)
                if not warehouse_in and transfer.get("warehouse_in"):
                    warehouse_in = self.env["stock.warehouse"].create({
                        "name": f"WH IN {transfer['warehouse_in'][:6]}",
                        "code": f"IN-{transfer['warehouse_in'][:3]}",
                        "fulfillment_warehouse_id": transfer["warehouse_in"],
                    })
                    _logger.info("[Fulfillment] Created warehouse_in %s", warehouse_in.name)

                # Find partner (fulfillment.partners) by fulfillment id if available
                fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', transfer.get('fulfillment_id') or transfer.get('owner_id'))], limit=1)

                # Decide picking type: if transfer provides status or type, map to code; fallback to internal
                p_type = picking_type_internal
                # if API explicitly gives movement type, try map
                ttype = transfer.get('type') or transfer.get('movement_type') or transfer.get('picking_type')
                if ttype:
                    if ttype in ('incoming', 'receipt', 'purchase'):
                        p_type = self.env['stock.picking.type'].search([('code', '=', 'incoming')], limit=1) or p_type
                    elif ttype in ('outgoing', 'delivery', 'shipment'):
                        p_type = self.env['stock.picking.type'].search([('code', '=', 'outgoing')], limit=1) or p_type
                    elif ttype in ('internal',):
                        p_type = self.env['stock.picking.type'].search([('code', '=', 'internal')], limit=1) or p_type

                # Create or update picking
                if not picking:
                    vals = {
                        "picking_type_id": p_type.id if p_type else False,
                        "location_id": warehouse_out.lot_stock_id.id if warehouse_out and warehouse_out.lot_stock_id else False,
                        "location_dest_id": warehouse_in.lot_stock_id.id if warehouse_in and warehouse_in.lot_stock_id else False,
                        "origin": transfer.get("reference") or transfer.get("origin") or f"Transfer {remote_tid}",
                        "fulfillment_transfer_id": remote_tid,
                        "state": transfer.get("status") or "draft",
                        "fulfillment_partner_id": fp.id if fp else False,
                    }
                    picking = self.create(vals)
                    created += 1
                    _logger.info("[Fulfillment] Created transfer picking %s (%s)", picking.name, remote_tid)
                else:
                    # update existing
                    picking.move_ids.unlink()
                    picking.state = transfer.get("status", picking.state)
                    # ensure partner link
                    if fp and picking.fulfillment_partner_id != fp:
                        picking.fulfillment_partner_id = fp.id
                    _logger.info("[Fulfillment] Updated transfer picking %s (%s)", picking.name, remote_tid)

                # Create moves
                for item in transfer.get("items", []) or []:
                    product_info = item.get("product")
                    if not product_info:
                        _logger.warning("[Fulfillment] Transfer item without product -> skip")
                        continue

                    product_code = product_info.get("sku") or f"FULFILL-{product_info.get('product_id')}"
                    product_barcode = product_info.get("barcode")
                    # Try find product by fulfillment_product_id / default_code / barcode
                    product_template = self.env['product.template'].search([
                        '|', '|',
                        ('fulfillment_product_id', '=', product_info.get('product_id')),
                        ('default_code', '=', product_code),
                        ('barcode', '=', product_barcode)
                    ], limit=1)

                    if not product_template:
                        product_template = self.env['product.template'].create({
                            'name': product_info.get('name', product_code),
                            'default_code': product_code,
                            'barcode': product_barcode,
                            'type': 'consu',
                            'uom_id': uom_unit.id if uom_unit else False,
                            'uom_po_id': uom_unit.id if uom_unit else False,
                            'fulfillment_product_id': product_info.get('product_id'),
                        })
                        _logger.info("[Fulfillment] Created product template %s", product_template.name)
                    else:
                        # ensure fulfillment_product_id set
                        if not product_template.fulfillment_product_id and product_info.get('product_id'):
                            product_template.fulfillment_product_id = product_info.get('product_id')

                    product_variant = product_template.product_variant_id
                    qty = float(item.get('quantity') or 0.0)

                    self.env['stock.move'].create({
                        'product_id': product_variant.id,
                        'name': transfer.get('reference') or transfer.get('origin') or product_variant.name,
                        'product_uom_qty': qty,
                        'product_uom': product_variant.uom_id.id,
                        'picking_id': picking.id,
                        'location_id': picking.location_id.id,
                        'location_dest_id': picking.location_dest_id.id,
                    })

            except Exception as e:
                _logger.exception("[Fulfillment] Failed processing transfer %s: %s", transfer.get('transfer_id') or transfer.get('id'), e)
                # continue with next transfer

        _logger.info("[Fulfillment] load_transfers finished: created=%s", created)
        return created

    # ===== helper methods =====
    def _get_transfer_warehouses(self, picking):
        """
        Вернёт коды/внешние ID складов (warehouse_out_id, warehouse_in_id) для API.
        Возвращает None когда не может определить.
        """
        warehouse_out_id, warehouse_in_id = None, None

        # warehouse_out: по source location -> родительский склад
        if picking.location_id:
            warehouse_out = self.env['stock.warehouse'].search([
                ('view_location_id', 'parent_of', picking.location_id.id)
            ], limit=1)
            if warehouse_out:
                # внешнее поле в твоей модели - warehouse.warehouse_id
                warehouse_out_id = warehouse_out.warehouse_id or warehouse_out.fulfillment_warehouse_id

        # warehouse_in: если outgoing -> ищем склад по partner_id (как в старом коде)
        if picking.picking_type_code == 'outgoing':
            if picking.partner_id:
                warehouse_in = self.env['stock.warehouse'].search([
                    ('partner_id', '=', picking.partner_id.id)
                ], limit=1)
                if warehouse_in:
                    warehouse_in_id = warehouse_in.warehouse_id or warehouse_in.fulfillment_warehouse_id
        else:
            # incoming/internal -> по dest location
            if picking.location_dest_id:
                warehouse_in = self.env['stock.warehouse'].search([
                    ('view_location_id', 'parent_of', picking.location_dest_id.id)
                ], limit=1)
                if warehouse_in:
                    warehouse_in_id = warehouse_in.warehouse_id or warehouse_in.fulfillment_warehouse_id

        return warehouse_out_id, warehouse_in_id
