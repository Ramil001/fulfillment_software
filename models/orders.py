from odoo import models, fields, api
import logging
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError


_logger = logging.getLogger(__name__)



class FulfillmentOrder(models.Model):
    _inherit = 'sale.order'

    fulfillment_order_id = fields.Char(
        string="Fulfillment Order ID",
        readonly=True,
        copy=False,
        index=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Создание заказа и синхронизация с Fulfillment API"""
        _logger.info(f"[DEBUG][ORDER][CREATE]: {vals_list}")
        
        records = super(FulfillmentOrder, self).create(vals_list)
        
        # пример логики после создания
        for order in records:
            _logger.info(f"[FULFILLMENT] Создан заказ: {order.name} (ID {order.id})")
            # тут можно добавить синхронизацию с API, если нужно
        
        return records

            

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fulfillment_item_manager = fields.Many2one(
        'res.partner',
        string='Fulfillment Delivery',
        help='Кто отправляет этот товар',
        domain="[('fulfillment_warehouse_id', '!=', False)]",
    )
    fulfillment_line_id = fields.Char(
        string="Fulfillment Line ID",
        readonly=True,
        copy=False,
    )
