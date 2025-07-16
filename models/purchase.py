from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

class FulfillmentPurchase(models.Model):
    _inherit = 'purchase.order'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _logger.info(f"[CREATE OVERRIDE]: {vals}")
            picking_type_id = vals.get('picking_type_id')
            if picking_type_id:
                # Берём ID если он [id], [6,0,[id]] или int
                if isinstance(picking_type_id, list):
                    picking_type_id = picking_type_id[0]

                picking_type = self.env['stock.picking.type'].browse(picking_type_id)
                if picking_type.exists():
                    warehouse = picking_type.warehouse_id
                    if warehouse:
                        _logger.info(f"[CREATE] Picking Type: {picking_type.display_name} → Warehouse: {warehouse.display_name}")
                        _logger.info(f"[CREATE] Warehouse.is_fulfillment = {warehouse.is_fulfillment}")
                    else:
                        _logger.warning(f"[CREATE] Picking Type has NO warehouse!")
                else:
                    _logger.warning(f"[CREATE] Picking Type ID {picking_type_id} does NOT exist!")

        return super().create(vals_list)

    def write(self, vals):
        _logger.warning(f"[WRITE OVERRIDE]: {vals}")

        picking_type_id = vals.get('picking_type_id')
        for order in self:
            pid = picking_type_id or order.picking_type_id.id
            if not pid:
                _logger.info(f"[WRITE] No picking_type_id for PO {order.id}")
                continue

            picking_type = self.env['stock.picking.type'].browse(pid)
            if picking_type.exists():
                warehouse = picking_type.warehouse_id
                if warehouse:
                    _logger.info(f"[WRITE] PO {order.name}: Picking Type → {picking_type.display_name}, Warehouse → {warehouse.display_name}")
                    _logger.info(f"[WRITE] Warehouse.is_fulfillment = {warehouse.is_fulfillment}")
                else:
                    _logger.warning(f"[WRITE] Picking Type {picking_type.display_name} has NO warehouse!")
            else:
                _logger.warning(f"[WRITE] Picking Type ID {pid} does NOT exist!")

        return super().write(vals)
