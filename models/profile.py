from odoo import models, fields, api, _
import requests
from datetime import datetime
import logging
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError
from urllib.parse import urlparse
_logger = logging.getLogger(__name__)


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
        help="API domain, to backend fulfillment.software",
        default="api.fulfillment.software"
    )
    webhook_domain = fields.Char(
        string="Webhook domain",
        help="Domain of this Odoo instance. Used by the Fulfillment API to send webhook notifications. Detected automatically from web.base.url.",
        default=lambda self: self._default_webhook_domain(),
    )

    

    email = fields.Char(string="Email")
    fulfillment_api_key = fields.Char(
        string="X-Fulfillment-API-Key"
    )
    fulfillment_profile_id = fields.Char(
        string="Fulfillment Application Key",
        readonly=True
    )
    name = fields.Char(string="Company name")
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

    is_available_webhook = fields.Selection([
        ('available', 'Available'),
        ('unavailable', 'Unavailable')],
        default='unavailable', string="Availiable webhook")
    
    
    
    update_at = fields.Datetime(
        string="Last Updated",
        readonly=True
    )
    
    allow_auto_import = fields.Boolean(
        string="Automatic Import",
        help=(
            "Allows external Odoo partner instances to automatically trigger "
            "updates of records in this database without manual approval."
        ),
        default=False,
    )

    
    
    
    @api.model
    def _default_webhook_domain(self):
        """Called once when a new profile record is instantiated."""
        return self._get_domain_from_config() or ''

    def _check_availiable_webhook(self):
        _logger.info(f"[_check_availiable_webhook]")
        
        for record in self:
            status = 'unavailable'

            if record.webhook_domain:
                url = f"https://{record.webhook_domain}/fulfillment/status"
                try:
                    response = requests.get(url, timeout=5)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get('status') == 'ok':
                            status = 'available'
                except Exception:
                    pass

            # Пишем БЕЗ повторного вызова write-логики
            super(FulfillmentProfile, record.with_context(skip_webhook_check=True)).write({
                'is_available_webhook': status
            })

    
    
    def action_set_current_domain(self):
        _logger.info(f"[action_set_current_domain]")
        for record in self:
            domain = self._get_domain_from_request()
            
            if domain and domain not in ['localhost', '127.0.0.1']:
                record.webhook_domain = domain
                return
            
            domain = self._get_domain_from_config()
            if domain:
                record.webhook_domain = domain
    
    def _get_domain_from_request(self):
        _logger.info(f"[_get_domain_from_request]")
        try:
            from odoo.http import request
            if request and hasattr(request, 'httprequest'):
                host = request.httprequest.host
                
                forwarded_host = request.httprequest.headers.get('X-Forwarded-Host')
                if forwarded_host:
                    host = forwarded_host
                
                domain = host.split(':')[0]
                
                if domain and domain not in ['localhost', '127.0.0.1']:
                    return domain
        except (RuntimeError, AttributeError):
            pass
        return False
    
    def _get_domain_from_config(self):
        _logger.info(f"[_get_domain_from_request]")
        config_param = self.env['ir.config_parameter'].sudo()
        
        web_base_url = config_param.get_param('web.base.url', '')
        
        if web_base_url:
            parsed = urlparse(web_base_url)
            if parsed.hostname and parsed.hostname not in ['localhost', '127.0.0.1']:
                return parsed.hostname
        for param_name in ['web.base.url.freeze', 'web.base.url.mycompany', 'website.domain']:
            url = config_param.get_param(param_name, '')
            if url:
                parsed = urlparse(url if '://' in url else f'http://{url}')
                if parsed.hostname and parsed.hostname not in ['localhost', '127.0.0.1']:
                    return parsed.hostname
        
        return False
        
        
    def action_fill_webhook_domain(self):
        _logger.info(f"[action_fill_webhook_domain]")
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for rec in self:
            rec.webhook_domain = base_url
            
            
            
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info(f"[create]")

        # Auto-fill webhook_domain if not provided or still placeholder
        for vals in vals_list:
            domain = vals.get('webhook_domain', '')
            if not domain or domain in ('example.com', 'localhost', '127.0.0.1'):
                # Try HTTP request headers first (most accurate when creating via UI)
                detected = self._get_domain_from_request() or self._get_domain_from_config()
                if detected:
                    vals['webhook_domain'] = detected
                    _logger.info('[Profile] Auto-detected webhook_domain: %s', detected)

        records = super().create(vals_list)

        # Check webhook availability right after creation
        for rec in records:
            if rec.webhook_domain:
                try:
                    rec._check_availiable_webhook()
                except Exception as e:
                    _logger.warning('[Profile] Webhook check failed on create: %s', e)

        if self.env.context.get('skip_auto_import'):
            _logger.debug("[PARTNERS][CREATE] skip_auto_import in context — skipping auto import_all")
            return records

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            _logger.warning("[PARTNERS][CREATE] No active fulfillment.profile with API key — skipping auto import_all")
            return records

        try:
            _logger.info(
                "[PARTNERS][CREATE] Triggering auto import_all() (profile id=%s) ...",
                profile.id
            )

            self.env['fulfillment.partners'].sudo().import_all(profile=profile.sudo())

            _logger.info("[PARTNERS][CREATE] auto import_all() finished")

        except Exception as e:
            _logger.exception("[PARTNERS][CREATE] auto import_all() failed: %s", e)

        return records




    def write(self, vals):
        _logger.info(f"[write]")

        vals['update_at'] = datetime.now()

        # Auto-fill webhook_domain when it is being set to a placeholder or is still empty
        if 'webhook_domain' not in vals:
            for rec in self:
                if not rec.webhook_domain or rec.webhook_domain in ('example.com', 'localhost', '127.0.0.1'):
                    detected = self._get_domain_from_request() or self._get_domain_from_config()
                    if detected:
                        vals['webhook_domain'] = detected
                        _logger.info('[Profile] Auto-set webhook_domain on write: %s', detected)
                    break  # same value for all records in the set

        had_key_before = bool(self.fulfillment_api_key)
        new_key = vals.get("fulfillment_api_key")

        result = super().write(vals)
        
        self._check_availiable_webhook()
        
        if not self.env.context.get('skip_webhook_check'):
            self._check_availiable_webhook()

        self._sync_with_fulfillment_api()

        if new_key and not had_key_before:
            try:
                _logger.info("[PROFILE][WRITE] fulfillment_api_key added → running import_all()")
                self.env['fulfillment.partners'].sudo().import_all(profile=self.sudo())
            except Exception as e:
                _logger.exception("[PROFILE][WRITE] import_all() failed: %s", e)

        return result


    def _resolve_webhook_domain(self):
        """
        Return the best available webhook_domain for this Odoo instance.
        Priority: stored value (if not a placeholder) → web.base.url → request host.
        Always updates the stored field if it was a placeholder.
        """
        _PLACEHOLDERS = {'example.com', 'localhost', '127.0.0.1', '', None}
        domain = self.webhook_domain
        if domain in _PLACEHOLDERS:
            domain = self._get_domain_from_config() or self._get_domain_from_request()
            if domain and domain not in _PLACEHOLDERS:
                _logger.info('[Profile] Auto-resolved webhook_domain: %s', domain)
                super(FulfillmentProfile, self.with_context(skip_webhook_check=True)).write(
                    {'webhook_domain': domain}
                )
        return domain or ''

    def _sync_with_fulfillment_api(self):
        _logger.info(f"[_sync_with_fulfillment_api]")
        bus = self.env['bus.utils']

        for record in self:
            if not record.fulfillment_api_key:
                bus.send_notification(
                    title="API connection error",
                    message="You have not filled in the Fulfillment API Key",
                    level="info",
                    sticky=True
                )
                _logger.warning("API key not set — sync skipped")
                continue

            client = FulfillmentAPIClient(record)

            # Always resolve the real domain — never push a placeholder to the API
            webhook_domain = record._resolve_webhook_domain()

            payload = {
                "name": record.name or "Default Name",
                "api_domain": record.api_domain or "api.fulfillment.software",
                "webhook_domain": webhook_domain,
            }

            try:
                if record.fulfillment_profile_id:
                    response = client.fulfillment.update(
                        record.fulfillment_profile_id,
                        payload
                    )
                    data = response.get("data")
                    if data:
                        _logger.info(
                            "Fulfillment %s обновлён через PATCH",
                            record.fulfillment_profile_id
                        )
                    else:
                        _logger.warning("PATCH — неожиданный ответ: %s", response)

                else:
                    response = client.fulfillment.create(payload)
                    data = response.get("data")

                    if data and data.get("id"):
                        record.write({
                            "fulfillment_profile_id": data["id"],
                            "name": data.get("name", record.name),
                            "api_domain": data.get("api_domain", record.api_domain),
                            "webhook_domain": data.get(
                                "webhook_domain",
                                record.webhook_domain
                            ),
                        })
                        _logger.info(
                            "Fulfillment создан через POST с ID %s",
                            data["id"]
                        )
                    else:
                        _logger.warning("POST — неожиданный ответ: %s", response)

            except FulfillmentAPIError as e:
                _logger.error("Ошибка API Fulfillment: %s", str(e))
            except Exception:
                _logger.exception("Неожиданная ошибка при sync")


    @api.model
    def get_my_profile_action(self):
        _logger.info(f"[get_my_profile_action]")
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

    def action_sync_images_from_api(self):
        """
        For every product that has a fulfillment_product_id but no local image,
        fetch its img_url from the Fulfillment API and download the image.
        Run this on Odoo B to populate missing product images.
        """
        self.ensure_one()
        api = FulfillmentAPIClient(self)
        Picking = self.env['stock.picking'].sudo()

        products = self.env['product.template'].search([
            ('fulfillment_product_id', '!=', False),
            ('image_1920', '=', False),
        ])
        updated = 0
        for rec in products:
            try:
                resolved_url = Picking._resolve_img_url(None, rec.fulfillment_product_id)
                if not resolved_url:
                    continue
                image_b64 = Picking._fetch_image_b64(resolved_url)
                if image_b64:
                    rec.with_context(skip_fulfillment_push=True).write(
                        {'image_1920': image_b64}
                    )
                    updated += 1
                    _logger.info(
                        '[Fulfillment] Image set for product "%s" from %s',
                        rec.name, resolved_url,
                    )
            except Exception as e:
                _logger.warning(
                    '[Fulfillment] Failed to sync image for "%s": %s', rec.name, e
                )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Image Sync Complete',
                'message': f'Updated images for {updated} product(s).',
                'type': 'success',
                'sticky': False,
            },
        }

    def action_resync_images_to_api(self):
        """
        Push absolute img_url for every linked product to the Fulfillment API.
        Run this on Odoo A once to fix legacy relative-URL entries so that
        Odoo B can download images when importing transfers.
        """
        self.ensure_one()
        api = FulfillmentAPIClient(self)
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')

        products = self.env['product.template'].search([
            ('fulfillment_product_id', '!=', False),
            ('image_1920', '!=', False),
        ])
        updated = 0
        for rec in products:
            img_url = f"{base_url}/web/image/product.template/{rec.id}/image_1920"
            try:
                api.product.update(rec.fulfillment_product_id, {"img_url": img_url})
                updated += 1
                _logger.info(
                    "[Fulfillment] Resynced img_url for product '%s' (id=%s)",
                    rec.name, rec.fulfillment_product_id,
                )
            except Exception as e:
                _logger.warning(
                    "[Fulfillment] Failed to resync img_url for '%s': %s",
                    rec.name, e,
                )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Image Resync Complete',
                'message': f'Updated {updated} product image URL(s) in the Fulfillment API.',
                'type': 'success',
                'sticky': False,
            },
        }

    @staticmethod
    def normalize_datetime_str(dt_str):
        _logger.info(f"[normalize_datetime_str]")
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
