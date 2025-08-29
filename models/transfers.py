from odoo import models, api, fields , _
from odoo.exceptions import ValidationError
import logging
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)


class FulfillmentTransfers(models.Model):
    _inherit = 'stock.picking'
    
    fulfillment_transfer_id = fields.Char(string="Fulfillment Transfer ID", default="Empty", help="Fulfillemnt ID for API" ,readonly=True)

    @api.model
    def create_fulfillment_receipt(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.error("[Fulfillment] Profile not found")
            return False

        fulfillmentApiClient = FulfillmentAPIClient(profile)
        try:
            purchases = fulfillmentApiClient.purchase.get()
            _logger.info(f"[PURCHASES]: {purchases}")
        except Exception as e:
            raise ValidationError(_("Fulfillment API error: %s") % str(e))

        partner = self.env['res.partner'].search([], limit=1)
        if not partner:
            partner = self.env['res.partner'].create({'name': 'Fulfillment Partner'})

        picking_type = self.env.ref('stock.picking_type_in', raise_if_not_found=False)
        location_suppliers = self.env.ref('stock.stock_location_suppliers', raise_if_not_found=False)
        location_stock = self.env.ref('stock.stock_location_stock', raise_if_not_found=False)

        for purchase in purchases:
            picking = self.env['stock.picking'].create({
                'partner_id': partner.id,
                'picking_type_id': picking_type.id if picking_type else False,
                'location_id': location_suppliers.id if location_suppliers else False,
                'location_dest_id': location_stock.id if location_stock else False,
                'origin': purchase['name'],
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

    def _get_or_create_fulfillment_warehouse(self, location):
        """Возвращает fulfillment_warehouse_id по location. Если нет — создаёт."""
        if not location:
            return None

        warehouse = location.warehouse_id  # из stock.location получаем warehouse
        if not warehouse:
            _logger.warning(f"[Fulfillment] No warehouse linked to location {location.name}")
            return None

        # Если уже есть fulfillment_warehouse_id, возвращаем
        if warehouse.fulfillment_warehouse_id:
            return warehouse.fulfillment_warehouse_id

        # Иначе создаём через API
        payload = {
            "name": warehouse.name,
            "code": warehouse.code or f"WH-{warehouse.id}", 
            "location": warehouse.lot_stock_id.name
        }

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found, cannot create warehouse")
            return None

        client = FulfillmentAPIClient(profile)
        try:
            response = client.warehouse.create(profile.fulfillment_profile_id, payload)
            data = response.get("data", {})
            
            warehouse_id = data.get("warehouse_id")
            
            if warehouse_id:
                warehouse.fulfillment_warehouse_id = warehouse_id
                _logger.info(
                    f"[Fulfillment] Created fulfillment warehouse {warehouse.name} → {warehouse_id}"
                )
                return warehouse_id
            else:
                _logger.warning(
                    f"[Fulfillment] API did not return warehouse_id, response={response}"
                )
        except Exception as e:
            _logger.error(f"[Fulfillment] Failed to create fulfillment warehouse: {e}")

        return None



    def _get_fulfillment_warehouse_id(self, location):
        if not location:
            return None

        warehouse = self.env['stock.warehouse'].search([
            ('lot_stock_id', '=', location.id)
        ], limit=1)

        if not warehouse:
            return None

        return self._get_or_create_fulfillment_warehouse(warehouse)



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

                fulfillment_api = FulfillmentAPIClient(profile)

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
                            response = fulfillment_api.product.create(product_payload)
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


                payload = {
                    "reference": vals.get("name", picking.name),
                    "warehouse_in": self._get_or_create_fulfillment_warehouse(picking.location_dest_id),
                    "warehouse_out": self._get_or_create_fulfillment_warehouse(picking.location_id),
                    "status": vals.get("status", picking.state or "draft"),
                    "items": items
                }

                # проверяем transfer_id
                if not picking.fulfillment_transfer_id or picking.fulfillment_transfer_id == "Empty":
                    # создаём новый
                    response = fulfillment_api.transfer.create(payload)
                    picking.fulfillment_transfer_id = response.get("transfer_id", "Empty")
                    _logger.info(f"[Fulfillment][Create] API transfer created with ID {picking.fulfillment_transfer_id}")
                else:
                    # обновляем существующий
                    fulfillment_api.transfer.update(picking.fulfillment_transfer_id, payload)
                    _logger.info(f"[Fulfillment][Update] API transfer {picking.fulfillment_transfer_id} updated")

            except Exception as e:
                _logger.error(f"[Fulfillment][Update] API update failed for transfer {picking.fulfillment_transfer_id}: {e}")

        return res
