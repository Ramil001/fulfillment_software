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
    
    fulfillment_contact_id = fields.Char(
        string="Fulfillment Contact Id",
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


    fulfillment_id = fields.Char(string="Fulfillment ID", required=True, index=True, readonly=True)
    name = fields.Char(string="Company name", required=True, readonly=True)
    fulfillment_logo = fields.Binary(string="Logo", attachment=True)
    api_domain = fields.Char(string="API", readonly=True)
    webhook_domain = fields.Char(string="Webhook domain", help="A webhook is the URL of the site where your Odoo runs. It is necessary to call the update function when your Odoo needs to update resources.")
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


    def message_post(self, **kwargs):
        """Forward user-written comments to the Fulfillment API (partner chatter → API)."""
        result = super().message_post(**kwargs)

        _logger.info(
            '[FulfillmentMessage][partner] message_post called: '
            'message_type=%s subtype=%s from_api=%s',
            kwargs.get('message_type'),
            kwargs.get('subtype_xmlid'),
            self.env.context.get('from_fulfillment_api'),
        )

        msg_type = kwargs.get('message_type', '')
        is_user_comment = msg_type in ('comment', '') or not msg_type
        # Only forward genuine user comments — skip system log notes, tracking msgs, etc.
        if (
            not self.env.context.get('from_fulfillment_api')
            and is_user_comment
            and kwargs.get('body')
        ):
            from odoo.tools import html2plaintext
            content = html2plaintext(kwargs.get('body', '')).strip()
            if content:
                profile = self.env['fulfillment.profile'].search([], limit=1)
                if profile and profile.fulfillment_profile_id:
                    from ..lib.api_client import FulfillmentAPIClient
                    client = FulfillmentAPIClient(profile)
                    for rec in self:
                        if rec.fulfillment_id:
                            try:
                                client.message.send(
                                    sender_fulfillment_id=profile.fulfillment_profile_id,
                                    receiver_fulfillment_id=rec.fulfillment_id,
                                    content=content,
                                )
                                _logger.info(
                                    '[FulfillmentMessage] Sent partner message to %s', rec.name
                                )
                            except Exception as e:
                                _logger.warning(
                                    '[FulfillmentMessage] Failed to send to API for %s: %s',
                                    rec.name, e,
                                )
        return result

    def action_fill_webhook_domain(self):
        _logger.info(f"[action_fill_webhook_domain]")
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for rec in self:
            rec.webhook_domain = base_url
                
    def _notify_bus(self, title, message, level="info", sticky=False):
        _logger.info(f"[_notify_bus]")
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
        _logger.info(f"[action_follow]")
        self.write({'status': 'follow'})

    def action_unfollow(self):
        _logger.info(f"[action_unfollow]")
        self.write({'status': 'unfollow'})

    def _valid_field_parameter(self, field, name):
        _logger.info(f"[_valid_field_parameter]")
        return name == 'password' or super()._valid_field_parameter(field, name)

    # ---------- Основная синхронизация ----------
    @api.model
    def import_all(self, profile=None):
        _logger.info(f"[import_all]")
        bus = self.env['bus.utils']
        bus.send_sync_status(running=True)
        try:
            if not profile:
                _logger.warning("[import_all] No active profile with API key found")
                bus.send_sync_status(running=False)
                return False

            data = self._fetch_api_data(profile)
            if not data:
                _logger.warning("[import_all] No data received from API")
                bus.send_sync_status(running=False)
                return False

            self._process_api_data(data, profile)

            for partner in self.search([]):
                _logger.info("[import_all] Importing warehouses for %s", partner.name)
                self.env['stock.warehouse'].sudo().with_context(skip_api_sync=True).import_warehouses(partner)

            for item in data:
                fulfillment_id = item.get("id")
                if fulfillment_id:
                    page = 1
                    limit = 100
                    while True:
                        _logger.info("[import_all] Transfers page %s for %s", page, fulfillment_id)
                        success = self.env['stock.picking'].sudo().with_context(skip_fulfillment_push=True).import_transfers(
                            fulfillment_id=fulfillment_id,
                            page=page,
                            limit=limit
                        )
                        if not success or success < limit:
                            break
                        page += 1

            warehouses = self.env['stock.warehouse'].search([
                ('fulfillment_warehouse_id', '!=', False)
            ])
            for warehouse in warehouses:
                _logger.info("[import_all] Importing stock for warehouse %s", warehouse.name)
                self.env['stock.quant'].sudo().import_stock(
                    filters={"warehouse_ids": [warehouse.fulfillment_warehouse_id]}
                )

            bus.send_sync_status(running=False)
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
            _logger.error("[import_all] Sync failed: %s", str(e))
            bus.send_sync_status(running=False)
            bus.send_notification(
                title="Fulfillment Sync Error",
                message=f"Error during synchronization: {str(e)}",
                level="danger",
                sticky=True
            )
            return False
        
    def _activate_stock_settings(self):
        _logger.info(f"[_activate_stock_settings]")
        try:
            Settings = self.env['res.config.settings'].sudo()
            
            # Создаем настройки. Важно: иногда нужно получить default значения,
            # но для принудительного включения True должно хватить.
            config = Settings.create({
                'group_stock_multi_locations': True,
                'group_stock_adv_location': True,
            })
            
           
            config.execute()
            
           
            _logger.info("✔ Settings applied")
            return True
        except Exception as e:
            _logger.error(f"❌ Error: {e}")
            return False
        
    def button_run_import_all(self):
        _logger.info(f"[button_run_import_all]")
        
        self._activate_stock_settings()
        
        profile = self._get_active_profile()
        success = self.import_all(profile=profile)
        if not success:
            self._notify_bus("Fulfillment Sync", "Synchronization failed", "danger", True)
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
        _logger.info(f"[import_contacts]")
        
        tag = self._get_fulfillment_tag()
        self._notify_bus("Import", f"Import contacts for {partner_record.name}", "info")

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
        _logger.info(f"[_get_active_profile]")
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            raise UserError("You need to set the API key in the Fulfillment settings.")
        return profile

    def _fetch_api_data(self, profile):
        _logger.info(f"[_fetch_api_data]")
        try:
            client = FulfillmentAPIClient(profile)
            data = client.fulfillment.list().get("data", [])
            _logger.info("Received %s partners from API", len(data))
            return data
        except FulfillmentAPIError as e:
            raise UserError(str(e))
        except Exception as e:
            raise UserError(f"API request failed: {str(e)}")

    def _process_api_data(self, data, profile):
        _logger.info(f"[_process_api_data]")
        for item in data:
            self.import_partners(item, profile)

    def import_partners(self, item, profile):
        _logger.info(f"[import_partners]")
        existing = self.search([('fulfillment_id', '=', item['id'])], limit=1)
        created_at = self._normalize_datetime(item.get('created_at'))
        vals = {
            'name': item.get('name') or 'Without name',
            'fulfillment_id': item.get('id'),
            'api_domain': item.get('api_domain'),
            'webhook_domain': item.get('webhook_domain'),
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
        _logger.info(f"[_normalize_datetime]")
        if not dt_str:
            return False
        for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
            try:
                return datetime.strptime(dt_str, fmt)
            except ValueError:
                continue
        return False

    def _get_fulfillment_tag(self):
        _logger.info(f"[_get_fulfillment_tag]")
        tag = self.env['res.partner.category'].search([('name', '=', 'Fulfillment')], limit=1)
        return tag or self.env['res.partner.category'].create({'name': 'Fulfillment'})

    # ---------- Фоновый крон-фолбэк (догоняет офлайн) ----------
    @api.model
    def cron_auto_import_from_api(self):
        """
        Запускается каждые 5 минут.  Для каждого известного Fulfillment-аккаунта
        вычитывает трансферы начиная с последнего сохранённого курсора (next_page_token).
        Это гарантирует, что если Odoo была офлайн и пропустила webhook-и, все
        пропущенные трансферы будут подтянуты при следующем запуске.
        """
        _logger.info('[CRON] cron_auto_import_from_api started')

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            _logger.debug('[CRON] No active profile — skipping')
            return

        from ..lib.api_client import FulfillmentAPIClient
        client = FulfillmentAPIClient(profile)

        try:
            data = client.fulfillment.list().get('data', [])
        except Exception as e:
            _logger.error('[CRON] Failed to fetch fulfillment list: %s', e)
            return

        Params = self.env['ir.config_parameter'].sudo()
        Picking = self.env['stock.picking'].sudo().with_context(skip_fulfillment_push=True)

        for item in data:
            fulfillment_id = item.get('id')
            if not fulfillment_id:
                continue

            cursor_key = f'fulfillment.transfer.cursor.{fulfillment_id}'
            cursor = Params.get_param(cursor_key) or None

            _logger.info(
                '[CRON] Syncing fulfillment=%s cursor=%s', fulfillment_id, cursor
            )

            try:
                while True:
                    response = client.transfer.list(
                        fulfillment_id=fulfillment_id,
                        limit=50,
                        next_page_token=cursor,
                    )
                    transfers = response.get('data', [])
                    meta = response.get('meta', {})

                    if not transfers:
                        break

                    for transfer in transfers:
                        try:
                            Picking._import_transfer(transfer)
                        except Exception as e:
                            _logger.error(
                                '[CRON] Import failed for transfer %s: %s',
                                transfer.get('id'), e,
                            )

                    next_cursor = meta.get('next_page_token')
                    if next_cursor:
                        Params.set_param(cursor_key, next_cursor)
                        cursor = next_cursor

                    if not meta.get('has_more'):
                        break

            except Exception as e:
                _logger.error(
                    '[CRON] Error syncing fulfillment %s: %s', fulfillment_id, e
                )

        _logger.info('[CRON] cron_auto_import_from_api finished')