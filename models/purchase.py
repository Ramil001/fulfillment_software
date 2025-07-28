from odoo import models, api
import logging
from ..lib.fulfillment_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)

class FulfillmentPurchase(models.Model):
    _inherit = 'purchase.order'

    @api.model_create_multi
    def create(self, vals_list):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        client = FulfillmentAPIClient(profile)

        for vals in vals_list:
            _logger.info(f"[CREATE OVERRIDE]: {vals}")

            # Получаем склад через picking_type
            picking_type_id = vals.get('picking_type_id')
            warehouse_id = None
            fulfillment_warehouse_id = None
            if picking_type_id:
                if isinstance(picking_type_id, list):
                    picking_type_id = picking_type_id[0]
                picking_type = self.env['stock.picking.type'].browse(picking_type_id)
                fulfillment_warehouse_id = picking_type.warehouse_id.fulfillment_warehouse_id
                if picking_type.exists() and picking_type.warehouse_id:
                    warehouse = picking_type.warehouse_id
                    warehouse_id = warehouse.id
                    _logger.info(f"[CREATE] Picking Type: {picking_type.display_name} → Warehouse: {warehouse.display_name}")
                    _logger.info(f"[CREATE] Warehouse.is_fulfillment = {warehouse.is_fulfillment}")
            
            if not warehouse_id:
                _logger.warning("[CREATE] Не удалось определить warehouse_id, пропускаем отправку в API")
                continue

            # Подготовка списка продуктов
            products = []
            for line in vals.get('order_line', []):
                if isinstance(line, (list, tuple)) and len(line) == 3 and line[0] == 0:
                    line_vals = line[2]
                    product_id = line_vals.get('product_id')
                    product_name = line_vals.get('name')
                    quantity = line_vals.get('product_qty')
                    price = line_vals.get('price_unit')

                    # product_id может быть [id, "name"]
                    if isinstance(product_id, (list, tuple)):
                        product_id = product_id[0]
                    
                    products.append({
                        "product_id": product_id,
                        "name": product_name,
                        "quantity": quantity,
                        "price": price,
                        "warehouse_id": picking_type.warehouse_id.fulfillment_warehouse_id,  # <-- ключевой момент
                    })

            # Логируем продукты
            product_names = [p['name'] for p in products]
            _logger.info(f"[CREATE] Products: {product_names}")

            # Payload
            payload = {
                "name": vals.get('origin') or "Auto",
                "products": products,
            }

            # Вызов API
            try:
                client.purchase.post(payload, fulfillment_warehouse_id)
                _logger.info(f"[CREATE] Payload sent to fulfillment API with warehouse_id={warehouse_id}")
            except Exception as e:
                _logger.error(f"[CREATE] API Call FAILED: {e}")

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
