from odoo import models, fields, api
from datetime import datetime
import requests
import logging

_logger = logging.getLogger(__name__)

class FulfillmentProfile(models.Model):
    _name = 'fulfillment.profile'
    _description = 'Fulfillment Profile'

    name = fields.Char(string="Fulfillment name")
    country_id = fields.Many2one('res.country', string="Country", default=lambda self: self.env.ref('base.de').id)
    state_id = fields.Many2one('res.country.state', string="City/Region", domain="[('country_id', '=', country_id)]")
    address = fields.Char(string="Address")
    phone = fields.Char(string="Phone number")
    email = fields.Char(string="Email")
    capabilities_id = fields.Many2one('fulfillment.profile.capabilities', string="Capabilities", ondelete='cascade')
    
    
    
    fulfillment_api_key = fields.Char(string="X-Fulfillment-API-Key", password=True)
    update_at = fields.Datetime(string="Last Updated")  # УБРАН readonly
    g_fulfillment_id = fields.Char(string="gFulfillment ID", readonly=True)
    domain = fields.Char(string="Domain", default="software.com")

    @api.model
    def create(self, vals):
        vals['update_at'] = datetime.now()
        record = super().create(vals)
        record._sync_with_fulfillment_api()
        return record

    def write(self, vals):
        vals['update_at'] = datetime.now()
        result = super().write(vals)
        self._sync_with_fulfillment_api()
        return result

    def _sync_with_fulfillment_api(self):
        for record in self:
            if not record.fulfillment_api_key:
                _logger.warning("API ключ не задан — пропущен sync.")
                continue

            headers = {
                'Content-Type': 'application/json',
                'X-Fulfillment-API-Key': record.fulfillment_api_key
            }

            payload = {
                "name": record.name or "Default Name",
                "domain": record.domain or "software.com",
            }

            try:
                if record.g_fulfillment_id:
                    url = f"https://api.fulfillment.software/api/v1/fulfillments/{record.g_fulfillment_id}"
                    response = requests.patch(url, json=payload, headers=headers, timeout=10)
                    response.raise_for_status()
                    result = response.json()

                    if result.get("status") == "OK":
                        _logger.info("Fulfillment %s обновлён через PATCH", record.g_fulfillment_id)
                    else:
                        _logger.warning("PATCH — неожиданный ответ: %s", result)
                else:
                    url = "https://api.fulfillment.software/api/v1/fulfillments/"
                    response = requests.post(url, json=payload, headers=headers, timeout=10)
                    response.raise_for_status()
                    result = response.json()

                    if result.get("status") == "OK" and "data" in result:
                        data = result["data"]
                        record.write({
                            "g_fulfillment_id": data.get("fulfillmentId"),
                            "name": data.get("name", record.name),
                            "domain": data.get("domain", record.domain)
                           
                        })
                        _logger.info("Fulfillment создан через POST с ID %s", data.get("fulfillmentId"))
                    else:
                        _logger.warning("POST — неожиданный ответ: %s", result)

            except requests.exceptions.RequestException as e:
                _logger.error("Ошибка при вызове API Fulfillment: %s", str(e))

    @api.model
    def get_my_profile_action(self):
        profile = self.search([], limit=1)
        if not profile:
            profile = self.create({'name': 'My new fulfillment company'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'fulfillment.profile',
            'view_mode': 'form',
            'res_id': profile.id,
            'views': [(self.env.ref('fulfillment_software.view_fulfillment_profile_form').id, 'form')],
            'target': 'current',
            'flags': {'form': {'action_buttons': True}},
            'context': {'create': False},
        }

    def normalize_datetime_str(dt_str):
        if not dt_str:
            return False
        try:
            # Try ISO8601 with milliseconds and Z
            dt = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S.%fZ')
        except ValueError:
            try:
                # Try ISO8601 without milliseconds
                dt = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ')
            except ValueError:
                # If format unknown, log and return False or None
                _logger.warning(f"Unrecognized datetime format: {dt_str}")
                return False
        return dt.strftime('%Y-%m-%d %H:%M:%S')
 
    
    
class FulfillmentProfileCapabilities(models.Model): 
    _name = 'fulfillment.profile.capabilities'
    _description = 'Fulfillment Profile Capabilities'
    
    version = fields.Char(string="Version capabilities")
    picking_outgoing = fields.Boolean(string="Picking Outgoing")
    picking_returns = fields.Boolean(string="Picking Returns")
    picking_dropshipping = fields.Boolean(string="Picking Dropshipping")
    picking_crossdock = fields.Boolean(string="Picking Crossdock")
    picking_internal = fields.Boolean(string="Picking Internal")
   
    fulfillment_api_key = fields.Char(string="X-Filfillment-API-Key")