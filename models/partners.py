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

    # Inverse M2M for products (defined on product.template side)
    sale_product_ids = fields.Many2many(
        'product.template',
        'product_sale_fulfillment_rel',
        'partner_id',
        'product_id',
        string='Products for Sale',
    )
    purchase_product_ids = fields.Many2many(
        'product.template',
        'product_purchase_fulfillment_rel',
        'partner_id',
        'product_id',
        string='Products for Purchase',
    )

    # ---- Dashboard counters (non-stored, recomputed on access) ----
    warehouse_count = fields.Integer(compute='_compute_network_counts', string='Warehouses')
    transfer_count = fields.Integer(compute='_compute_network_counts', string='Transfers')
    product_count = fields.Integer(compute='_compute_network_counts', string='Products')
    order_count = fields.Integer(compute='_compute_network_counts', string='Orders')
    client_count = fields.Integer(compute='_compute_network_counts', string='Clients')

    # ---- Messaging (tracking only — chatter is the UI) ----
    message_ids_fulfillment = fields.One2many(
        'fulfillment.message', 'partner_id',
        string='API Message Tracking',
    )

    @api.depends(
        'warehouses_client_ids', 'warehouses_owner_ids',
        'transfers_purchase_ids', 'transfers_internal_ids', 'transfers_delivery_ids',
        'sale_product_ids', 'purchase_product_ids',
    )
    def _compute_network_counts(self):
        for rec in self:
            rec.warehouse_count = len(rec.warehouses_owner_ids) + len(rec.warehouses_client_ids)
            rec.transfer_count = (
                len(rec.transfers_purchase_ids)
                + len(rec.transfers_internal_ids)
                + len(rec.transfers_delivery_ids)
            )
            rec.product_count = len(rec.sale_product_ids | rec.purchase_product_ids)
            rec.client_count = len(
                rec.transfers_delivery_ids.mapped('partner_id').filtered('id')
            )
            order_lines = self.env['sale.order.line'].search([
                ('fulfillment_item_manager', '=', rec.id)
            ])
            rec.order_count = len(order_lines.mapped('order_id'))

    # ---- Chatter → API bridge ----
    def message_post(self, **kwargs):
        """
        Intercept chatter messages and forward them to the Fulfillment API.
        Only real user comments (not internal notes, not system messages)
        are forwarded.  The context flag 'from_fulfillment_api' prevents
        messages received from the API being re-sent back.
        """
        result = super().message_post(**kwargs)

        # Skip if message originated from the API (avoid infinite loop)
        if self.env.context.get('from_fulfillment_api'):
            return result

        # Only forward regular comments, not internal notes
        message_type = kwargs.get('message_type', 'comment')
        subtype_xmlid = kwargs.get('subtype_xmlid', 'mail.mt_comment')
        if message_type != 'comment' or subtype_xmlid == 'mail.mt_note':
            return result

        # Skip OdooBot (automated messages posted by the system scheduler)
        if self.env.user.id == self.env.ref('base.user_root', raise_if_not_found=False).id:
            return result

        body_html = kwargs.get('body', '')
        if not body_html:
            return result

        from odoo.tools import html2plaintext
        content = html2plaintext(body_html).strip()
        if not content:
            return result

        profile = self._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            return result

        client = FulfillmentAPIClient(profile)

        for partner in self.filtered(lambda p: p.status == 'follow' and p.fulfillment_id):
            try:
                api_result = client.message.send(
                    sender_fulfillment_id=profile.fulfillment_profile_id,
                    receiver_fulfillment_id=partner.fulfillment_id,
                    content=content,
                )
                api_msg = api_result.get('data', api_result)
                self.env['fulfillment.message'].create({
                    'partner_id': partner.id,
                    'external_id': api_msg.get('id'),
                    'direction': 'out',
                    'sent_at': fields.Datetime.now(),
                })
            except Exception as e:
                _logger.warning(
                    "[FulfillmentPartners] Failed to send chatter message to API for %s: %s",
                    partner.name, e,
                )

        return result

    # ---- Smart-button actions ----
    def action_view_warehouses(self):
        self.ensure_one()
        wh_ids = (self.warehouses_owner_ids | self.warehouses_client_ids).ids
        return {
            'type': 'ir.actions.act_window',
            'name': f'Warehouses — {self.name}',
            'res_model': 'stock.warehouse',
            'view_mode': 'list,form',
            'domain': [('id', 'in', wh_ids)],
        }

    def action_view_products(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'Products — {self.name}',
            'res_model': 'product.template',
            'view_mode': 'list,kanban,form',
            'domain': ['|',
                       ('sale_fulfillment_partner_ids', 'in', [self.id]),
                       ('purchase_fulfillment_partner_ids', 'in', [self.id])],
        }

    def action_view_orders(self):
        self.ensure_one()
        order_lines = self.env['sale.order.line'].search([
            ('fulfillment_item_manager', '=', self.id)
        ])
        order_ids = order_lines.mapped('order_id').ids
        return {
            'type': 'ir.actions.act_window',
            'name': f'Orders — {self.name}',
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', order_ids)],
        }

    def action_view_clients(self):
        self.ensure_one()
        client_ids = (
            self.transfers_delivery_ids.mapped('partner_id').filtered('id').ids
        )
        return {
            'type': 'ir.actions.act_window',
            'name': f'Clients — {self.name}',
            'res_model': 'res.partner',
            'view_mode': 'list,kanban,form',
            'domain': [('id', 'in', client_ids)],
        }

    def action_view_transfers(self):
        self.ensure_one()
        picking_ids = (
            self.transfers_purchase_ids
            | self.transfers_internal_ids
            | self.transfers_delivery_ids
        ).ids
        return {
            'type': 'ir.actions.act_window',
            'name': f'Transfers — {self.name}',
            'res_model': 'stock.picking',
            'view_mode': 'list,form',
            'domain': [('id', 'in', picking_ids)],
        }

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
        try:
            bus.send_notification(
                title="Fulfillment Sync",
                message="Start of full synchronization with Fulfillment API",
                level="info",
                sticky=True
            )

            if not profile:
                bus.send_notification(
                    title="Fulfillment Sync",
                    message="No active profile with API key found",
                    level="danger",
                    sticky=True
                )
                return False

            bus.send_notification(
                title="Fulfillment Sync",
                message="Getting data from the Fulfillment API...",
                level="info"
            )

            data = self._fetch_api_data(profile)
            if not data:
                bus.send_notification(
                    title="Fulfillment Sync",
                    message="Data from the API was not received",
                    level="warning",
                    sticky=True
                )
                return False

            bus.send_notification(
                title="Fulfillment Sync",
                message="Processing of the received data...",
                level="info"
            )
            self._process_api_data(data, profile)

            bus.send_notification(
                title="Fulfillment Sync",
                message="Importing partner and related warehouse data...",
                level="info"
            )

            for partner in self.search([]):
                _logger.info(f"[IMPORT DONE][{partner.name}] Purchases={partner.transfers_purchase_ids.ids}")
                bus.send_notification(
                    title="Fulfillment Sync",
                    message=f"Importing warehouses for a partner {partner.name}",
                    level="info"
                )
                self.env['stock.warehouse'].sudo().with_context(skip_api_sync=True).import_warehouses(partner)

            bus.send_notification(
                title="Fulfillment Sync",
                message="Importing transfer data...",
                level="info"
            )

            for item in data:
                fulfillment_id = item.get("id")
                if fulfillment_id:
                    page = 1
                    limit = 100
                    while True:
                        bus.send_notification(
                            title="Fulfillment Sync",
                            message=f"Downloading transfers for {fulfillment_id}, page {page}",
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
                message="Checking warehouses for importing balances...",
                level="info"
            )

            warehouses = self.env['stock.warehouse'].search([
                ('fulfillment_warehouse_id', '!=', False)
            ])
            if not warehouses:
                bus.send_notification(
                    title="Fulfillment Sync",
                    message="No warehouses with fulfillment_warehouse_id — import of balances skipped",
                    level="warning",
                    sticky=True
                )
            else:
                for warehouse in warehouses:
                    bus.send_notification(
                        title="Fulfillment Sync",
                        message=f"Importing balances for warehouse {warehouse.name}",
                        level="info"
                    )
                    self.env['stock.quant'].sudo().import_stock(
                        filters={"warehouse_ids": [warehouse.fulfillment_warehouse_id]}
                    )

            bus.send_notification(
                title="Fulfillment Sync",
                message="Data import from Fulfillment successfully completed",
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

    @api.model
    def cron_auto_import_from_api(self):
        """Scheduled backup: same as «Run import all» when allow_auto_import is on."""
        profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
        if not profile or not profile.allow_auto_import:
            return
        try:
            prof = self._get_active_profile()
        except UserError as err:
            _logger.debug("[cron_auto_import_from_api] skip: %s", err)
            return
        try:
            self.import_all(profile=prof)
        except Exception as err:
            _logger.exception("[cron_auto_import_from_api] failed: %s", err)

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
#