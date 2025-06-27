from odoo import models, fields, api
import requests
import logging
from datetime import datetime
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class FulfillmentPartners(models.Model):
   _name = 'fulfillment.partners'
   _description = 'Fulfillment Partners'
   _inherit = ['mail.thread', 'mail.activity.mixin']
   
   status = fields.Selection([('follow', 'Follow'),
                              ('unfollow', 'Unfollow')],
                             default='unfollow', tracking=True)
   
   name = fields.Char(string="Fulfillment Name", required=True)
   fulfillment_id = fields.Char(string="Fulfillment ID", required=True, index=True)
   domain = fields.Char(string="Domain")
   webhook_url = fields.Char(string="Webhook URL")
   created_at = fields.Datetime(string="Created At")
   user_id = fields.Char(string="User ID")
   fulfillment_api_key = fields.Char(string="X-Fulfillment-API-Key")


   def action_send_request_follow(self):
        # тут любая логика обработки
        # ...

        # показать сообщение
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Успех',
                'message': 'Запрос отправлен',
                'type': 'success',  # также можно 'danger', 'warning', 'info'
                'sticky': False,    # True = сообщение не исчезает
            }
        }

   # Разрешаем использование параметра password в поле
   def _valid_field_parameter(self, field, name):
      return name == 'password' or super()._valid_field_parameter(field, name)

   @api.model
   def sync_from_api(self):
      """Sync data from API and return proper action"""
      try:
         profile = self._get_active_profile()
         if not profile:
               return {
                  'type': 'ir.actions.client',
                  'tag': 'display_notification',
                  'params': {
                     'title': 'Error',
                     'message': 'No active profile with API key',
                     'type': 'danger',
                     'sticky': True
                  }
               }

         data = self._fetch_api_data(profile.fulfillment_api_key)
         if not data:
               return False  # Error already logged and notified

         self._process_api_data(data, profile.fulfillment_api_key)
         
         # Return action to show the partners list
         return {
               'type': 'ir.actions.act_window',
               'name': 'Partners',
               'res_model': 'fulfillment.partners',
               'view_mode': 'tree,form',
               'views': [
                  (self.env.ref('fulfillment_software.view_fulfillment_partners_tree').id, 'tree'),
                  (False, 'form')
               ],
               'target': 'current'
         }

      except Exception as e:
         _logger.error("Sync failed: %s", str(e))
         return {
               'type': 'ir.actions.client',
               'tag': 'display_notification',
               'params': {
                  'title': 'Error',
                  'message': f'Sync failed: {str(e)}',
                  'type': 'danger',
                  'sticky': True
               }
         }
         
   def button_sync_from_api(self):
      success = self.sync_from_api()
      if not success:
         # При желании здесь можно показать уведомление об ошибке
         return {
               'type': 'ir.actions.client',
               'tag': 'display_notification',
               'params': {
                  'title': 'Ошибка',
                  'message': 'Синхронизация не удалась',
                  'type': 'danger',
                  'sticky': True,
               }
         }
      # После успешной синхронизации просто перезагружаем текущий список
      return {
         'type': 'ir.actions.client',
         'tag': 'reload',
   }

   
   def _get_active_profile(self):
      """Получаем активный профиль с API ключом"""
      profile = self.env['fulfillment.profile'].search([], limit=1)
      if not profile or not profile.fulfillment_api_key:
         _logger.error("❌ No active profile with API key")
         raise UserError("No active profile with API key configured")
      return profile

   def _fetch_api_data(self, api_key):
      """Получаем данные из API"""
      url = "https://api.fulfillment.software/api/v1/fulfillments/"
      headers = {
         "Content-Type": "application/json",
         "X-Fulfillment-API-Key": api_key
      }

      try:
         response = requests.get(url, headers=headers, timeout=10)
         response.raise_for_status()
         data = response.json().get("data", [])
         _logger.info("✅ Received %s partners from API", len(data))
         return data

      except requests.exceptions.HTTPError as e:
         error_msg = self._handle_http_error(e)
         _logger.error(error_msg)
         raise UserError(error_msg)
      except Exception as e:
         _logger.error("❌ API request error: %s", str(e))
         raise UserError(f"API request failed: {str(e)}")

   def _handle_http_error(self, error):
      """Обработка HTTP ошибок"""
      if error.response.status_code == 429:
         return "⚠️ API rate limit exceeded. Please try again later."
      elif error.response.status_code == 401:
         return "❌ Authentication failed. Check your API key."
      else:
         return f"❌ HTTP Error {error.response.status_code}: {str(error)}"

   def _process_api_data(self, data, api_key):
      """Обработка полученных данных"""
      for item in data:
         self._create_or_update_partner(item, api_key)

   def _create_or_update_partner(self, item, api_key):
      """Создание или обновление партнера"""
      existing = self.search([('fulfillment_id', '=', item['fulfillmentId'])], limit=1)
      created_at = self._normalize_datetime(item.get('createdAt'))

      vals = {
         'name': item.get('name'),
         'domain': item.get('domain'),
         'webhook_url': item.get('webHookUrl'),
         'created_at': created_at,
         'user_id': item.get('userId'),
         'fulfillment_api_key': api_key,
      }

      if existing:
         existing.write(vals)
      else:
         vals['fulfillment_id'] = item['fulfillmentId']
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

  