from odoo import models, fields, api
import logging
from datetime import datetime
from odoo.exceptions import UserError
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    x_fulfillment_id = fields.Char(string="Fulfillment External ID", index=True, copy=False)

class FulfillmentPartners(models.Model):
    _name = 'fulfillment.partners'
    _description = 'Fulfillment Partners'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    status = fields.Selection([
        ('follow', 'Follow'),
        ('unfollow', 'Unfollow')],
        default='unfollow', tracking=True)

    profile_id = fields.Many2one('fulfillment.profile', string="Profile")
    name = fields.Char(string="Fulfillment company", required=True, readonly=True)
    fulfillment_id = fields.Char(string="Fulfillment ID", required=True, index=True, readonly=True)
    api_domain = fields.Char(string="Domain API", readonly=True)
    webhook_url = fields.Char(string="Webhook URL")
    created_at = fields.Datetime(string="Date created")
    user_id = fields.Char(string="User external ID")
    fulfillment_api_key = fields.Char(string="X-Fulfillment-API-Key")
    
    warehouses_owner_ids = fields.One2many('stock.warehouse', 'fulfillment_owner_id')
    warehouses_client_ids = fields.One2many('stock.warehouse', 'fulfillment_client_id')

    
    
    transfers_purchase_ids = fields.One2many(
        'stock.picking',
        compute='_compute_transfers',
        string="Purchase Receipts"
    )
    transfers_internal_ids = fields.One2many(
        'stock.picking',
        compute='_compute_transfers',
        string="Internal Transfers"
    )
    transfers_delivery_ids = fields.One2many(
        'stock.picking',
        compute='_compute_transfers',
        string="Delivery Orders"
    )
    # Ссылка на ID контакта odoo привязанного к fulfillment профилю 
    partner_id = fields.Many2one(
        'res.partner',
        string="Linked contact",
        help="Odoo contact lined to this fulfillment partner",
        readonly=True
    )
    


    def _compute_transfers(self):
        for partner in self:
            warehouses_owner = partner.warehouses_owner_ids
            warehouses_client = partner.warehouses_client_ids
            warehouses_all = warehouses_owner | warehouses_client

            if not warehouses_all:
                partner.transfers_purchase_ids = False
                partner.transfers_internal_ids = False
                partner.transfers_delivery_ids = False
                continue

            Pick = self.env['stock.picking']

            # Приходы (партнёр — клиент)
            partner.transfers_purchase_ids = Pick.search([
                ('picking_type_id.code', '=', 'incoming'),
                ('picking_type_id.warehouse_id', 'in', warehouses_client.ids)
            ])

            # Отгрузки (партнёр — владелец)
            partner.transfers_delivery_ids = Pick.search([
                ('picking_type_id.code', '=', 'outgoing'),
                ('picking_type_id.warehouse_id', 'in', warehouses_owner.ids)
            ])

            # Внутренние (любой склад, связанный с партнёром)
            partner.transfers_internal_ids = Pick.search([
                ('picking_type_id.code', '=', 'internal'),
                ('picking_type_id.warehouse_id', 'in', warehouses_all.ids)
            ])


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

    def _create_or_update_contact(self, partner_record):
        """Создаём или обновляем контакт res.partner с тегом Fulfillment и возвращаем его"""
        tag = self._get_fulfillment_tag()

        contact = self.env['res.partner'].search([
            ('x_fulfillment_id', '=', partner_record.fulfillment_id)
        ], limit=1)

        contact_vals = {
            'name': partner_record.name,
            'comment': f"Synced from Fulfillment {partner_record.api_domain or ''}",
            'category_id': [(4, tag.id)],  # добавить тег
            'x_fulfillment_id': partner_record.fulfillment_id,
        }

        if contact:
            contact.write(contact_vals)
        else:
            contact = self.env['res.partner'].create(contact_vals)

        return contact


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
            partner_record = existing
        else:
            partner_record = self.create(vals)

        # --- Создание/обновление res.partner ---
        contact = self._create_or_update_contact(partner_record)

        # --- Заполняем ссылку на контакт ---
        if contact and partner_record.partner_id != contact:
            partner_record.partner_id = contact.id




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
    

    def _get_fulfillment_tag(self):
        """Создаём или ищем тег 'Fulfillment'"""
        tag = self.env['res.partner.category'].search([('name', '=', 'Fulfillment')], limit=1)
        if not tag:
            tag = self.env['res.partner.category'].create({'name': 'Fulfillment'})
        return tag
