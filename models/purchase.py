from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

class FulfillmentPurchase(models.Model):
    _inherit = 'purchase.order'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _logger.info(f"[CREATE OVERRIDE]: {vals}")

            # Проверка picking_type_id
            picking_type_id = vals.get('picking_type_id')
            if picking_type_id:
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

            # Логируем продукты из order_line, если есть
            order_lines = vals.get('order_line')
            if order_lines:
                product_names = []
                for line in order_lines:
                    # Odoo One2many: (0, 0, {vals})
                    if isinstance(line, (list, tuple)) and len(line) == 3 and line[0] == 0:
                        line_vals = line[2]
                        product_id = line_vals.get('product_id')
                        if product_id:
                            # Если это (ID, NAME) или просто ID
                            if isinstance(product_id, (list, tuple)):
                                product_id = product_id[0]
                            product = self.env['product.product'].browse(product_id)
                            if product.exists():
                                product_names.append(product.display_name)
                _logger.info(f"[CREATE] Products: {product_names}")

        return super().create(vals_list)



    def write(self, vals):
        _logger.warning(f"[WRITE OVERRIDE]: {vals}")

        picking_type_id = vals.get('picking_type_id')
        
        res = super().write(vals)
        
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

            # Логируем продукты из order_line текущего заказа
            line_info = []
            for line in order.order_line:
                if line.product_id:
                    info = (line.product_id.id, line.product_id.display_name, line.product_qty)
                    line_info.append(info)
            _logger.info(f"[WRITE]: PO {order.name}: Products: {line_info} ")

        return res
