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
        
        # Вызываем super с блокировкой автопуша в fulfillment API
        res = super(FulfillmentOrder, self.with_context(
            skip_fulfillment_push=True
        )).action_confirm()
        
        profile = self.env['fulfillment.profile'].search([], limit=1)
        client = FulfillmentAPIClient(profile) if profile else None

        for order in self:

            # --- КОНСОЛИДАЦИЯ ---
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

            # --- Группируем строки по складу ---
            grouped_lines = {}
            for line in order.order_line:
                warehouse = line.preferred_warehouse_id
                if warehouse:
                    grouped_lines.setdefault(warehouse, []).append(line)

            # --- Валидация связей партнёр <-> склад для отправки ---
            # `preferred_warehouse_id` определяет физический warehouse_out,
            # а `fulfillment_item_manager` определяет "кто является источником в fulfillment".
            # Они должны соответствовать, иначе уйдёт неверное `warehouse_out` в API.
            for warehouse, lines in grouped_lines.items():
                for line in lines:
                    if not line.fulfillment_item_manager:
                        continue
                    if not line.fulfillment_item_warehouse:
                        raise UserError(
                            f"[Fulfillment] Для строки '{line.name}' выбран партнёр отправки, но не выбран/не найден склад в fulfillment-профиле партнёра."
                        )
                    if line.fulfillment_item_warehouse and line.preferred_warehouse_id and line.preferred_warehouse_id != line.fulfillment_item_warehouse:
                        raise UserError(
                            f"[Fulfillment] Для строки '{line.name}' выбран склад '{line.preferred_warehouse_id.name}', но fulfillment_item_warehouse для партнёра: '{line.fulfillment_item_warehouse.name}'. "
                            f"Выберите согласованный preferred_warehouse_id (или исправьте fulfillment_item_manager)."
                        )

            if not grouped_lines:
                _logger.info(f"[FULFILLMENT][ORDER {order.name}] Нет доступных складов — пропуск.")
                continue

            # --- Удаляем стандартные pickings созданные Odoo ---
            std_pickings = order.picking_ids.filtered(
                lambda p: p.state not in ('done', 'cancel')
                and (not p.fulfillment_transfer_id or p.fulfillment_transfer_id == 'Empty')
            )
            if std_pickings:
                _logger.info(
                    f"[FULFILLMENT][ORDER {order.name}] Удаляем стандартные pickings: {std_pickings.mapped('name')}"
                )
                std_pickings.action_cancel()
                std_pickings.unlink()

            # --- Создаём продукты если нужно ---
            for warehouse, lines in grouped_lines.items():
                for line in lines:
                    tmpl = line.product_id.product_tmpl_id
                    if tmpl.fulfillment_product_id:
                        continue
                    product_payload = {
                        "name": tmpl.name,
                        "sku": tmpl.default_code or f"SKU-{tmpl.id}",
                        "barcode": tmpl.barcode or str(tmpl.id).zfill(6),
                    }
                    try:
                        resp = client.product.create(product_payload)
                        if resp and resp.get("data", {}).get("id"):
                            tmpl.product_variant_id.fulfillment_product_id = resp["data"]["id"]
                            _logger.info("[Fulfillment][Product][Create] %s -> %s", tmpl.name, resp["data"]["id"])
                    except FulfillmentAPIError as e:
                        _logger.error("[Fulfillment][Product][API Error] %s: %s", tmpl.name, e)
                    except Exception as e:
                        _logger.exception("[Fulfillment][Product][Unexpected] %s: %s", tmpl.name, e)

            # --- Создаём кастомные pickings и трансферы ---
            for warehouse, lines in grouped_lines.items():
                picking_type = warehouse.out_type_id
                if not picking_type:
                    _logger.warning(f"[FULFILLMENT][ORDER {order.name}] Нет picking_type для склада {warehouse.name}.")
                    continue

                # Создаём picking с блокировкой автопуша
                picking = self.env['stock.picking'].with_context(
                    skip_fulfillment_push=True
                ).create({
                    'partner_id': order.partner_id.id,
                    'origin': order.name,
                    'picking_type_id': picking_type.id,
                    'location_id': picking_type.default_location_src_id.id,
                    'location_dest_id': order.partner_id.property_stock_customer.id,
                    'sale_id': order.id,
                })
                _logger.info(f"[FULFILLMENT][ORDER {order.name}] Создан picking {picking.name} для склада {warehouse.name}")

                # --- Создаём moves и собираем items для API ---
                move_items = []
                for line in lines:
                    self.env['stock.move'].create({
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

                # --- Отправляем трансфер в API ---
                try:
                    fulfillment_owner_out = warehouse.fulfillment_owner_id
                    if not fulfillment_owner_out or not fulfillment_owner_out.fulfillment_id:
                        _logger.warning(
                            f"[FULFILLMENT] Warehouse {warehouse.name} не имеет fulfillment_owner_id, пропуск трансфера"
                        )
                        continue

                    payload = {
                        # Use the picking name (e.g. htf/OUT/00035) so the
                        # receiving Odoo instance shows the same transfer name.
                        "reference": picking.name,
                        "transfer_type": "outgoing",
                        "warehouse_out": warehouse.fulfillment_warehouse_id or None,
                        "warehouse_in": None,
                        "fulfillment_out": fulfillment_owner_out.fulfillment_id,
                        # Mark ourselves as the sender so the receiving instance
                        # can identify the reply target for messages.
                        "fulfillment_in": profile.fulfillment_profile_id or None,
                        "items": move_items,
                    }

                    receiver_id = order.partner_shipping_id.fulfillment_contact_id
                    if receiver_id:
                        payload["contacts"] = [{"contact_id": receiver_id, "role": "DELIVERY"}]

                    _logger.info(f"[FULFILLMENT][PAYLOAD] {payload}")

                    response = client.transfer.create(payload)
                    transfer_id = response.get("data", {}).get("id")

                    if transfer_id:
                        picking.with_context(skip_fulfillment_push=True).write({
                            'fulfillment_transfer_id': transfer_id,
                            'fulfillment_transfer_owner_id': payload.get('fulfillment_in') or 'Empty',
                            'fulfillment_transfer_out_id': payload.get('fulfillment_out') or 'Empty',
                        })
                        _logger.info(f"[FULFILLMENT][SYNC] Transfer {transfer_id} создан для {picking.name}")
                    else:
                        _logger.warning(f"[FULFILLMENT][SYNC] API не вернул transfer_id для {picking.name}")

                except FulfillmentAPIError as e:
                    _logger.error(f"[FULFILLMENT][ERROR] {picking.name}: {e}")
                except Exception as e:
                    _logger.exception(f"[FULFILLMENT][UNEXPECTED] {picking.name}: {e}")

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
        
        out_picking_type = self.consolidation_warehouse_id.out_type_id
        customer_picking = StockPicking.create({
            'partner_id': self.partner_shipping_id.id,
            'picking_type_id': out_picking_type.id,
            'location_id': out_picking_type.default_location_src_id.id,
            'location_dest_id': self.partner_id.property_stock_customer.id,
            'origin': self.name,
            'sale_id': self.id,
        })

        lines_by_warehouse = {}
        for line in self.order_line:
            wh = line.preferred_warehouse_id # Склад из строки
            if wh:
                lines_by_warehouse.setdefault(wh, []).append(line)

        for warehouse, lines in lines_by_warehouse.items():
            if warehouse == self.consolidation_warehouse_id:
                self._create_moves_for_picking(customer_picking, lines)
                continue

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
            self._create_moves_for_picking(customer_picking, lines)

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
    
    
    warehouse_display = fields.Char(
        string='Source Stock',
        compute='_compute_warehouse_display',
        store=False
    )

    def _compute_warehouse_display(self):
        for line in self:
            if line.preferred_warehouse_id and line.product_id:
                qty = line.product_id.with_context(
                    location=line.preferred_warehouse_id.lot_stock_id.id
                ).qty_available
                line.warehouse_display = f"{line.preferred_warehouse_id.name} ({qty:.0f})"
            else:
                line.warehouse_display = line.preferred_warehouse_id.name or ''


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