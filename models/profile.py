from odoo import models, fields, api, _
import requests
from datetime import datetime
import logging
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


class FulfillmentProfile(models.Model):
    _name = 'fulfillment.profile'
    _description = 'Fulfillment Profile'
    

    address = fields.Char(string=_("Address"))
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
    webhook_domain = fields.Char(string="Webhook domain", help="A webhook is the domain of the site where your Odoo runs. It is necessary to call the update function when your Odoo needs to update resources.", default="example.com")

    

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

    
    
    
    def _check_availiable_webhook(self):
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

            # ❗ Пишем БЕЗ повторного вызова write-логики
            super(FulfillmentProfile, record.with_context(skip_webhook_check=True)).write({
                'is_available_webhook': status
            })

    
    
    def action_set_current_domain(self):
        """Установить текущий домен в поле webhook_domain"""
        for record in self:
            # Пробуем получить домен из текущего запроса
            domain = self._get_domain_from_request()
            
            if domain and domain not in ['localhost', '127.0.0.1']:
                record.webhook_domain = domain
                return
            
            # Если не получилось из request, пробуем из параметров
            domain = self._get_domain_from_config()
            if domain:
                record.webhook_domain = domain
    
    def _get_domain_from_request(self):
        """Получить домен из текущего HTTP запроса"""
        try:
            from odoo.http import request
            if request and hasattr(request, 'httprequest'):
                # Получаем хост из заголовков
                host = request.httprequest.host
                
                # Проверяем заголовки, которые могут содержать реальный домен
                forwarded_host = request.httprequest.headers.get('X-Forwarded-Host')
                if forwarded_host:
                    host = forwarded_host
                
                # Убираем порт если есть
                domain = host.split(':')[0]
                
                # Проверяем что это не localhost
                if domain and domain not in ['localhost', '127.0.0.1']:
                    return domain
        except (RuntimeError, AttributeError):
            pass
        return False
    
    def _get_domain_from_config(self):
        """Получить домен из конфигурации"""
        config_param = self.env['ir.config_parameter'].sudo()
        
        # Пробуем разные параметры
        web_base_url = config_param.get_param('web.base.url', '')
        
        if web_base_url:
            parsed = urlparse(web_base_url)
            if parsed.hostname and parsed.hostname not in ['localhost', '127.0.0.1']:
                return parsed.hostname
        
        # Пробуем другие возможные параметры
        for param_name in ['web.base.url.freeze', 'web.base.url.mycompany', 'website.domain']:
            url = config_param.get_param(param_name, '')
            if url:
                parsed = urlparse(url if '://' in url else f'http://{url}')
                if parsed.hostname and parsed.hostname not in ['localhost', '127.0.0.1']:
                    return parsed.hostname
        
        return False
        
        
    def action_fill_webhook_domain(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for rec in self:
            rec.webhook_domain = base_url
            
            
            
    @api.model_create_multi
    def create(self, vals_list):
        # Создаём записи как обычно
        records = super().create(vals_list)
        _logger.info("[PARTNERS][CREATE] Created partners: %s", records.ids)

        # Если вызов уже помечен skip_auto_import — ничего не делаем.
        if self.env.context.get('skip_auto_import'):
            _logger.debug("[PARTNERS][CREATE] skip_auto_import in context — skipping auto import_all")
            return records

        # Ищем активный профиль с API ключом
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            _logger.warning("[PARTNERS][CREATE] No active fulfillment.profile with API key — skipping auto import_all")
            return records

        try:
            _logger.info(
                "[PARTNERS][CREATE] Triggering auto import_all() (profile id=%s) ...",
                profile.id
            )

            # вызываем метод модели partners с sudo()
            self.env['fulfillment.partners'].sudo().import_all(profile=profile.sudo())

            _logger.info("[PARTNERS][CREATE] auto import_all() finished")

        except Exception as e:
            _logger.exception("[PARTNERS][CREATE] auto import_all() failed: %s", e)

        return records




    def write(self, vals):
        vals['update_at'] = datetime.now()

        # запоминаем старое значение
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


    # --- Sync через API client ---
    def _sync_with_fulfillment_api(self):
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

            payload = {
                "name": record.name or "Default Name",
                "api_domain": record.api_domain or "api.fulfillment.software",
                "webhook_domain": record.webhook_domain,
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
