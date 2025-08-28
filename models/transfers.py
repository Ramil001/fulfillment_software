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



    def create(self, vals):
        _logger.info(f"[Fulfillment][Create] Stock Picking CREATE called with vals={vals}")
        record = super(FulfillmentTransfers, self).create(vals)

        if record.move_ids:
            try:
                profile = self.env['fulfillment.profile'].search([], limit=1)
                if not profile:
                    _logger.warning("[Fulfillment][Create] Profile not found, skipping API call")
                    return record

                fulfillment_api = FulfillmentAPIClient(profile)

                # используем transfers, как у purchase
                payload = {
                    "reference": record.name,
                    "warehouse_in": record.location_dest_id.id,
                    "warehouse_out": record.location_id.id,
                    "status": "draft",
                    "items": [
                        {
                            "product_id": move.product_id.default_code,
                            "quantity": move.product_uom_qty,
                            "unit": move.product_uom.name
                        }
                        for move in record.move_ids
                    ]
                }

                response = fulfillment_api.transfer.create(payload)  # <-- вот здесь
                record.fulfillment_transfer_id = response.get("transfer_id", "Empty")
                _logger.info(f"[Fulfillment][Create] API transfer created with ID {record.fulfillment_transfer_id}")
            except Exception as e:
                _logger.error(f"[Fulfillment][Create] API create failed: {e}")

        return record




    def write(self, vals):
        _logger.info(f"[Fulfillment][Update] Stock Picking {self.ids} WRITE called with vals={vals}")
        res = super(FulfillmentTransfers, self).write(vals)

        for picking in self:
            if picking.fulfillment_transfer_id and picking.move_ids:
                try:
                    profile = self.env['fulfillment.profile'].search([], limit=1)
                    if not profile:
                        _logger.warning("[Fulfillment][Update] Profile not found, skipping API call")
                        continue

                    fulfillment_api = FulfillmentAPIClient(profile)

                    items = []
                    for move in picking.move_ids:
                        tmpl = move.product_id.product_tmpl_id
                        # Проверка: есть ли поле fulfillment_product_id в модели
                        if 'fulfillment_product_id' not in tmpl._fields:
                            _logger.error(
                                f"[Fulfillment][Check] Model product.template has no field 'fulfillment_product_id'. "
                                f"Product '{tmpl.name}' (tmpl_id={tmpl.id})"
                            )
                            continue

                        # Если поле есть → проверяем его значение
                        if tmpl.fulfillment_product_id:
                            _logger.info(
                                f"[Fulfillment][Check] Product '{tmpl.name}' (tmpl_id={tmpl.id}) "
                                f"fulfillment_product_id={tmpl.fulfillment_product_id}"
                            )
                        else:
                            _logger.warning(
                                f"[Fulfillment][Check] Product '{tmpl.name}' (tmpl_id={tmpl.id}) "
                                f"has EMPTY fulfillment_product_id"
                            )

                        items.append({
                            "name": move.product_id.name,
                            "product_id": move.product_id.default_code,
                            "quantity": move.product_uom_qty,
                            "unit": move.product_uom.name
                        })

                    payload = {
                        "reference": vals.get("name", picking.name),
                        "warehouse_in": picking.location_dest_id.id,
                        "warehouse_out": picking.location_id.id,
                        "status": vals.get("status", "draft"),
                        "items": items
                    }

                    fulfillment_api.transfer.update(picking.fulfillment_transfer_id, payload)
                    _logger.info(f"[Fulfillment][Update] API transfer {picking.fulfillment_transfer_id} updated")

                except Exception as e:
                    _logger.error(
                        f"[Fulfillment][Update] API update failed for transfer "
                        f"{picking.fulfillment_transfer_id}: {e}"
                    )

        return res
