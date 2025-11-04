from odoo import models, fields, api
import logging
from datetime import datetime
from odoo.exceptions import UserError
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


# ---------- Контакты ----------
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


# ---------- Партнеры ----------
class FulfillmentPartners(models.Model):
    _name = 'fulfillment.partners'
    _description = 'Fulfillment Partners'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    status = fields.Selection([
        ('follow', 'Follow'),
        ('unfollow', 'Unfollow')],
        default='unfollow', tracking=True, required=True)

    name = fields.Char(string="Company name", required=True, readonly=True)
    fulfillment_id = fields.Char(string="Fulfillment ID", required=True, index=True, readonly=True)
    fulfillment_logo = fields.Binary(string="Logo", attachment=True)
    api_domain = fields.Char(string="API", readonly=True)
    webhook_url = fields.Char(string="Webhook", readonly=True)
    created_at = fields.Datetime(string="Date created")
    fulfillment_api_key = fields.Char(string="X-Fulfillment-API-Key")

    warehouses_owner_ids = fields.One2many('stock.warehouse', 'fulfillment_owner_id')
    warehouses_client_ids = fields.One2many('stock.warehouse', 'fulfillment_client_id')

    transfers_purchase_ids = fields.One2many('stock.picking', 'fulfillment_partner_id', string="Purchase Receipts")
    transfers_internal_ids = fields.One2many('stock.picking', 'fulfillment_partner_id', string="Internal Transfers")
    transfers_delivery_ids = fields.One2many('stock.picking', 'fulfillment_partner_id', string="Delivery Orders")

    partner_id = fields.Many2one(
        'res.partner',
        string="Contact",
        help="Odoo contact linked to this fulfillment partner"
    )

    # ---------- Вспомогательный helper ----------
    def _notify_bus(self, title, message, level="info", sticky=False):
        """Упрощённый вызов уведомлений через bus.utils"""
        try:
            bus = self.env['bus.utils']
            bus.send_notification(
                title=title,
                message=message,
                level=level,
                sticky=sticky
            )
        except Exception as e:
            _logger.warning(f"[BUS_NOTIFY_FAIL] {e}")

    # ---------- Действия ----------
    def action_follow(self):
        self.write({'status': 'follow'})

    def action_unfollow(self):
        self.write({'status': 'unfollow'})

    def _valid_field_parameter(self, field, name):
        return name == 'password' or super()._valid_field_parameter(field, name)

    # ---------- Основная синхронизация ----------
    @api.model
    def import_all(self, profile=None):
        """Полный импорт партнёров и связанных данных"""
        bus = self.env['bus.utils']
        try:
            bus.send_notification(
                title="Fulfillment Sync",
                message="Начало полной синхронизации с Fulfillment API",
                level="info",
                sticky=True
            )

            if not profile:
                bus.send_notification(
                    title="Fulfillment Sync",
                    message="Не найден активный профиль с API ключом",
                    level="danger",
                    sticky=True
                )
                return False

            bus.send_notification(
                title="Fulfillment Sync",
                message="Получение данных из Fulfillment API...",
                level="info"
            )

            data = self._fetch_api_data(profile)
            if not data:
                bus.send_notification(
                    title="Fulfillment Sync",
                    message="Данные из API не были получены",
                    level="warning",
                    sticky=True
                )
                return False

            bus.send_notification(
                title="Fulfillment Sync",
                message="Обработка полученных данных...",
                level="info"
            )
            self._process_api_data(data, profile)

            bus.send_notification(
                title="Fulfillment Sync",
                message="Импорт данных партнёров и связанных складов...",
                level="info"
            )

            for partner in self.search([]):
                _logger.info(f"[IMPORT DONE][{partner.name}] Purchases={partner.transfers_purchase_ids.ids}")
                bus.send_notification(
                    title="Fulfillment Sync",
                    message=f"Импорт складов для партнёра {partner.name}",
                    level="info"
                )
                self.env['stock.warehouse'].sudo().with_context(skip_api_sync=True).import_warehouses(partner)

            bus.send_notification(
                title="Fulfillment Sync",
                message="Импорт данных о трансферах...",
                level="info"
            )

            for item in data:
                fulfillment_id = item.get("fulfillment_id")
                if fulfillment_id:
                    page = 1
                    limit = 100
                    while True:
                        bus.send_notification(
                            title="Fulfillment Sync",
                            message=f"Загрузка трансферов для {fulfillment_id}, страница {page}",
                            level="info"
                        )
                        success = self.env['stock.picking'].sudo().with_context(skip_fulfillment_push=True).import_transfers(
                            fulfillment_id=fulfillment_id,
                            page=page,
                            limit=limit
                        )
                        if not success or success < limit:
                            break
                        page += 1

            bus.send_notification(
                title="Fulfillment Sync",
                message="Проверка складов для импорта остатков...",
                level="info"
            )

            warehouses = self.env['stock.warehouse'].search([
                ('fulfillment_warehouse_id', '!=', False)
            ])
            if not warehouses:
                bus.send_notification(
                    title="Fulfillment Sync",
                    message="Нет складов с fulfillment_warehouse_id — импорт остатков пропущен",
                    level="warning",
                    sticky=True
                )
            else:
                for warehouse in warehouses:
                    bus.send_notification(
                        title="Fulfillment Sync",
                        message=f"Импорт остатков для склада {warehouse.name}",
                        level="info"
                    )
                    self.env['stock.quant'].sudo().import_stock(
                        filters={"warehouse_ids": [warehouse.fulfillment_warehouse_id]}
                    )

            bus.send_notification(
                title="Fulfillment Sync",
                message="Импорт данных из Fulfillment успешно завершён",
                level="success",
                sticky=True
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
            bus.send_notification(
                title="Fulfillment Sync Error",
                message=f"Ошибка при выполнении синхронизации: {str(e)}",
                level="danger",
                sticky=True
            )
            return False

    def button_run_import_all(self):
        """Кнопка запуска полной синхронизации"""
        profile = self._get_active_profile()
        success = self.import_all(profile=profile)
        if not success:
            self._notify_bus("Fulfillment Sync", "Синхронизация не удалась", "danger", True)
            return False
        return {
            'effect': {
                'type': 'rainbow_man',
                'message': 'Import success',
                'fadeout': 'slow',
            }
        }

    # ---------- Контакты ----------
    def import_contacts(self, partner_record):
        """Создаём или обновляем контакт res.partner"""
        tag = self._get_fulfillment_tag()
        self._notify_bus("Импорт", f"Импорт контактов для {partner_record.name}", "info")

        contact_vals = {
            'name': partner_record.name,
            'comment': f"Synced from Fulfillment {partner_record.api_domain or ''}",
            'category_id': [(4, tag.id)],
            'fulfillment_partner_id': partner_record.fulfillment_id,
        }

        contact = self.env['res.partner'].search([
            ('fulfillment_partner_id', '=', partner_record.fulfillment_id)
        ], limit=1)

        if contact:
            contact.write(contact_vals)
        else:
            contact = self.env['res.partner'].create(contact_vals)

        partner_record.partner_id = contact.id
        self.env.cr.commit()
        return contact

    # ---------- Служебные ----------
    def _get_active_profile(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            raise UserError("No active profile with API key configured")
        return profile

    def _fetch_api_data(self, profile):
        try:
            client = FulfillmentAPIClient(profile)
            data = client.fulfillment.list().get("data", [])
            _logger.info("📦 Received %s partners from API", len(data))
            return data
        except FulfillmentAPIError as e:
            raise UserError(str(e))
        except Exception as e:
            raise UserError(f"API request failed: {str(e)}")

    def _process_api_data(self, data, profile):
        for item in data:
            self.import_partners(item, profile)

    def import_partners(self, item, profile):
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

        if existing:
            existing.write(vals)
            partner_record = existing
        else:
            partner_record = self.create(vals)

        contact = self.import_contacts(partner_record)
        return partner_record

    def _normalize_datetime(self, dt_str):
        if not dt_str:
            return False
        for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
            try:
                return datetime.strptime(dt_str, fmt)
            except ValueError:
                continue
        return False

    def _get_fulfillment_tag(self):
        tag = self.env['res.partner.category'].search([('name', '=', 'Fulfillment')], limit=1)
        return tag or self.env['res.partner.category'].create({'name': 'Fulfillment'})
