from odoo import models, fields, api
from odoo.exceptions import UserError
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError
import logging
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)


class FulfillmentMessage(models.Model):
    _name = 'fulfillment.message'
    _description = 'Fulfillment Inter-Instance Message'
    _order = 'sent_at asc, id asc'
    _rec_name = 'content_preview'

    partner_id = fields.Many2one(
        'fulfillment.partners',
        string='Partner',
        required=True,
        ondelete='cascade',
        index=True,
    )
    external_id = fields.Char(string='External ID', index=True, readonly=True)
    direction = fields.Selection(
        [('out', 'Outgoing'), ('in', 'Incoming')],
        string='Direction',
        required=True,
    )
    content = fields.Text(string='Message', required=True)
    content_preview = fields.Char(
        string='Preview',
        compute='_compute_preview',
        store=True,
    )
    is_read = fields.Boolean(string='Read', default=False)
    ref_type = fields.Char(string='Reference Type')   # 'transfer' | 'order'
    ref_id = fields.Char(string='Reference ID')
    sent_at = fields.Datetime(string='Sent At', required=True, default=fields.Datetime.now)

    @api.depends('content')
    def _compute_preview(self):
        for rec in self:
            rec.content_preview = (rec.content or '')[:60]

    # ------------------------------------------------------------------ #
    #  Send a new outgoing message to a partner instance via the API
    # ------------------------------------------------------------------ #
    def action_send_message(self, partner, content, ref_type=None, ref_id=None):
        """
        Send a message to `partner` (fulfillment.partners record).
        Creates a local record and pushes it to the central API.
        """
        profile = self.env['fulfillment.partners']._get_active_profile()
        if not profile:
            raise UserError("Fulfillment profile is not configured.")
        if not profile.fulfillment_profile_id:
            raise UserError("This Odoo instance has no fulfillment_profile_id. Please sync the profile first.")
        if not partner.fulfillment_id:
            raise UserError(f"Partner '{partner.name}' has no fulfillment_id.")

        client = FulfillmentAPIClient(profile)
        try:
            result = client.message.send(
                sender_fulfillment_id=profile.fulfillment_profile_id,
                receiver_fulfillment_id=partner.fulfillment_id,
                content=content,
                ref_type=ref_type,
                ref_id=ref_id,
            )
        except FulfillmentAPIError as e:
            raise UserError(f"Failed to send message: {e}")

        api_msg = result.get('data', result)
        return self.env['fulfillment.message'].create({
            'partner_id': partner.id,
            'external_id': api_msg.get('id'),
            'direction': 'out',
            'content': content,
            'ref_type': ref_type,
            'ref_id': ref_id,
            'is_read': True,
            'sent_at': fields.Datetime.now(),
        })

    # ------------------------------------------------------------------ #
    #  Poll incoming messages for all followed partners
    # ------------------------------------------------------------------ #
    @api.model
    def _poll_new_messages(self):
        """
        Called by ir.cron every minute.
        Fetches new messages from the API for every followed partner.
        """
        profile = self.env['fulfillment.partners']._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            _logger.debug("[FulfillmentMessage] No profile configured, skipping poll.")
            return

        partners = self.env['fulfillment.partners'].search([
            ('status', '=', 'follow'),
            ('fulfillment_id', '!=', False),
        ])
        if not partners:
            return

        client = FulfillmentAPIClient(profile)
        my_fulfillment_id = profile.fulfillment_profile_id

        for partner in partners:
            try:
                self._poll_partner(client, my_fulfillment_id, partner)
            except Exception as e:
                _logger.warning(
                    "[FulfillmentMessage] Poll failed for partner %s: %s",
                    partner.name, e,
                )

    def _poll_partner(self, client, my_fulfillment_id, partner):
        """Fetch new incoming messages from a single partner and create local records."""
        # Find the most recent message we have from this partner (for since= cursor)
        last = self.search([
            ('partner_id', '=', partner.id),
            ('direction', '=', 'in'),
        ], order='sent_at desc', limit=1)

        since = None
        if last:
            since = last.sent_at.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        result = client.message.fetch(
            fulfillment_id=my_fulfillment_id,
            partner_id=partner.fulfillment_id,
            since=since,
            limit=100,
        )

        api_messages = result.get('data', [])
        if not api_messages:
            return

        existing_ext_ids = set(
            self.search([('partner_id', '=', partner.id)]).mapped('external_id')
        )

        new_records = []
        unread_ids = []

        for msg in api_messages:
            ext_id = msg.get('id')
            if ext_id in existing_ext_ids:
                continue

            is_incoming = msg.get('sender_fulfillment_id') == partner.fulfillment_id
            direction = 'in' if is_incoming else 'out'

            sent_at_str = msg.get('created_at', '')
            try:
                sent_at = datetime.strptime(sent_at_str[:19], '%Y-%m-%dT%H:%M:%S')
            except (ValueError, TypeError):
                sent_at = fields.Datetime.now()

            new_records.append({
                'partner_id': partner.id,
                'external_id': ext_id,
                'direction': direction,
                'content': msg.get('content', ''),
                'ref_type': msg.get('ref_type'),
                'ref_id': msg.get('ref_id'),
                'is_read': not is_incoming,  # outgoing = already "read"
                'sent_at': sent_at,
            })
            if is_incoming:
                unread_ids.append(ext_id)

        if new_records:
            created = self.create(new_records)
            _logger.info(
                "[FulfillmentMessage] Imported %d new messages from partner %s",
                len(created), partner.name,
            )
            # Notify Odoo users about new incoming messages
            if unread_ids:
                self._notify_new_messages(partner, len(unread_ids))

    def _notify_new_messages(self, partner, count):
        """Create a system notification for new incoming messages."""
        try:
            partner.message_post(
                body=f"📩 {count} new message(s) from {partner.name}",
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
        except Exception:
            pass
