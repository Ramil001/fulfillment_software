from odoo import models, api, fields , _
from odoo.exceptions import ValidationError
import logging
from ..lib.api_client import FulfillmentAPIClient

# Логгер 
_logger = logging.getLogger(__name__)


class FulfillmentTransfers(models.Model):
    _inherit = 'stock.picking'
    
    fulfillment_transfer_id = fields.Char(string="Fulfillment Transfer ID", default="Empty", help="Fulfillemnt ID for API" ,readonly=True)
    
    @property
    def fulfillment_api(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment][Profile not found]")
            return None
        return FulfillmentAPIClient(profile)
    
        
    # Публичные методы
    @api.model
    def write(self, vals):
        _logger.info(f"[Fulfillment][Update] Stock Picking {self.ids} WRITE called with vals={vals}")
        res = super(FulfillmentTransfers, self).write(vals)

        for picking in self:
            if not picking.move_ids:
                continue

            try:
                profile = self.env['fulfillment.profile'].search([], limit=1)
                if not profile:
                    _logger.warning("[Fulfillment][Update] Profile not found, skipping API call")
                    continue

            

                # собираем items
                items = []
                for move in picking.move_ids:
                    tmpl = move.product_id.product_tmpl_id

                    # проверяем поле
                    if 'fulfillment_product_id' not in tmpl._fields:
                        _logger.error(
                            f"[Fulfillment][Check] Model product.template has no field 'fulfillment_product_id'. "
                            f"Product '{tmpl.name}' (tmpl_id={tmpl.id})"
                        )
                        continue

                    # создаём продукт в API если ещё нет
                    if not tmpl.fulfillment_product_id:
                        _logger.warning(
                            f"[Fulfillment][Check] Product '{tmpl.name}' (tmpl_id={tmpl.id}) "
                            f"has EMPTY fulfillment_product_id → creating in API"
                        )

                        product_payload = {
                            "name": tmpl.name,
                            "sku": tmpl.default_code or f"SKU-{tmpl.id}",
                            "barcode": tmpl.barcode or str(tmpl.id).zfill(6)
                        }

                        try:
                            response = self.fulfillment_api.product.create(product_payload)
                            if response.get("status") == "success":
                                product_id = response["data"]["product_id"]
                                tmpl.fulfillment_product_id = product_id
                                _logger.info(
                                    f"[Fulfillment][Create] Product '{tmpl.name}' created in API "
                                    f"with id={product_id} and saved to Odoo"
                                )
                            else:
                                _logger.error(f"[Fulfillment][Create] API response error: {response}")
                                continue
                        except Exception as e:
                            _logger.error(f"[Fulfillment][Create] API product creation failed for '{tmpl.name}': {e}")
                            continue
                    else:
                        _logger.info(
                            f"[Fulfillment][Check] Product '{tmpl.name}' already linked "
                            f"fulfillment_product_id={tmpl.fulfillment_product_id}"
                        )

                    items.append({
                        "name": move.product_id.name,
                        "product_id": tmpl.fulfillment_product_id,
                        "quantity": move.product_uom_qty,
                        "unit": move.product_uom.name
                    })

                if not items:
                    continue
                
                
                
                
                warehouse_out = picking.picking_type_id.warehouse_id
                warehouse_in = self.env['stock.warehouse'].search([
                    ('view_location_id', 'parent_of', picking.location_dest_id.id)
                ], limit=1)


                warehouse_out_id, warehouse_in_id = self._get_transfer_warehouses(picking)

                payload = {
                    "reference": picking.name,
                    "warehouse_out": warehouse_out_id,
                    "warehouse_in": warehouse_in_id,
                    "status": picking.state or "draft",
                    "items": items,
}

                # проверяем transfer_id
                if not picking.fulfillment_transfer_id or picking.fulfillment_transfer_id == "Empty":
                    # создаём новый
                    response = self.fulfillment_api.transfer.create(payload)
                    picking.fulfillment_transfer_id = response.get("transfer_id", "Empty")
                    _logger.info(f"[Fulfillment][Create] API transfer created with ID {picking.fulfillment_transfer_id}")
                else:
                    # обновляем существующий
                    self.fulfillment_api.transfer.update(picking.fulfillment_transfer_id, payload)
                    _logger.info(f"[Fulfillment][Update] API transfer {picking.fulfillment_transfer_id} updated")

            except Exception as e:
                _logger.error(f"[Fulfillment][Update] API update failed for transfer {picking.fulfillment_transfer_id}: {e}")

        return res


    def create_fulfillment_receipt(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.error("[Fulfillment] Profile not found")
            return False

        fulfillmentApiClient = FulfillmentAPIClient(profile)
        try:
            purchases = fulfillmentApiClient.purchase.get()
            _logger.info(f"[PURCHASES]: {purchases}")

            # Добавляем проверку, чтобы не было NoneType
            if not purchases:
                _logger.warning("[Fulfillment] No purchases returned from API")
                return False

        except Exception as e:
            raise ValidationError(_("Fulfillment API error: %s") % str(e))

        partner = self.env['res.partner'].search([], limit=1)
        if not partner:
            partner = self.env['res.partner'].create({'name': 'Fulfillment Partner'})

        picking_type = self.env.ref('stock.picking_type_in', raise_if_not_found=False)
        location_suppliers = self.env.ref('stock.stock_location_suppliers', raise_if_not_found=False)
        location_stock = self.env.ref('stock.stock_location_stock', raise_if_not_found=False)

        for purchase in purchases:
            if not purchase:
                continue  # безопасно пропускаем пустые записи
            picking = self.env['stock.picking'].create({
                'partner_id': partner.id,
                'picking_type_id': picking_type.id if picking_type else False,
                'location_id': location_suppliers.id if location_suppliers else False,
                'location_dest_id': location_stock.id if location_stock else False,
                'origin': purchase.get('name', 'Unknown'),
            })


            for order_line in purchase.get('orders', []):
                product_info = order_line.get('product')
                if not product_info:
                    continue

                product_code = f"FULFILL-[{product_info['id']}]"

                product_template = self.env['product.template'].search([
                    ('default_code', '=', product_code)
                ], limit=1)

                if not product_template:
                    product_template = self.env['product.template'].create({
                        'name': product_info['name'],
                        'default_code': product_code,
                        'type': 'consu',
                    })
                    _logger.info(f"[Fulfillment] Created product template {product_template.name}")

                product_variant = product_template.product_variant_id

                self.env['stock.move'].create({
                    'product_id': product_variant.id,
                    'name': purchase['name'],
                    'product_uom_qty': order_line.get('quantity', 0),
                    'product_uom': product_variant.uom_id.id,
                    'picking_id': picking.id,
                    'location_id': picking.location_id.id,
                    'location_dest_id': picking.location_dest_id.id,
                })

            picking.action_confirm()
            _logger.info(f"[Fulfillment] Created picking {picking.name} from purchase {purchase['name']}")


        return True
    
    
    def load_transfers(self, fulfillment_id=None, page=1, limit=100):
        """
        Загружает трансферы из Fulfillment API и создает внутренние перемещения в Odoo.
        """
        profile = self.env["fulfillment.profile"].search([], limit=1)
        if not profile:
            raise ValidationError(_("Fulfillment profile not found"))

        client = FulfillmentAPIClient(profile)
        fulfillment_id = fulfillment_id or profile.fulfillment_profile_id

        try:
            response = client.fulfillment.get_transfers_by_fulfillment(
                fulfillment_id, params={"page": page, "limit": limit}
            )
        except Exception as e:
            raise ValidationError(_("Fulfillment API error: %s") % str(e))

        if response.get("status") != "success":
            _logger.warning(f"[Fulfillment] Ошибка API: {response}")
            return False

        transfers = response.get("data", [])
        if not transfers:
            _logger.info("[Fulfillment] Нет трансферов для загрузки")
            return True

        picking_type_internal = self.env.ref("stock.picking_type_internal", raise_if_not_found=False)

        for transfer in transfers:
            # Пытаемся найти существующий трансфер
            picking = self.search([("fulfillment_transfer_id", "=", transfer["transfer_id"])], limit=1)

            # --- Склады (создаём при необходимости) ---
            warehouse_out = self.env["stock.warehouse"].search([
                ("fulfillment_warehouse_id", "=", transfer["warehouse_out"])
            ], limit=1)
            if not warehouse_out:
                warehouse_out = self.env["stock.warehouse"].create({
                    "name": f"WH OUT {transfer['warehouse_out'][:6]}",
                    "code": f"OUT-{transfer['warehouse_out'][:3]}",
                    "fulfillment_warehouse_id": transfer["warehouse_out"],
                })

            warehouse_in = self.env["stock.warehouse"].search([
                ("fulfillment_warehouse_id", "=", transfer["warehouse_in"])
            ], limit=1)
            if not warehouse_in:
                warehouse_in = self.env["stock.warehouse"].create({
                    "name": f"WH IN {transfer['warehouse_in'][:6]}",
                    "code": f"IN-{transfer['warehouse_in'][:3]}",
                    "fulfillment_warehouse_id": transfer["warehouse_in"],
                })

            # --- Создание или обновление transfer ---
            if not picking:
                picking = self.create({
                    "picking_type_id": picking_type_internal.id if picking_type_internal else False,
                    "location_id": warehouse_out.lot_stock_id.id,
                    "location_dest_id": warehouse_in.lot_stock_id.id,
                    "origin": transfer["reference"],
                    "fulfillment_transfer_id": transfer["transfer_id"],
                    "state": transfer.get("status", "draft"),
                })
                _logger.info(f"[Fulfillment] Создан новый transfer {picking.name} ({transfer['transfer_id']})")
            else:
                _logger.info(f"[Fulfillment] Обновляем существующий transfer {picking.name} ({transfer['transfer_id']})")
                picking.move_ids.unlink()
                picking.state = transfer.get("status", picking.state)

            # --- Обработка товаров ---
            for item in transfer.get("items", []):
                product_info = item.get("product")
                if not product_info:
                    _logger.warning(f"[Fulfillment] Item {item.get('id')} без product → пропуск")
                    continue

                product_code = product_info.get("sku") or f"FULFILL-{product_info['product_id']}"
                product_barcode = product_info.get("barcode")

                # Ищем продукт
                product_template = self.env["product.template"].search([
                    "|", "|",
                    ("fulfillment_product_id", "=", product_info["product_id"]),
                    ("default_code", "=", product_code),
                    ("barcode", "=", product_barcode)
                ], limit=1)

                if not product_template:
                    # Создаём новый продукт
                    uom_unit = self.env.ref("uom.product_uom_unit")
                    product_template = self.env["product.template"].create({
                        "name": product_info.get("name", "Unnamed Product"),
                        "default_code": product_code,
                        "barcode": product_barcode,
                        "type": "consu",
                        "uom_id": uom_unit.id,
                        "uom_po_id": uom_unit.id,
                        "fulfillment_product_id": product_info["product_id"],
                    })
                    _logger.info(f"[Fulfillment] Создан новый продукт {product_template.name}")
                else:
                    # Обновляем, если есть расхождения
                    if not product_template.fulfillment_product_id:
                        product_template.fulfillment_product_id = product_info["product_id"]

                    vals_update = {}
                    if product_info.get("name") and product_template.name != product_info["name"]:
                        vals_update["name"] = product_info["name"]
                    if product_code and product_template.default_code != product_code:
                        vals_update["default_code"] = product_code
                    if product_barcode and product_template.barcode != product_barcode:
                        vals_update["barcode"] = product_barcode
                    if vals_update:
                        product_template.write(vals_update)
                        _logger.info(f"[Fulfillment] Обновлён продукт {product_template.name}")

                product_variant = product_template.product_variant_id

                # Создаём движение
                self.env["stock.move"].create({
                    "product_id": product_variant.id,
                    "name": transfer["reference"],
                    "product_uom_qty": item.get("quantity", 0),
                    "product_uom": product_variant.uom_id.id,
                    "picking_id": picking.id,
                    "location_id": picking.location_id.id,
                    "location_dest_id": picking.location_dest_id.id,
                })

        
    
    def _get_transfer_warehouses(self, picking):
        """Вернёт (warehouse_out_id, warehouse_in_id)"""
        warehouse_out_id, warehouse_in_id = None, None

        # --- warehouse_out: по source location ---
        if picking.location_id:
            warehouse_out = self.env['stock.warehouse'].search([
                ('view_location_id', 'parent_of', picking.location_id.id)
            ], limit=1)
            if warehouse_out:
                warehouse_out_id = warehouse_out.warehouse_id  # внешний ID

        # --- warehouse_in: если outgoing → по партнёру ---
        if picking.picking_type_code == 'outgoing':
            if picking.partner_id:
                warehouse_in = self.env['stock.warehouse'].search([
                    ('partner_id', '=', picking.partner_id.id)
                ], limit=1)
                if warehouse_in:
                    warehouse_in_id = warehouse_in.warehouse_id

        # --- если incoming/internal → по dest location ---
        else:
            if picking.location_dest_id:
                warehouse_in = self.env['stock.warehouse'].search([
                    ('view_location_id', 'parent_of', picking.location_dest_id.id)
                ], limit=1)
                if warehouse_in:
                    warehouse_in_id = warehouse_in.warehouse_id

        return warehouse_out_id, warehouse_in_id

    # Приватные методы 
    def _get_or_create_fulfillment_warehouse(self, location, client=None, cache=None):
        if not location:
            return None

        warehouse = location.warehouse_id
        if not warehouse:
            _logger.warning(f"[Fulfillment] No warehouse linked to location {location.name}")
            return None

        if warehouse.fulfillment_warehouse_id:
            return warehouse.fulfillment_warehouse_id

        # Используем кэш
        if cache is None:
            cache = {}
        if warehouse.id in cache:
            return cache[warehouse.id]

        # Создаём через API
        if not client:
            profile = self.env['fulfillment.profile'].search([], limit=1)
            if not profile:
                _logger.warning("[Fulfillment] Profile not found, cannot create warehouse")
                return None
            client = FulfillmentAPIClient(profile)

        payload = {
            "name": warehouse.name,
            "code": warehouse.code or f"WH-{warehouse.id}",
            "location": warehouse.lot_stock_id.name
        }

        try:
            response = client.warehouse.create(profile.fulfillment_profile_id, payload)
            warehouse_id = response.get("data", {}).get("warehouse_id")
            if warehouse_id:
                warehouse.fulfillment_warehouse_id = warehouse_id
                cache[warehouse.id] = warehouse_id
                _logger.info(f"[Fulfillment] Created fulfillment warehouse {warehouse.name} → {warehouse_id}")
                return warehouse_id
            else:
                _logger.warning(f"[Fulfillment] API did not return warehouse_id, response={response}")
        except Exception as e:
            _logger.error(f"[Fulfillment] Failed to create fulfillment warehouse: {e}")

        return None


