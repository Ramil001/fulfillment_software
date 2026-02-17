# -*- coding: utf-8 -*-
import logging
from odoo import models, api

_logger = logging.getLogger(__name__)


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

   
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info(f"[create]")
        warehouses = super().create(vals_list)

        for wh in warehouses:
            try:
                self._update_return_types_for_warehouse(wh)
            except Exception as e:
                _logger.error("[Warehouse][CREATE][ERROR] %s", e)

        return warehouses

    
    def write(self, vals):
        _logger.info(f"[write]")
        res = super().write(vals)
        trigger_fields = {
            "fulfillment_warehouse_id",
            "partner_id",
            "company_id",
            "fulfillment_owner_id",
            "fulfillment_client_id",
        }

        if trigger_fields.intersection(vals.keys()):
            changed = trigger_fields.intersection(vals.keys())

            for wh in self:
                _logger.info(
                    "[Warehouse][WRITE] Recomputing picking types for %s (changed: %s)",
                    wh.name,
                    list(changed),
                )
                try:
                    self._update_return_types_for_warehouse(wh)
                except Exception as e:
                    _logger.error("[Warehouse][WRITE][ERROR] %s", e)

        return res

    def _update_return_types_for_warehouse(self, warehouse):
        _logger.info(f"[_update_return_types_for_warehouse]")

        picking_types = self.env["stock.picking.type"].search([
            ('code', '=', 'outgoing'),
            ('warehouse_id', '=', warehouse.id),
        ])

        if not picking_types:
            _logger.warning("[Warehouse][NO OUT] No outgoing picking types for warehouse %s", warehouse.name)
            return

        warehouse_location = warehouse.lot_stock_id
        relevant_picking_types = picking_types.filtered(
            lambda pt: pt.default_location_src_id == warehouse_location
        )

        if not relevant_picking_types:
            _logger.warning("[Warehouse][NO RELEVANT OUT] No outgoing picking types with source location for warehouse %s", warehouse.name)
            return

        return_type = relevant_picking_types[:1]

        for pt in relevant_picking_types:
            pt.return_picking_type_id = return_type.id
            _logger.info(
                "[Warehouse][SET] %s → return_picking_type_id = %s",
                pt.name,
                return_type.name
            )
