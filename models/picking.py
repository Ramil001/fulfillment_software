# -*- coding: utf-8 -*-
import logging
from odoo import models, api

_logger = logging.getLogger(__name__)


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    # ---------------------------------------------
    # CREATE
    # ---------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        warehouses = super().create(vals_list)

        for wh in warehouses:
            try:
                self._update_return_types_for_warehouse(wh)
            except Exception as e:
                _logger.error("[Warehouse][CREATE][ERROR] %s", e)

        return warehouses

    # ---------------------------------------------
    # WRITE — теперь работает корректно
    # ---------------------------------------------
    def write(self, vals):
        res = super().write(vals)

        # Поля, изменения которых должны пересчитывать return_picking_type_id
        trigger_fields = {
            "fulfillment_warehouse_id",
            "partner_id",
            "company_id",
            "fulfillment_owner_id",
            "fulfillment_client_id",
        }

        # Если какое-либо из них присутствует в vals → запускаем обновление
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

    # ---------------------------------------------
    # Recompute return picking types
    # ---------------------------------------------
    def _update_return_types_for_warehouse(self, warehouse):
        _logger.info("[Warehouse][SCAN] Recompute picking types for %s", warehouse.name)

        picking_types = self.env["stock.picking.type"].search([
            ('warehouse_id', '=', warehouse.id),
            ('code', '=', 'outgoing'),
        ])

        if not picking_types:
            _logger.warning("[Warehouse][NO OUT] No outgoing picking types for warehouse %s", warehouse.name)
            return

        # Первый OUT тип — эталонный
        return_type = picking_types[:1]

        for pt in picking_types:
            pt.return_picking_type_id = return_type.id
            _logger.info(
                "[Warehouse][SET] %s → return_picking_type_id = %s",
                pt.name,
                return_type.name
            )
