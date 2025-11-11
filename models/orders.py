from odoo import models, fields, api, tools
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



    def action_confirm(self):
        """Переопределяем подтверждение заказа:
        создаёт отдельный исходящий складской документ для каждого fulfillment-партнёра.
        """
        res = super().action_confirm()

        StockPicking = self.env['stock.picking']

        for order in self:
            # Собираем строки по fulfillment-партнёрам
            grouped_lines = {}
            for line in order.order_line:
                partner = line.fulfillment_item_manager
                if not partner:
                    continue
                grouped_lines.setdefault(partner, []).append(line)

            if not grouped_lines:
                _logger.info(f"[FULFILLMENT][ORDER {order.name}] Нет Fulfillment-партнёров — пропуск.")
                continue

            for partner, lines in grouped_lines.items():
                # Берём первый склад у этого партнёра
                warehouse = lines[0].fulfillment_item_warehouse
                if not warehouse:
                    _logger.warning(f"[FULFILLMENT][ORDER {order.name}] Нет склада для {partner.name} — пропуск.")
                    continue

                picking_type = warehouse.out_type_id
                if not picking_type:
                    _logger.warning(f"[FULFILLMENT][ORDER {order.name}] Нет picking_type для склада {warehouse.name}.")
                    continue

                # Создаём stock.picking
                picking_vals = {
                    'partner_id': order.partner_id.id,
                    'origin': order.name,
                    'picking_type_id': picking_type.id,
                    'location_id': picking_type.default_location_src_id.id,
                    'location_dest_id': order.partner_id.property_stock_customer.id,
                    'fulfillment_partner_id': partner.id,
                    'fulfillment_warehouse_id': warehouse.id,
                    'sale_id': order.id,
                }

                picking = StockPicking.create(picking_vals)
                _logger.info(f"[FULFILLMENT][ORDER {order.name}] Создан picking {picking.name} для {partner.name}")

                StockMove = self.env['stock.move']
                for line in lines:
                    StockMove.create({
                        'picking_id': picking.id,
                        'name': line.name,
                        'product_id': line.product_id.id,
                        'product_uom_qty': line.product_uom_qty,
                        'product_uom': line.product_uom.id,
                        'location_id': picking.location_id.id,
                        'location_dest_id': picking.location_dest_id.id,
                        'sale_line_id': line.id,
                    })



        return res

    @api.model_create_multi
    def create(self, vals_list):
        """Создание заказа и синхронизация с Fulfillment API"""
        _logger.info(f"[DEBUG][ORDER][CREATE]: {vals_list}")

        records = super(FulfillmentOrder, self).create(vals_list)

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT] Профиль интеграции не найден, пропускаем синхронизацию.")
            return records

        client = FulfillmentAPIClient(profile)

        for order in records:
            try:
                payload = {
                    "order_name": order.name,
                    "order_id": order.id,
                    "items": [
                        {
                            "product_id": (
                                line.product_id.fulfillment_product_id
                                or line.product_id.default_code
                                or str(line.product_id.id)
                            ),
                            "quantity": int(line.product_uom_qty),
                            "fulfillment_partner_id": (
                                line.fulfillment_item_manager.fulfillment_id
                                if line.fulfillment_item_manager and line.fulfillment_item_manager.exists()
                                else None
                            ),
                        }
                        for line in order.order_line
                    ],
                    "total": float(order.amount_total),
                    "currency": order.currency_id.name or "UAH",
                }

                _logger.info(f"[FULFILLMENT][SYNC] Payload для API: {payload}")
                response = client.order.create(payload)
                _logger.info(f"[FULFILLMENT][SYNC] Ответ API: {response}")
                
                api_order = response.get("order", {})
                fulfillment_id = api_order.get("order_id") or api_order.get("id")

                order.write({
                    "fulfillment_order_id": fulfillment_id
                    }) 

            except FulfillmentAPIError as e:
                _logger.error(f"[FULFILLMENT][ERROR] Ошибка синхронизации заказа {order.name}: {e}")
            except Exception as e:
                _logger.exception(f"[FULFILLMENT][UNEXPECTED] Ошибка при отправке заказа {order.name}: {e}")

        return records

            

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    fulfillment_item_manager = fields.Many2one(
        'fulfillment.partners',
        string='Fulfillment Delivery',
        help='Кто отправляет этот товар',
    )
    fulfillment_line_id = fields.Char(
        string="Fulfillment Line ID",
        readonly=True,
        copy=False,
    )
    
    fulfillment_item_warehouse = fields.Many2one(
        'stock.warehouse',
        string='Warehouse Fulfillment',
        help='Склад, принадлежащий выбранному Fulfillment-партнёру',
        domain="[('fulfillment_owner_id', '=', fulfillment_item_manager)]",
    )


    @api.onchange('fulfillment_item_manager')
    def _onchange_fulfillment_item_manager(self):
        """Автоматически выбирает склад fulfillment_item_warehouse при смене партнёра"""
        for line in self:
            if not line.fulfillment_item_manager:
                line.fulfillment_item_warehouse = False
                return

            partner = line.fulfillment_item_manager
            _logger.info(f"[ONCHANGE] Выбран партнёр {partner.name}")

            warehouse = self.env['stock.warehouse'].search([
                ('fulfillment_owner_id', '=', partner.id)
            ], limit=1)

            if not warehouse:
                warehouse = self.env['stock.warehouse'].search([
                    ('fulfillment_client_id', '=', partner.id)
                ], limit=1)

            if warehouse:
                line.fulfillment_item_warehouse = warehouse.id
                _logger.info(f"[AUTO] Для партнёра {partner.name} выбран склад {warehouse.name}")
            else:
                line.fulfillment_item_warehouse = False
                _logger.warning(f"[AUTO] Для партнёра {partner.name} не найден склад")
                
                
    @api.model_create_multi
    def create(self, vals_list):
        """Перед созданием — проверяем корректность fulfillment_item_manager"""
        for vals in vals_list:
            if vals.get("fulfillment_item_manager"):
                partner_exists = self.env['fulfillment.partners'].browse(
                    vals["fulfillment_item_manager"]
                ).exists()
                if not partner_exists:
                    _logger.warning(
                        f"[FULFILLMENT][CLEANUP] Партнёр {vals['fulfillment_item_manager']} не найден, поле очищено"
                    )
                    vals["fulfillment_item_manager"] = False
        return super().create(vals_list)

    def write(self, vals):
        """Перед обновлением — проверяем корректность fulfillment_item_manager"""
        if vals.get("fulfillment_item_manager"):
            partner_exists = self.env['fulfillment.partners'].browse(
                vals["fulfillment_item_manager"]
            ).exists()
            if not partner_exists:
                _logger.warning(
                    f"[FULFILLMENT][CLEANUP] Некорректный партнёр {vals['fulfillment_item_manager']} — очищаем"
                )
                vals["fulfillment_item_manager"] = False
        return super().write(vals)

    @api.model
    def _auto_init(self):
        """Исправление битых связей при установке/обновлении модуля"""
        res = super()._auto_init()

        # Очистим все несуществующие ссылки
        query = """
        UPDATE sale_order_line
        SET fulfillment_item_manager = NULL
        WHERE fulfillment_item_manager IS NOT NULL
          AND fulfillment_item_manager NOT IN (SELECT id FROM fulfillment_partners)
        """
        self.env.cr.execute(query)
        _logger.info("[FULFILLMENT][CLEANUP] Все битые ссылки fulfillment_item_manager очищены")

        return res