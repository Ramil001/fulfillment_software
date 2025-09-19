from odoo import models, fields, api
import logging
from datetime import datetime
from odoo.exceptions import UserError
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


class FulfillmentPartners(models.Model):
    _name = 'fulfillment.partners'
    _description = 'Fulfillment Partners'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    status = fields.Selection([
        ('follow', 'Follow'),
        ('unfollow', 'Unfollow')],
        default='unfollow', tracking=True)

    name = fields.Char(string="Fulfillment company name", required=True, readonly=True)
    fulfillment_id = fields.Char(string="Fulfillment external ID", required=True, index=True, readonly=True)
    api_domain = fields.Char(string="API domain", readonly=True)
    webhook_url = fields.Char(string="Webhook URL")
    created_at = fields.Datetime(string="Date created")
    user_id = fields.Char(string="User external ID")
    profile_id = fields.Many2one('fulfillment.profile', string="Profile")
    fulfillment_api_key = fields.Char(string="X-Fulfillment-API-Key")

    # Разрешаем использование параметра password в поле
    def _valid_field_parameter(self, field, name):
        return name == 'password' or super()._valid_field_parameter(field, name)

    # --- Основная синхронизация ---
    @api.model
    def sync_from_api(self, profile=None):
        """Sync data from API and return proper action"""
        try:
            if not profile:
                return self._notification("Error", "No active profile with API key", "danger", sticky=True)

            data = self._fetch_api_data(profile)
            if not data:
                return False

            self._process_api_data(data, profile)

            # обновляем склады и приходы
            self.env['stock.picking'].sudo().create_fulfillment_receipt()
            self.env['stock.warehouse'].sudo().reload_warehouses()

            # загружаем трансферы
            for item in data:
                fulfillment_id = item.get("fulfillment_id")
                if fulfillment_id:
                    page = 1
                    limit = 100
                    while True:
                        success = self.env['stock.picking'].sudo().load_transfers(
                            fulfillment_id=fulfillment_id,
                            page=page,
                            limit=limit
                        )
                        if not success or success < limit:
                            break
                        page += 1

            return {
                'type': 'ir.actions.act_window',
                'name': 'Partners',
                'res_model': 'fulfillment.partners',
                'view_mode': 'tree,form',
                'views': [
                    (self.env.ref('fulfillment_software.view_fulfillment_partners_list').id, 'tree'),
                    (False, 'form')
                ],
                'target': 'current'
            }

        except Exception as e:
            _logger.error("Sync failed: %s", str(e))
            return self._notification("Error", f"Sync failed: {str(e)}", "danger", sticky=True)

    def button_sync_from_api(self):
        """Кнопка в интерфейсе"""
        profile = self._get_active_profile()
        success = self.sync_from_api(profile=profile)

        _logger.info(f"[FULFILLMENT][button_sync_from_api]: {success}")

        if not success:
            return self._notification("Ошибка", "Синхронизация не удалась", "danger", sticky=True)

        return self._notification("Синхронизация", "Обновление включилось!", "success", sticky=False,
                                  extra={'next': {'type': 'ir.actions.client', 'tag': 'reload'}})

    # --- Вспомогательные методы ---

    def _get_active_profile(self):
        """Получаем активный профиль с API ключом"""
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            _logger.error("❌ No active profile with API key")
            raise UserError("No active profile with API key configured")
        return profile

    def _fetch_api_data(self, profile):
        """Получаем список фулфилментов через API client"""
        try:
            client = FulfillmentAPIClient(profile)
            data = client.fulfillment.list()
            data = data.get("data", [])
            _logger.info("Received %s partners from API", len(data))
            return data
        except FulfillmentAPIError as e:
            _logger.error(f"❌ API error: {e}")
            raise UserError(str(e))
        except Exception as e:
            _logger.error(f"❌ Unexpected API error: {e}")
            raise UserError(f"API request failed: {str(e)}")

    def _process_api_data(self, data, profile):
        """Обработка полученных данных и обновление партнеров"""
        for item in data:
            self._create_or_update_partner(item, profile)

    def _create_or_update_partner(self, item, profile):
        """Создание или обновление партнера"""
        existing = self.search([('fulfillment_id', '=', item['fulfillment_id'])], limit=1)
        created_at = self._normalize_datetime(item.get('created_at'))

        vals = {
            'name': item.get('name') or 'Без имени',
            'fulfillment_id': item.get('fulfillment_id'),
            'api_domain': item.get('api_domain'),
            'webhook_url': item.get('webhook_url'),
            'created_at': created_at,
            'user_id': item.get('user_id'),
            'fulfillment_api_key': profile.fulfillment_api_key,
            'profile_id': profile.id,
        }

        if existing:
            existing.write(vals)
        else:
            self.create(vals)

    def _normalize_datetime(self, dt_str):
        """Нормализация формата даты"""
        if not dt_str:
            return False
        try:
            return datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%S.%fZ')
        except ValueError:
            try:
                return datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ')
            except Exception:
                _logger.warning(f"Unknown time format: {dt_str}")
                return False

    def _notification(self, title, message, type_, sticky=False, extra=None):
        """Унифицированное уведомление"""
        notif = {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': type_,
                'sticky': sticky,
            }
        }
        if extra:
            notif['params'].update(extra)
        return notif
    
