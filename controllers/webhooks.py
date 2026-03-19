import json
from odoo import http
from odoo.http import request
from odoo import SUPERUSER_ID
import logging

_logger = logging.getLogger(__name__)


class FulfillmentWebhookController(http.Controller):

    @http.route('/fulfillment/status', type="http", auth="public")
    def status(self):
        return request.make_response(
            '{"status": "ok"}',
            headers=[('Content-Type', 'application/json')]
        )

    @http.route(
        '/fulfillment/webhook/message',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
    )
    def receive_message(self, **kwargs):
        """
        Called by api.fulfillment.software immediately when a message is sent.
        Accepts plain JSON body (not JSON-RPC wrapped).
        """
        try:
            data = json.loads(request.httprequest.data or b'{}')
        except Exception:
            data = {}

        ext_id = data.get('id')
        sender_fulfillment_id = data.get('sender_fulfillment_id')
        content = (data.get('content') or '').strip()
        sender_name = data.get('sender_name', 'Partner')

        def _json(data, status=200):
            return request.make_response(
                json.dumps(data),
                headers=[('Content-Type', 'application/json')],
                status=status,
            )

        if not sender_fulfillment_id or not content:
            return _json({'status': 'ignored', 'reason': 'missing fields'})

        env = request.env(user=SUPERUSER_ID)

        # Find the partner who sent this
        partner = env['fulfillment.partners'].search([
            ('fulfillment_id', '=', sender_fulfillment_id),
            ('status', '=', 'follow'),
        ], limit=1)

        if not partner:
            _logger.debug("[Webhook] No followed partner for fulfillment_id=%s", sender_fulfillment_id)
            return _json({'status': 'ignored', 'reason': 'partner not found'})

        # Deduplication — skip if already imported
        if ext_id:
            existing = env['fulfillment.message'].search(
                [('external_id', '=', ext_id)], limit=1
            )
            if existing:
                return _json({'status': 'duplicate'})

        # Post to chatter; disable email sending to avoid smtp-exception popups
        author_id = partner.partner_id.id if partner.partner_id else False
        partner.with_context(
            from_fulfillment_api=True,
            mail_notify_force_send=False,
        ).message_post(
            body=content,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=author_id,
        )

        # Track for deduplication
        sent_at = data.get('created_at')
        from odoo import fields as odoo_fields
        from datetime import datetime
        try:
            sent_at_dt = datetime.strptime(sent_at[:19], '%Y-%m-%dT%H:%M:%S') if sent_at else odoo_fields.Datetime.now()
        except Exception:
            sent_at_dt = odoo_fields.Datetime.now()

        env['fulfillment.message'].create({
            'partner_id': partner.id,
            'external_id': ext_id,
            'direction': 'in',
            'sent_at': sent_at_dt,
        })
        env.cr.commit()

        # Send bus notification to all internal users → triggers popup on frontend
        notification_payload = {
            'partner_id': partner.id,
            'partner_name': partner.name,
            'partner_fulfillment_id': sender_fulfillment_id,
            'content': content,
            'external_id': ext_id,
        }
        try:
            bus = env['bus.bus'].sudo()
            users = env['res.users'].sudo().search([
                ('share', '=', False),
                ('active', '=', True),
            ])
            for user in users:
                if user.partner_id:
                    bus._sendone(user.partner_id, 'fulfillment_new_message', notification_payload)
        except Exception as e:
            _logger.warning("[Webhook] Bus notification failed: %s", e)

        _logger.info(
            "[Webhook] Message from %s (%s) delivered to chatter",
            partner.name, sender_fulfillment_id,
        )
        return _json({'status': 'ok', 'partner': partner.name})
