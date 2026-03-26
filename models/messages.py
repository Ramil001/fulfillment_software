from odoo import models, fields, api, _
from odoo.tools import html2plaintext
from odoo.exceptions import ValidationError
from ..lib.api_client import FulfillmentAPIClient
import logging
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)


class FulfillmentMessage(models.Model):
    """
    Tracks messages exchanged via the Fulfillment API.
    Used only for deduplication (stores external_id) so we never
    import the same message twice.  The actual UI is the native
    Odoo chatter on fulfillment.partners.
    """
    _name = 'fulfillment.message'
    _description = 'Fulfillment Inter-Instance Message (tracking)'
    _order = 'sent_at desc'
    _rec_name = 'external_id'

    partner_id = fields.Many2one(
        'fulfillment.partners',
        string='Partner',
        ondelete='cascade',
        index=True,
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Transfer picking',
        ondelete='cascade',
        index=True,
    )
    external_id = fields.Char(string='External ID', index=True, readonly=True)
    direction = fields.Selection(
        [('out', 'Outgoing'), ('in', 'Incoming')],
        required=True,
    )
    sent_at = fields.Datetime(default=fields.Datetime.now)

    @api.constrains('partner_id', 'picking_id')
    def _check_partner_or_picking(self):
        for rec in self:
            if not rec.partner_id and not rec.picking_id:
                raise ValidationError(
                    _('Link a fulfillment partner and/or a transfer picking for this API message.')
                )

    # ------------------------------------------------------------------ #
    #  Cron: poll incoming messages for all followed partners
    # ------------------------------------------------------------------ #
    @api.model
    def _poll_new_messages(self):
        """Called by ir.cron every minute."""
        profile = self.env['fulfillment.partners']._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            _logger.debug("[FulfillmentMessage] No profile — skipping poll.")
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
                    "[FulfillmentMessage] Poll failed for %s: %s",
                    partner.name, e,
                )

    def _poll_partner(self, client, my_fulfillment_id, partner):
        """Fetch new messages from API and post them into the partner's chatter."""
        # Use the most recent tracked message as since= cursor
        last = self.search([
            ('partner_id', '=', partner.id),
            ('direction', '=', 'in'),
        ], order='sent_at desc', limit=1)

        if last:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            since_dt = min(last.sent_at, now_utc)
            since = since_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        else:
            since = None

        result = client.message.fetch(
            fulfillment_id=my_fulfillment_id,
            partner_id=partner.fulfillment_id,
            since=since,
            limit=100,
        )

        api_messages = result.get('data', [])
        if not api_messages:
            return

        ext_ids = [m.get('id') for m in api_messages if m.get('id')]
        existing_ext_ids = set(
            self.search([('external_id', 'in', ext_ids)]).mapped('external_id')
        ) if ext_ids else set()

        # Use partner's linked res.partner as message author (so name shows in chatter)
        author_id = partner.partner_id.id if partner.partner_id else False

        new_count = 0
        Picking = self.env['stock.picking'].sudo()
        for msg in api_messages:
            ext_id = msg.get('id')
            if not ext_id or ext_id in existing_ext_ids:
                continue

            is_incoming = msg.get('sender_fulfillment_id') == partner.fulfillment_id
            direction = 'in' if is_incoming else 'out'

            sent_at_str = msg.get('created_at', '')
            try:
                sent_at = datetime.strptime(sent_at_str[:19], '%Y-%m-%dT%H:%M:%S')
            except (ValueError, TypeError):
                sent_at = fields.Datetime.now()

            content = msg.get('content', '')
            ref_type = (msg.get('ref_type') or '').strip()
            ref_id = (msg.get('ref_id') or '').strip()

            posted_to_picking = False
            picking_rec = self.env['stock.picking']
            if (
                ref_type == 'transfer'
                and ref_id
                and is_incoming
                and content
            ):
                candidates = Picking.search(
                    [('fulfillment_transfer_id', '=', ref_id)], limit=2
                )
                if len(candidates) > 1:
                    _logger.warning(
                        "[FulfillmentMessage] Multiple pickings for transfer %s; using first",
                        ref_id,
                    )
                picking_rec = candidates[:1]
                if picking_rec:
                    picking_rec.with_context(
                        from_fulfillment_api=True,
                        mail_notify_force_send=False,
                    ).message_post(
                        body=content,
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                        author_id=author_id,
                    )
                    posted_to_picking = True
                    new_count += 1
                    notification_payload = {
                        'content': content,
                        'partner_name': partner.name,
                        'partner_id': partner.id,
                        'external_id': ext_id,
                        'picking_id': picking_rec.id,
                    }
                    bus = self.env['bus.bus'].sudo()
                    for user in self.env['res.users'].sudo().search(
                        [('share', '=', False), ('active', '=', True)]
                    ):
                        if user.partner_id:
                            bus._sendone(
                                user.partner_id, 'fulfillment_new_message', notification_payload
                            )

            if is_incoming and content and not posted_to_picking:
                # Post into partner chatter; disable email to avoid smtp-exception popups
                partner.with_context(
                    from_fulfillment_api=True,
                    mail_notify_force_send=False,
                ).message_post(
                    body=content,
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                    author_id=author_id,
                )
                new_count += 1

                # Push bus notification so the chat popup appears in the UI
                notification_payload = {
                    'content': content,
                    'partner_name': partner.name,
                    'partner_id': partner.id,
                    'external_id': ext_id,
                }
                bus = self.env['bus.bus'].sudo()
                for user in self.env['res.users'].sudo().search([('share', '=', False), ('active', '=', True)]):
                    if user.partner_id:
                        bus._sendone(user.partner_id, 'fulfillment_new_message', notification_payload)

            track_vals = {
                'partner_id': partner.id,
                'external_id': ext_id,
                'direction': direction,
                'sent_at': sent_at,
            }
            if posted_to_picking and picking_rec:
                track_vals['picking_id'] = picking_rec.id
            self.create(track_vals)

        if new_count:
            _logger.info(
                "[FulfillmentMessage] Posted %d new messages from %s into chatter",
                new_count, partner.name,
            )
