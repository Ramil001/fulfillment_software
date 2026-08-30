# -*- coding: utf-8 -*-
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    from ..lib.api_client import FulfillmentAPIClient
except ImportError:
    FulfillmentAPIClient = None


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    fulfillment_stock_id = fields.Char(
        string='Fulfillment Stock ID',
        readonly=True,
        copy=False,
        help='External stock record ID from the Fulfillment API',
    )

    is_external_fulfillment_stock = fields.Boolean(
        string='External Fulfillment Stock',
        compute='_compute_is_external_fulfillment_stock',
        help='True when this quant belongs to a fulfillment partner warehouse '
             'that is not owned by this Odoo instance. '
             'Quantity can only be updated via API import, not manually.',
    )

    @api.depends('location_id')
    def _compute_is_external_fulfillment_stock(self):
        for quant in self:
            warehouse = self.env['stock.warehouse'].search([
                '|',
                ('lot_stock_id', '=', quant.location_id.id),
                ('view_location_id', 'parent_of', quant.location_id.id),
            ], limit=1)
            quant.is_external_fulfillment_stock = (
                bool(warehouse)
                and bool(warehouse.fulfillment_warehouse_id)
                and not quant._is_local_warehouse(warehouse)
            )

    def _is_local_warehouse(self, warehouse):
        """Return True if this warehouse is owned/operated by the current Odoo instance."""
        if not warehouse:
            return True
        owner = getattr(warehouse, 'fulfillment_owner_id', None)
        if not owner:
            return True
        profile = self.env['fulfillment.profile'].search([], limit=1)
        my_id = getattr(profile, 'fulfillment_profile_id', None)
        owner_fid = getattr(owner, 'fulfillment_id', None)
        return not my_id or not owner_fid or owner_fid == my_id
    
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for quant in records:
            qty = quant.quantity
            if qty != 0:
                _logger.info(
                    "[Stock Create] Создана новая запись stock.quant для товара '%s' (ID: %s). Начальное количество: %s шт.",
                    quant.product_id.display_name,
                    quant.id,
                    qty
                )
        return records

    def write(self, vals):
        """Prevent manual edits to stock quantities for external fulfillment warehouses.

        Only blocks direct edits from the Inventory Adjustments UI (inventory_mode context).
        Automatic updates from transfers (stock moves), imports, and system operations
        are always allowed.
        """
        qty_fields = {'quantity', 'reserved_quantity'}
        ctx = self.env.context
        is_manual_edit = (
            qty_fields & set(vals.keys())
            and ctx.get('inventory_mode')
            and not ctx.get('from_fulfillment_import')
            and not ctx.get('skip_fulfillment_push')
        )
        
        # Сохраняем старые значения количества для логирования разницы
        old_quantities = {q.id: q.quantity for q in self} if 'quantity' in vals else {}

        if is_manual_edit:
            for quant in self:
                warehouse = self.env['stock.warehouse'].search([
                    '|',
                    ('lot_stock_id', '=', quant.location_id.id),
                    ('view_location_id', 'parent_of', quant.location_id.id),
                ], limit=1)
                if (
                    warehouse
                    and warehouse.fulfillment_warehouse_id
                    and not quant._is_local_warehouse(warehouse)
                ):
                    raise UserError(_(
                        "You cannot manually edit stock quantities for the "
                        "fulfillment partner warehouse '%s'. "
                        "Use the Import function to synchronise stock from "
                        "the fulfillment API.",
                        warehouse.display_name,
                    ))
                    
        res = super().write(vals)
        
        # Логируем изменение количества (добавилось/удалилось)
        if 'quantity' in vals:
            for quant in self:
                old_qty = old_quantities.get(quant.id, 0.0)
                new_qty = quant.quantity
                diff = new_qty - old_qty
                if diff != 0:
                    _logger.info(
                        "[Stock Change] Товар '%s' (ID: %s): количество изменилось с %s до %s. "
                        "Разница: %s (%s шт.)",
                        quant.product_id.display_name,
                        quant.id,
                        old_qty,
                        new_qty,
                        f"+{diff}" if diff > 0 else diff,
                        abs(diff)
                    )

        if is_manual_edit:
            self._push_stock_to_api()
        return res
    
    def _push_stock_to_api(self):
        """Sends updated stock quantities to the Fulfillment API."""
        try:
            from odoo.addons.fulfillment_software.lib.api_client.client import FulfillmentAPIClient
        except ImportError:
            _logger.error("[Fulfillment] Cannot import FulfillmentAPIClient")
            return

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found for push")
            return

        client = FulfillmentAPIClient(profile)
        _logger.info("[Fulfillment] Starting push process for %s quants", len(self))

        for quant in self:
            warehouse = self.env['stock.warehouse'].search([
                '|',
                ('lot_stock_id', '=', quant.location_id.id),
                ('view_location_id', 'parent_of', quant.location_id.id),
            ], limit=1)

            _logger.info("[Fulfillment DEBUG] Quant: %s, Location: %s, Warehouse: %s", 
                         quant.id, quant.location_id.display_name, warehouse.name if warehouse else 'None')

            if not warehouse:
                _logger.info("[Fulfillment DEBUG] SKIP: No warehouse found for location %s", quant.location_id.display_name)
                continue
                
            if not warehouse.fulfillment_warehouse_id:
                _logger.info("[Fulfillment DEBUG] SKIP: Warehouse '%s' has empty fulfillment_warehouse_id", warehouse.name)
                continue
                
            if not quant._is_local_warehouse(warehouse):
                _logger.info("[Fulfillment DEBUG] SKIP: Warehouse '%s' is not considered local", warehouse.name)
                continue

            product_fid = quant.product_id.product_tmpl_id.fulfillment_product_id
            if not product_fid:
                _logger.info("[Fulfillment DEBUG] SKIP: Product '%s' has empty fulfillment_product_id", quant.product_id.display_name)
                continue

            try:
                payload = {
                    'product_id': product_fid,
                    'warehouse_id': warehouse.fulfillment_warehouse_id,
                    'quantity': quant.quantity,
                }
                
                if quant.fulfillment_stock_id:
                    _logger.info("[Fulfillment DEBUG] Sending UPDATE to API. Stock ID: %s, Payload: %s", quant.fulfillment_stock_id, payload)
                    client.stock.update(quant.fulfillment_stock_id, payload)
                    _logger.info("[Fulfillment] Pushed stock UPDATE for %s: qty=%s", quant.product_id.name, quant.quantity)
                else:
                    _logger.info("[Fulfillment DEBUG] Sending CREATE to API. Payload: %s", payload)
                    response = client.stock.create(payload)
                    _logger.info("[Fulfillment] Pushed stock CREATE for %s: qty=%s", quant.product_id.name, quant.quantity)
                    
                    if response and isinstance(response, dict) and response.get('id'):
                        quant.with_context(skip_fulfillment_push=True).write({
                            'fulfillment_stock_id': response.get('id')
                        })

            except Exception as e:
                _logger.error("[Fulfillment] Error pushing stock for quant %s: %s", quant.id, e, exc_info=True)

    def import_stock(self, filters=None):
        _logger.info("[import_stock]")
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found")
            return False

        if FulfillmentAPIClient is None:
            _logger.error("[Fulfillment] API client not available")
            return False

        client = FulfillmentAPIClient(profile)
        try:
            response = client.stock.list(filters=filters)
        except Exception as e:
            _logger.error("[Fulfillment] Error fetching stock: %s", e)
            return False

        data = response.get('data')
        if not isinstance(data, list):
            _logger.warning("[Fulfillment] Invalid stock response: %s", response)
            return False

        for item in data:
            try:
                self._import_stock_item(item)
            except Exception as e:
                _logger.error("[Fulfillment] Error importing stock item %s: %s", item, e, exc_info=True)
                self.env.cr.rollback()

        return True

    def _import_stock_item(self, item):
        fulfillment_product_id = item.get('product_id')
        warehouse_id = item.get('warehouse_id')
        qty = float(item.get('quantity') or 0.0)
        available = float(item.get('available') or 0.0)
        stock_id = item.get('id')

        if not fulfillment_product_id or not warehouse_id:
            _logger.warning("[Fulfillment] Stock item missing product or warehouse: %s", item)
            return

        product = self.env['product.template'].search(
            [('fulfillment_product_id', '=', fulfillment_product_id)], limit=1
        )
        if not product:
            _logger.warning("[Fulfillment] Product not found for fulfillment_id %s", fulfillment_product_id)
            return

        warehouse = self.env['stock.warehouse'].search(
            [('fulfillment_warehouse_id', '=', warehouse_id)], limit=1
        )
        if not warehouse:
            _logger.warning("[Fulfillment] Warehouse not found for fulfillment_id %s", warehouse_id)
            return

        location = warehouse.lot_stock_id
        if not location:
            _logger.warning("[Fulfillment] Warehouse %s has no stock location", warehouse.name)
            return

        quant = self.search([
            ('product_id', '=', product.product_variant_id.id),
            ('location_id', '=', location.id),
        ], limit=1)

        if quant:
            quant.with_context(from_fulfillment_import=True).write({
                'quantity': qty,
                'reserved_quantity': qty - available if qty > available else 0.0,
            })
            _logger.info("[Fulfillment] Updated stock: %s qty=%s", product.name, qty)
        else:
            self.with_context(from_fulfillment_import=True).create({
                'product_id': product.product_variant_id.id,
                'location_id': location.id,
                'quantity': qty,
                'reserved_quantity': qty - available if qty > available else 0.0,
                'fulfillment_stock_id': stock_id,
            })
            _logger.info("[Fulfillment] Created stock: %s qty=%s", product.name, qty)


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    fulfillment_operation_type = fields.Selection([
        ('send_to_fulfillment', 'Send to Fulfillment'),
        ('request_from_fulfillment', 'Request from Fulfillment'),
    ], string='Fulfillment Operation', copy=False,
       help='Mark this operation type for fulfillment integration.\n'
            '• Send to Fulfillment: outgoing transfer from your warehouse to a fulfillment partner warehouse.\n'
            '• Request from Fulfillment: incoming transfer requesting goods from a fulfillment partner warehouse.')

    fulfillment_partner_id = fields.Many2one(
        'fulfillment.partners',
        string='Fulfillment Partner',
        copy=False,
        help='The fulfillment partner associated with this operation type. '
             'Used to automatically resolve API warehouse IDs during transfer sync.',
    )


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    warehouse_role = fields.Selection([
        ('own', 'Own'),
        ('rented', 'Rented'),
        ('leased_out', 'Leased out'),
    ], string='Warehouse Role', compute='_compute_warehouse_role', store=True,
       help='Own: local warehouse. Rented: physical space leased from a fulfillment partner. '
            'Leased out: your warehouse space given to a client.')

    @api.depends('fulfillment_owner_id', 'fulfillment_client_id', 'fulfillment_warehouse_id')
    def _compute_warehouse_role(self):
        profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
        my_id = profile.fulfillment_profile_id if profile else None
        for wh in self:
            if not wh.fulfillment_warehouse_id:
                wh.warehouse_role = 'own'
                continue
            owner_fid = wh.fulfillment_owner_id.fulfillment_id if wh.fulfillment_owner_id else None
            client_fid = wh.fulfillment_client_id.fulfillment_id if wh.fulfillment_client_id else None
            if owner_fid and my_id and owner_fid != my_id:
                wh.warehouse_role = 'rented'
            elif client_fid and my_id and client_fid != my_id:
                wh.warehouse_role = 'leased_out'
            else:
                wh.warehouse_role = 'own'

    def name_get(self):
        _icons = {
            'rented': '📦',
            'leased_out': '🔑',
            'own': '🏠',
        }
        result = []
        for wh in self:
            role = wh.warehouse_role or ('own' if not wh.fulfillment_warehouse_id else None)
            icon = _icons.get(role, '')
            name = f"{icon} {wh.name}" if icon else wh.name
            result.append((wh.id, name))
        return result

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        for icon in ('📦 ', '🔑 ', '🏠 '):
            if name.startswith(icon):
                name = name[len(icon):]
                break
        return super().name_search(name=name, args=args, operator=operator, limit=limit)