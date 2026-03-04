from odoo import models, fields, api
from odoo.exceptions import UserError
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
    
    is_consolidate_source = fields.Boolean(
        string="Single Source?", 
        help="Check to collect all products at one warehouse before shipping."
    )

    consolidation_warehouse_id = fields.Many2one(
        'stock.warehouse', 
        string="Ship-from Hub",
        help="The warehouse where all goods will be gathered."
    )

    fulfillment_partner_id = fields.Many2one('res.partner')
    fulfillment_warehouse_id = fields.Many2one('stock.warehouse')
    fulfillment_split = fields.Boolean()
    
    def action_confirm(self):
        _logger.info(f"[action_confirm] Start")
        
        res = super().action_confirm()
        
        StockPicking = self.env['stock.picking']
        StockMove = self.env['stock.move']
        
        profile = self.env['fulfillment.profile'].search([], limit=1)
        client = FulfillmentAPIClient(profile) if profile else None

        for order in self:
            
            if order.is_consolidate_source and order.consolidation_warehouse_id:
                _logger.info(f"[CONSOLIDATION] Start for {order.name}")
                
                auto_pickings = order.picking_ids.filtered(
                    lambda p: p.state not in ('done', 'cancel')
                )
                auto_pickings.action_cancel()
                auto_pickings.unlink()

                order._create_consolidated_flow()
                continue 

            if not profile:
                _logger.warning("[FULFILLMENT] Профиль не найден, пропускаем внешнюю синхронизацию.")
                continue
                
            grouped_lines = {}
            for line in order.order_line:
                partner = line.fulfillment_item_manager
                if partner:
                    grouped_lines.setdefault(partner, []).append(line)
            if not grouped_lines:
                _logger.info(f"[FULFILLMENT][ORDER {order.name}] Нет Fulfillment-партнёров — пропуск.")
                continue
            for partner, lines in grouped_lines.items():
                for line in lines:
                    product = line.product_id
                    tmpl = product.product_tmpl_id
                    if tmpl.fulfillment_product_id:
                        continue  
                    product_payload = {
                        "name": tmpl.name,
                        "sku": tmpl.default_code or f"SKU-{tmpl.id}",
                        "barcode": tmpl.barcode or str(tmpl.id).zfill(6),
                    }
                    try:
                        resp = client.product.create(product_payload)
                        _logger.info("[Fulfillment][SaleOrder][Product][Create] %s -> %s", tmpl.name, resp)

                        if resp and resp.get("status") == "success":
                            new_id = resp["data"].get("product_id")

                            if new_id:
                                tmpl.product_variant_id.fulfillment_product_id = new_id
                                _logger.info(
                                    "[Fulfillment][SaleOrder] Saved product_id %s for %s",
                                    new_id, tmpl.name
                                )
                            else:
                                _logger.warning(
                                    "[Fulfillment][SaleOrder] No product_id in API response for %s",
                                    tmpl.name
                                )

                    except FulfillmentAPIError as e:
                        _logger.error(
                            "[Fulfillment][SaleOrder][Product][API Error] %s: %s",
                            tmpl.name, e
                        )

                    except Exception as e:
                        _logger.exception(
                            "[Fulfillment][SaleOrder][Product][Unexpected] %s: %s",
                            tmpl.name, e
                        )
                warehouse = lines[0].fulfillment_item_warehouse
                if not warehouse:
                    _logger.warning(f"[FULFILLMENT][ORDER {order.name}] Нет склада для {partner.name} — пропуск.")
                    continue
                picking_type = warehouse.out_type_id
                if not picking_type:
                    _logger.warning(
                        f"[FULFILLMENT][ORDER {order.name}] Нет picking_type для склада {warehouse.name}."
                    )
                    continue
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
                _logger.info(
                    f"[FULFILLMENT][ORDER {order.name}] Создан picking {picking.name} для {partner.name}"
                )
                move_items = []
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
                    move_items.append({
                        "product_id": (
                            line.product_id.fulfillment_product_id
                            or line.product_id.default_code
                            or str(line.product_id.id)
                        ),
                        "quantity": int(line.product_uom_qty),
                        "unit": line.product_uom.name or "Units",
                    })
                # --- Создаём трансфер через Fulfillment API ---
                try:
                    receiver_id = order.partner_shipping_id.fulfillment_contact_id
                    fulfillment_partner = warehouse.fulfillment_owner_id

                    if not fulfillment_partner or not fulfillment_partner.fulfillment_id:
                        _logger.warning(
                            f"[FULFILLMENT] У склада {warehouse.name} нет связанного fulfillment_id"
                        )
                        continue

                    payload = {
                        "reference": picking.name,
                        "transfer_type": "outgoing",
                        "fulfillment_out":  warehouse.fulfillment_owner_id,
                        "warehouse_out": (warehouse.fulfillment_warehouse_id or "None"),
                        "status": "confirmed",
                        "items": move_items,
                    }
                    if receiver_id:
                        payload["contacts"] = [{
                            "contact_id": receiver_id,
                            "role": "CUSTOMER"
                        }]
                    response = client.transfer.create(payload)
                    transfer_id = response.get("data", {}).get("id")
                    if transfer_id:
                        picking.write({'fulfillment_transfer_id': transfer_id})
                        _logger.info(
                            f"[FULFILLMENT][SYNC] Трансфер {transfer_id} успешно создан в API."
                        )
                    else:
                        _logger.warning(
                            f"[FULFILLMENT][SYNC] API не вернул transfer_id для {picking.name}"
                        )
                except FulfillmentAPIError as e:
                    _logger.error(
                        f"[FULFILLMENT][ERROR] Ошибка API при создании трансфера {picking.name}: {e}"
                    )
                except Exception as e:
                    _logger.exception(
                        f"[FULFILLMENT][UNEXPECTED] Ошибка при отправке трансфера {picking.name}: {e}"
                    )
            self.action_lock()
        return res


    def action_unlock(self):
        _logger.info(f"[action_unlock]")
        raise UserError("Разблокировка заказа запрещена.")
    
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info(f"[create]")
        records = super(FulfillmentOrder, self).create(vals_list)
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT] Профиль интеграции не найден, пропускаем синхронизацию.")
            return records
        client = FulfillmentAPIClient(profile)
        for order in records:
            try:
                partner = order.partner_id
                if not partner.fulfillment_contact_id:
                    contact_payload = {
                        "type": "CUSTOMER",
                        "name": partner.name,
                        "email": partner.email or "",
                        "phone": partner.phone or "",
                        "street": partner.street or "",
                        "street2": partner.street2 or "",
                        "city": partner.city or "",
                        "zip": partner.zip or "",
                        "country": partner.country_id.name if partner.country_id else "",
                        "isCompany": partner.is_company,
                        "companyName": partner.name if partner.is_company else None,
                        "parentId": None,
                    }
                    try:
                        _logger.info(f"[FULFILLMENT][CONTACT][CREATE] Payload: {contact_payload}")
                        contact_resp = client.contact.create(contact_payload)
                        _logger.info(f"[FULFILLMENT][CONTACT][CREATE] Response: {contact_resp}")

                        contact_id = (
                            contact_resp.get("data", {}).get("id")
                            if isinstance(contact_resp, dict)
                            else None
                        )
                        if contact_id:
                            partner.write({"fulfillment_contact_id": contact_id})
                            _logger.info(
                                f"[FULFILLMENT][CONTACT] Saved contact_id {contact_id} for partner {partner.name}"
                            )
                        else:
                            _logger.warning(
                                f"[FULFILLMENT][CONTACT] API returned no id for partner {partner.name}"
                            )
                    except FulfillmentAPIError as e:
                        _logger.error(f"[FULFILLMENT][CONTACT][ERROR] Ошибка API: {e}")
                    except Exception as e:
                        _logger.exception(f"[FULFILLMENT][CONTACT][UNEXPECTED]: {e}")
                payload = {
                    "external_order_id": order.name,
                    "notes": order.note or "",
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
                    "contacts": [
                        {
                            "role": "customer",
                            "contact_id": order.partner_id.fulfillment_contact_id
                        },
                        *[
                            {
                                "role": "delivery",
                                "contact_id": line.fulfillment_item_manager.partner_id.fulfillment_contact_id
                            }
                            for line in order.order_line
                            if line.fulfillment_item_manager and line.fulfillment_item_manager.partner_id.fulfillment_contact_id
                        ]
                    ]
                }
                response = client.order.create(payload)
                _logger.info(f"[create] payload: {payload}")
                _logger.info(f"[create] response: {response}")
                fulfillment_id = response.get("data", {}).get("id")
                order.write({
                    "fulfillment_order_id": fulfillment_id
                })
            except FulfillmentAPIError as e:
                _logger.error(f"[FULFILLMENT][ERROR] Ошибка синхронизации заказа {order.name}: {e}")
            except Exception as e:
                _logger.exception(f"[FULFILLMENT][UNEXPECTED] Ошибка при отправке заказа {order.name}: {e}")
        return records

    def _create_consolidated_flow(self):
        self.ensure_one()
        StockPicking = self.env['stock.picking']
        
        # 1. Создаем ГЛАВНУЮ отгрузку (Из Хаба к Клиенту)
        out_picking_type = self.consolidation_warehouse_id.out_type_id
        customer_picking = StockPicking.create({
            'partner_id': self.partner_shipping_id.id,
            'picking_type_id': out_picking_type.id,
            'location_id': out_picking_type.default_location_src_id.id,
            'location_dest_id': self.partner_id.property_stock_customer.id,
            'origin': self.name,
            'sale_id': self.id,
        })

        # 2. Группируем строки по складам, чтобы не плодить лишние трансферы
        lines_by_warehouse = {}
        for line in self.order_line:
            wh = line.preferred_warehouse_id # Склад из строки
            if wh:
                lines_by_warehouse.setdefault(wh, []).append(line)

        # 3. Создаем ВНУТРЕННИЕ перемещения (Склады строк -> Хаб)
        for warehouse, lines in lines_by_warehouse.items():
            # Если склад строки совпадает с хабом — перемещение не нужно, 
            # просто добавляем товар в финальную отгрузку
            if warehouse == self.consolidation_warehouse_id:
                self._create_moves_for_picking(customer_picking, lines)
                continue

            # Создаем внутренний трансфер
            internal_type = warehouse.int_type_id
            internal_picking = StockPicking.create({
                'partner_id': self.company_id.partner_id.id,
                'picking_type_id': internal_type.id,
                'location_id': warehouse.lot_stock_id.id,
                'location_dest_id': self.consolidation_warehouse_id.lot_stock_id.id,
                'origin': f"Consolidation: {self.name}",
                'sale_id': self.id,
            })
            
            self._create_moves_for_picking(internal_picking, lines)
            self._create_moves_for_picking(customer_picking, lines) # Добавляем в план отгрузки хаба

            internal_picking.action_confirm()
            internal_picking.action_assign()

        customer_picking.action_confirm()
        customer_picking.action_assign()

    def _create_moves_for_picking(self, picking, lines):
        """Вспомогательный метод для создания Stock Move"""
        for line in lines:
            self.env['stock.move'].create({
                'name': line.name,
                'product_id': line.product_id.id,
                'product_uom_qty': line.product_uom_qty,
                'product_uom': line.product_uom.id,
                'picking_id': picking.id,
                'location_id': picking.location_id.id,
                'location_dest_id': picking.location_dest_id.id,
                'sale_line_id': line.id,
            })
            

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'
    
    preferred_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Availiable Warehouse',
        help='Availiable warehouse with stock'
    )

    warehouse_filter_ids = fields.Many2many(
        'stock.warehouse',
        compute='_compute_warehouse_filter_ids',
        store=False
    )

    @api.depends('product_id')
    def _compute_warehouse_filter_ids(self):
        for line in self:
            if not line.product_id:
                line.warehouse_filter_ids = False
                continue

            warehouses = self.env['stock.warehouse'].search([])

            available_warehouses = warehouses.filtered(
                lambda w: self.env['stock.quant'].search_count([
                    ('product_id', '=', line.product_id.id),
                    ('location_id', 'child_of', w.lot_stock_id.id),
                    ('quantity', '>', 0)
                ]) > 0
            )

            line.warehouse_filter_ids = available_warehouses
            
            
            
    fulfillment_item_manager = fields.Many2one(
        'fulfillment.partners',
        string='Warehouses',
        help='Кто отправляет этот товар',
    )
    fulfillment_line_id = fields.Char(
        string="Fulfillment Line ID",
        readonly=True,
        copy=False,
    )
    fulfillment_item_warehouse = fields.Many2one(
        'stock.warehouse',
        string='Location',
        help='Склад, принадлежащий выбранному Fulfillment-партнёру',
    )




    @api.onchange('fulfillment_item_manager')
    def _onchange_fulfillment_item_manager(self):
        _logger.info(f"[_onchange_fulfillment_item_manager]")
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
        _logger.info(f"[create]")
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
        _logger.info(f"[write]")
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
        _logger.info(f"[_auto_init]")
        res = super()._auto_init()
        query = """
        UPDATE sale_order_line
        SET fulfillment_item_manager = NULL
        WHERE fulfillment_item_manager IS NOT NULL
          AND fulfillment_item_manager NOT IN (SELECT id FROM fulfillment_partners)
        """
        self.env.cr.execute(query)
        _logger.info("[FULFILLMENT][CLEANUP] Все битые ссылки fulfillment_item_manager очищены")
        return res