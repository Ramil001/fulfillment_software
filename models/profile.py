from odoo import models, fields, api
from datetime import datetime
import logging
from .helpers import get_default_domain_host
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


## Revision
class FulfillmentProfile(models.Model):
    _name = 'fulfillment.profile'
    _description = 'Fulfillment Profile'

    address = fields.Char(string="Address")
    capabilities_id = fields.Many2one(
        'fulfillment.profile.capabilities',
        string="Capabilities",
        ondelete='cascade'
    )
    country_id = fields.Many2one(
        'res.country',
        string="Country",
        default=lambda self: self.env.ref('base.de').id
    )
    api_domain = fields.Char(
        string="API domain",
        default="api.fulfillment.software"
    )
    webhook_url = fields.Char(
        string="Webhook URL",
        help="This field use for get odoo domain instance. Use for webhook.",
        readonly=True,
        default=lambda self: get_default_domain_host(self.env)
    )
    email = fields.Char(string="Email")
    fulfillment_api_key = fields.Char(
        string="X-Fulfillment-API-Key"
    )
    fulfillment_profile_id = fields.Char(
        string="Fulfillment Application Key",
        readonly=True
    )
    name = fields.Char(string="Fulfillment name")
    phone = fields.Char(string="Phone number")
    state_id = fields.Many2one(
        'res.country.state',
        string="City/Region",
        domain="[('country_id', '=', country_id)]"
    )
    verification_account = fields.Selection([
        ('verification', 'Verification'),
        ('not_verification', 'Not verification')],
        default='not_verification')

    update_at = fields.Datetime(
        string="Last Updated",
        readonly=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals['update_at'] = datetime.now()

        records = super().create(vals_list)
        records._sync_with_fulfillment_api()
        return records

    def write(self, vals):
        vals['update_at'] = datetime.now()
        result = super().write(vals)
        self._sync_with_fulfillment_api()
        return result

    # --- Sync через API client ---
    def _sync_with_fulfillment_api(self):
        for record in self:
            if not record.fulfillment_api_key:
                _logger.warning("API ключ не задан — sync пропущен.")
                continue

            client = FulfillmentAPIClient(record)

            payload = {
                "name": record.name or "Default Name",
                "api_domain": record.api_domain or "api.fulfillment.software",
                "webhook_url": record.webhook_url,
            }

            try:
                if record.fulfillment_profile_id:
                    # обновляем существующий профиль
                    response = client.fulfillment.update(record.fulfillment_profile_id, payload)
                    if response.get("status") == "success":
                        _logger.info("Fulfillment %s обновлён через PATCH", record.fulfillment_profile_id)
                    else:
                        _logger.warning("PATCH — неожиданный ответ: %s", response)
                else:
                    # создаём новый профиль
                    response = client.fulfillment.create(payload)
                    if response.get("status") == "success" and "data" in response:
                        data = response["data"]
                        record.write({
                            "fulfillment_profile_id": data.get("fulfillment_id"),
                            "name": data.get("name", record.name),
                            "api_domain": data.get("api_domain", record.api_domain)
                        })
                        _logger.info("Fulfillment создан через POST с ID %s", data.get("fulfillment_id"))
                    else:
                        _logger.warning("POST — неожиданный ответ: %s", response)

            except FulfillmentAPIError as e:
                _logger.error("Ошибка API Fulfillment: %s", str(e))
            except Exception as e:
                _logger.error("Неожиданная ошибка при sync: %s", str(e))

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

    @staticmethod
    def normalize_datetime_str(dt_str):
        if not dt_str:
            return False
        try:
            return datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S.%fZ').strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            try:
                return datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ').strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                _logger.warning(f"Unrecognized datetime format: {dt_str}")
                return False





class FulfillmentProfileCapabilities(models.Model): 
    _name = 'fulfillment.profile.capabilities'
    _description = 'Fulfillment Profile Capabilities'
    
    version = fields.Char(string="Version capabilities")
    picking_outgoing = fields.Boolean(string="Picking Outgoing")
    picking_returns = fields.Boolean(string="Picking Returns")
    picking_dropshipping = fields.Boolean(string="Picking Dropshipping")
    picking_crossdock = fields.Boolean(string="Picking Crossdock")
    picking_internal = fields.Boolean(string="Picking Internal")

    fulfillment_api_key = fields.Char(string="X-Fulfillment-API-Key")
