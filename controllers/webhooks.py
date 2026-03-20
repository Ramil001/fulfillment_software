import json
import logging
import threading

import odoo
from odoo import api, http, SUPERUSER_ID
from odoo.http import request

_logger = logging.getLogger(__name__)


def _run_fulfillment_import_all(dbname):
    """Background worker: same logic as Partners → Run import all (HTTP must return fast)."""
    try:
        registry = odoo.registry(dbname)
        with registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            profile = env["fulfillment.profile"].sudo().search([], limit=1)
            if not profile:
                _logger.info("[Fulfillment webhook/sync] skipped: no fulfillment.profile")
                return
            if not profile.allow_auto_import:
                _logger.info("[Fulfillment webhook/sync] skipped: allow_auto_import is disabled")
                return
            partners = env["fulfillment.partners"].sudo()
            try:
                prof = partners._get_active_profile()
            except Exception as err:
                _logger.warning("[Fulfillment webhook/sync] no active profile: %s", err)
                return
            partners.import_all(profile=prof)
            cr.commit()
            _logger.info("[Fulfillment webhook/sync] import_all finished OK")
    except Exception:
        _logger.exception("[Fulfillment webhook/sync] import_all failed")


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

    @http.route(
        '/fulfillment/webhook/sync',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
    )
    def receive_sync(self, **kwargs):
        """
        Called by api.fulfillment.software after orders/transfers (and similar) change.
        Runs the same import as Partners → «Run import all», in a background thread.
        Optional shared secret: ir.config_parameter `fulfillment.webhook_sync_token`
        must match header `X-Fulfillment-Sync-Token` when the parameter is set.
        Requires fulfillment.profile.allow_auto_import = True.
        """
        try:
            data = json.loads(request.httprequest.data or b'{}')
        except Exception:
            data = {}

        def _json(body, status=200):
            return request.make_response(
                json.dumps(body),
                headers=[('Content-Type', 'application/json')],
                status=status,
            )

        # Token check (optional): only when parameter is configured on this DB
        expected = request.env['ir.config_parameter'].sudo().get_param('fulfillment.webhook_sync_token')
        if expected:
            got = (request.httprequest.headers.get('X-Fulfillment-Sync-Token') or '').strip()
            if got != expected:
                _logger.warning("[Fulfillment webhook/sync] invalid or missing sync token")
                return _json({'status': 'forbidden'}, status=403)

        profile = request.env['fulfillment.profile'].sudo().search([], limit=1)
        if not profile or not profile.allow_auto_import:
            _logger.debug("[Fulfillment webhook/sync] ignored (allow_auto_import off)")
            return _json({'status': 'ignored', 'reason': 'auto_import_disabled'})

        dbname = request.env.cr.dbname
        _logger.info(
            "[Fulfillment webhook/sync] queued import_all event=%s resource=%s",
            data.get('event'),
            data.get('resource'),
        )
        threading.Thread(
            target=_run_fulfillment_import_all,
            args=(dbname,),
            daemon=True,
        ).start()
        return _json({'status': 'accepted', 'resource': data.get('resource')})
