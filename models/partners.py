from odoo import models, fields, api
import logging
from datetime import datetime
from odoo.exceptions import UserError
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


# Тут мы переопределяем модель парнеров контактов и добавляем нужные поля.
# ---------- Контакты  -----------#
class FulfillmentOverrideResPartner(models.Model):
    _inherit = 'res.partner'

    fulfillment_warehouse_id = fields.Char(
        string="Fulfillment warehouse Id", index=True, copy=False, readonly=True
    )
    linked_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string="Linked warehouse",
        help="Warehouse that this contact represents",
        ondelete="set null",
        copy=False,
        readonly=True
    )
    
    fulfillment_partner_id = fields.Char(
        string="Fulfillment partner Id",
        index=True,
        copy=False,
        readonly=True
    )


# ---------- Партнеры  -----------#
class FulfillmentPartners(models.Model):
    _name = 'fulfillment.partners'
    _description = 'Fulfillment Partners'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # Переменная подписки на fulfillment показывает статус, подписан / отписан
    status = fields.Selection([
        ('follow', 'Follow'),
        ('unfollow', 'Unfollow')],
        default='unfollow', tracking=True, required=True)


    # Название компании партнера
    name = fields.Char(string="Company name", required=True, readonly=True)
    fulfillment_id = fields.Char(string="Fulfillment ID", required=True, index=True, readonly=True)
    fulfillment_logo = fields.Binary(string="Logo", attachment=True, help="Upload a logo or photo for this fulfillment partner.")
    
    api_domain = fields.Char(string="API", readonly=True)
    webhook_url = fields.Char(string="Webhook", readonly=True)
    
    created_at = fields.Datetime(string="Date created")
    
    fulfillment_api_key = fields.Char(string="X-Fulfillment-API-Key")
    
    # эти переменные пока не работают..
    warehouses_owner_ids = fields.One2many('stock.warehouse', 'fulfillment_owner_id')
    warehouses_client_ids = fields.One2many('stock.warehouse', 'fulfillment_client_id')
    
    transfers_purchase_ids = fields.One2many('stock.picking','fulfillment_partner_id',string="Purchase Receipts")
    transfers_internal_ids = fields.One2many('stock.picking','fulfillment_partner_id',string="Internal Transfers")
    transfers_delivery_ids = fields.One2many('stock.picking','fulfillment_partner_id',string="Delivery Orders")
    #=========
    
    # Ссылка на ID контакта odoo привязанного к fulfillment профилю 
    partner_id = fields.Many2one(
        'res.partner',
        string="Contact",
        help="Odoo contact linked to this fulfillment partner"
    )
    
    def action_follow(self):
        self.write({'status': 'follow'})

    def action_unfollow(self):
        self.write({'status': 'unfollow'})
        
    # Разрешаем использование параметра password в поле
    def _valid_field_parameter(self, field, name):
        return name == 'password' or super()._valid_field_parameter(field, name)

    # --- Основная синхронизация ---
    @api.model
    def import_all(self, profile=None):
        """Sync data from API and return proper action"""
        try:
            if not profile:
                return self._notification("Error", "No active profile with API key", "danger", sticky=True)

            data = self._fetch_api_data(profile)
            if not data:
                return False

            self._process_api_data(data, profile)

            for partner in self.search([]):
                _logger.info(
                    f"[IMPORT DONE][{partner.name}] "
                    f"Purchases={partner.transfers_purchase_ids.ids}, "
                    f"Internal={partner.transfers_internal_ids.ids}, "
                    f"Delivery={partner.transfers_delivery_ids.ids}"
                )
                self.env['stock.warehouse'].sudo().with_context(skip_api_sync=True).import_warehouses(partner)

            # загружаем трансферы
            for item in data:
                fulfillment_id = item.get("fulfillment_id")
                if fulfillment_id:
                    page = 1
                    limit = 100
                    while True:
                        _logger.info(f"🔄 Начинаем загрузку трансферов для {fulfillment_id}, page={page}, limit={limit}")
                        success = self.env['stock.picking'].sudo().with_context(skip_fulfillment_push=True).import_transfers(
                            fulfillment_id=fulfillment_id,
                            page=page,
                            limit=limit
                        )

                        _logger.info(f"✅ Результат import_transfers для {fulfillment_id}, page={page}: {success}")
                        if not success or success < limit:
                            break
                        page += 1


            fulfillment_warehouses = self.env['stock.warehouse'].search([
                        ('fulfillment_warehouse_id', '!=', False)
                    ])

            if not fulfillment_warehouses:
                _logger.warning("[FULFILLMENT][IMPORT_ALL] Нет складов с fulfillment_warehouse_id — импорт остатков пропущен")
            else:
                _logger.info(
                    "[FULFILLMENT][IMPORT_ALL] Импорт остатков для складов: %s",
                    ', '.join(fulfillment_warehouses.mapped('name'))
                )

                stock_model = self.env['stock.quant'].sudo()
                for warehouse in fulfillment_warehouses:
                    filters = {
                        "warehouse_ids": [warehouse.fulfillment_warehouse_id],
                    }
                    _logger.info(
                        "[FULFILLMENT][IMPORT_ALL] 🔄 Импорт остатков для склада %s (%s)",
                        warehouse.name, warehouse.fulfillment_warehouse_id
                    )
                    success = stock_model.import_stock(filters=filters)
                    _logger.info(
                        "[FULFILLMENT][IMPORT_ALL] ✅ Импорт остатков завершён для %s: %s",
                        # warehouse.name, success
                    )
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


    

    def button_run_import_all(self):
        """Кнопка в интерфейсе"""
        profile = self._get_active_profile()
        success = self.import_all(profile=profile)

        if not success:
            return self._notification("Ошибка", "Синхронизация не удалась", "danger", sticky=True)

        return self._notification("Синхронизация", "Обновление включилось!", "success", sticky=False,
                                  extra={'next': {'type': 'ir.actions.client', 'tag': 'reload'}})
        
        
    

    # --- Вспомогательные методы ---
    def import_contacts(self, partner_record):
        """Создаём или обновляем контакт res.partner с тегом Fulfillment и возвращаем его"""
        tag = self._get_fulfillment_tag()
        self._notification("Ошибка", "Импорт контаков", "success", sticky=True)
        contact = self.env['res.partner'].search([
            ('fulfillment_partner_id', '=', partner_record.fulfillment_id)
        ], limit=1)
        
        contact_vals = {
            'name': partner_record.name,
            'comment': f"Synced from Fulfillment {partner_record.api_domain or ''}",
            'category_id': [(4, tag.id)],
            'fulfillment_partner_id': partner_record.fulfillment_id,
        }

        contact = self.env['res.partner'].search([
            ('fulfillment_partner_id', '=', partner_record.fulfillment_id)
        ], limit=1)
        
        _logger.info(f"🔄 Creating/updating res.partner for {partner_record.name} ({partner_record.fulfillment_id}) with {contact_vals}")

        if contact:
            contact.write(contact_vals)
        else:
            contact = self.env['res.partner'].create(contact_vals)
            
        partner_record.partner_id = contact.id
        self.env.cr.commit() 

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
            _logger.info("📦[JSON][FULFILLMENT LIST][API]: %s", data)
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
        _logger.info(f"[🌙][PROCESS_API]: DATA: {data} PROFILE: {profile}")
        for item in data:
            self.import_partners(item, profile)

    def import_partners(self, item, profile):
        """Создание или обновление партнера"""
        existing = self.search([('fulfillment_id', '=', item['fulfillment_id'])], limit=1)
        created_at = self._normalize_datetime(item.get('created_at'))

        vals = {
            'name': item.get('name') or 'Без имени',
            'fulfillment_id': item.get('fulfillment_id'),
            'api_domain': item.get('api_domain'),
            'webhook_url': item.get('webhook_url'),
            'created_at': created_at,
            'fulfillment_api_key': profile.fulfillment_api_key,
        }
        
        _logger.info(f"🔄 Creating/updating fulfillment.partner: {vals}")
        
        if existing:
            existing.write(vals)
            partner_record = existing
        else:
            partner_record = self.create(vals)

        # --- Создание/обновление res.partner ---
        contact = self.import_contacts(partner_record)

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
