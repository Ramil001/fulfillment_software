import logging
import requests
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient
from datetime import datetime

_logger = logging.getLogger(__name__)

class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    is_fulfillment = fields.Boolean(string="Is this for client storage?")
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Fulfillment owner")
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Fulfillment client")
    fulfillment_warehouse_id = fields.Char(string="Fulfillment Software Warehouse Id", readonly=True)
    last_update = fields.Datetime(string='Last Update', readonly=True)

    @api.model
    def write(self, vals):
        for record in self:
            is_fulfillment = vals.get('is_fulfillment', record.is_fulfillment)
            if not is_fulfillment:
                continue

            profile = self.env['fulfillment.profile'].search([], limit=1)
            client = FulfillmentAPIClient(profile)

            payload = {
                'name': record.name,
                'code': record.code,
                'location': 'UKR',
            }
            fulfillment_id = record.fulfillment_client_id.fulfillment_id
            try:
                response = client.warehouse.update(fulfillment_id, record.fulfillment_warehouse_id, payload)
                record.fulfillment_warehouse_id = response['data'].get('warehouse_id')
            except Exception as e:
                _logger.warning(f"Warehouse update failed: {e}")
        vals['last_update'] = datetime.now()
        return super().write(vals)

    @api.model
    def create(self, vals):
        vals['last_update'] = datetime.now()
        warehouse = super().create(vals)

        if not warehouse.is_fulfillment:
            return warehouse

        profile = self.env['fulfillment.profile'].search([], limit=1)
        client = FulfillmentAPIClient(profile)

        payload = {
            'name': warehouse.name,
            'code': warehouse.code,
            'location': 'UKR',
        }
        fulfillment_id = warehouse.fulfillment_client_id.fulfillment_id or profile.fulfillment_profile_id
        try:
            response = client.warehouse.create(fulfillment_id, payload)
            warehouse.fulfillment_warehouse_id = response['data'].get('warehouse_id')
        except Exception as e:
            _logger.error(f"[Fulfillment] Create failed: {e}")

        return warehouse

    @api.model
    def reload_warehouses(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.error("[Fulfillment] Profile not found")
            return False

        client = FulfillmentAPIClient(profile)

        try:
            response = client.warehouse.get()
            data = response.get('data', [])
            if not data:
                _logger.info("[Fulfillment] No warehouses received from API")
                return True

            default_country = self.env.ref('base.ua')
            existing = self.search([('is_fulfillment', '=', True)])
            existing_map = {w.fulfillment_warehouse_id: w for w in existing}
            processed_ids = set()

            for wh in data:
                ext_id = str(wh['warehouse_id'])
                warehouse = existing_map.get(ext_id)
                base_name = wh.get('name', 'Unnamed')
                code = wh.get('code', '')
                unique_name = base_name
                suffix = 1

                while self.search_count([('name', '=', unique_name), ('company_id', '=', self.env.company.id)]):
                    unique_name = f"{base_name} [{code}-{suffix}]"
                    suffix += 1

                country = self.env['res.country'].search([('code', '=', wh.get('location', 'UA'))], limit=1) or default_country
                partner_vals = {
                    'name': unique_name,
                    'country_id': country.id,
                    'is_company': True,
                    'type': 'delivery'
                }

                vals = {
                    'name': unique_name,
                    'code': code,
                    'is_fulfillment': True,
                    'fulfillment_warehouse_id': ext_id,
                }

                if warehouse:
                    if warehouse.partner_id:
                        warehouse.partner_id.write(partner_vals)
                    else:
                        vals['partner_id'] = self.env['res.partner'].create(partner_vals).id
                    warehouse.write(vals)
                    processed_ids.add(warehouse.id)
                else:
                    vals['partner_id'] = self.env['res.partner'].create(partner_vals).id
                    try:
                        new_warehouse = self.create(vals)
                        processed_ids.add(new_warehouse.id)
                        _logger.info(f"[Fulfillment] Created warehouse: {new_warehouse.name}")
                    except Exception as e:
                        _logger.error(f"[Fulfillment] Failed to create: {e}")
                        self.env.cr.rollback()

            # Деактивация неактуальных
            to_deactivate = existing.filtered(lambda w: w.id not in processed_ids)
            to_deactivate.write({'active': False})
            _logger.info(f"[Fulfillment] Deactivated {len(to_deactivate)} obsolete warehouses")

        except Exception as e:
            _logger.error(f"[Fulfillment] Sync error: {e}")
            return False

        return True
