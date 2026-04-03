import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class FulfillmentWebhookController(http.Controller):

    @http.route(
        '/fulfillment/webhook/message',
        type='json',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def message_webhook(self, **kwargs):
        """
        Instant push from the Fulfillment API when a partner sends a message.
        Payload (sent directly by messageService.pushWebhook):
            {
                "id":                    "<uuid>",
                "sender_fulfillment_id": "<uuid>",
                "sender_name":           "Partner Name",
                "content":               "message text",
                "ref_type":              "transfer" | null,
                "ref_id":                "<uuid>" | null,
                "created_at":            "2026-…",
            }
        Returns 200 always so the API does not retry.
        """
        data = request.get_json_data() or {}
        ext_id     = data.get('id')
        sender_fid = data.get('sender_fulfillment_id')
        content    = (data.get('content') or '').strip()
        ref_type   = (data.get('ref_type') or '').strip()
        ref_id     = (data.get('ref_id') or '').strip()

        _logger.info(
            '[Webhook][message] from=%s ref=%s/%s id=%s',
            sender_fid, ref_type, ref_id, ext_id,
        )

        if not sender_fid or not content:
            _logger.warning('[Webhook][message] Missing required fields, skipping')
            return {'status': 'error', 'reason': 'missing fields'}

        env = request.env

        # Deduplication — only block if we already SUCCESSFULLY posted the message
        if ext_id:
            existing = env['fulfillment.message'].sudo().search(
                [('external_id', '=', ext_id)], limit=1
            )
            if existing:
                _logger.info('[Webhook][message] Duplicate id=%s, skipping', ext_id)
                return {'status': 'ok', 'reason': 'duplicate'}

        partner = env['fulfillment.partners'].sudo().search(
            [('fulfillment_id', '=', sender_fid)], limit=1
        )
        author_id = partner.partner_id.id if partner and partner.partner_id else False

        internal_users = env['res.users'].sudo().search([
            ('share', '=', False),
            ('active', '=', True),
        ])

        posted_to_picking = False
        message = None
        picking_rec = env['stock.picking']

        # ── Transfer chatter ──────────────────────────────────────────────────
        if ref_type == 'transfer' and ref_id:
            picking_rec = env['stock.picking'].sudo().search(
                [('fulfillment_transfer_id', '=', ref_id)], limit=1
            )

            # On-demand import: transfer not yet in local DB → fetch from API now
            if not picking_rec:
                _logger.info(
                    '[Webhook][message] Transfer %s not found locally, '
                    'triggering on-demand import', ref_id,
                )
                try:
                    _on_demand_import_transfer(env, ref_id)
                    picking_rec = env['stock.picking'].sudo().search(
                        [('fulfillment_transfer_id', '=', ref_id)], limit=1
                    )
                    if picking_rec:
                        _logger.info(
                            '[Webhook][message] On-demand import succeeded: %s',
                            picking_rec.name,
                        )
                    else:
                        _logger.warning(
                            '[Webhook][message] Transfer %s still not found '
                            'after on-demand import', ref_id,
                        )
                except Exception as exc:
                    _logger.warning(
                        '[Webhook][message] On-demand import failed: %s', exc
                    )

            if picking_rec:
                try:
                    message = picking_rec.with_context(
                        from_fulfillment_api=True,
                        mail_notify_force_send=False,
                    ).message_post(
                        body=content,
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                        author_id=author_id,
                    )
                    posted_to_picking = True
                    _logger.info(
                        '[Webhook][message] Posted to picking %s', picking_rec.name
                    )
                    _send_inbox_notifications(
                        picking_rec, message, internal_users, author_id
                    )
                    # Always send our custom bus event so the JS can refresh the
                    # chatter in real time even if Odoo's own notification only
                    # updates the top-bar Discuss icon.
                    _bus_refresh_chatter(
                        picking_rec, message, internal_users, author_id,
                        extra={'picking_id': picking_rec.id,
                               'partner_name': data.get('sender_name', 'Fulfillment')},
                    )
                except Exception as exc:
                    _logger.error(
                        '[Webhook][message] message_post failed on %s: %s',
                        picking_rec.name, exc,
                    )

        # ── Partner chatter fallback ──────────────────────────────────────────
        if not posted_to_picking and partner:
            try:
                message = partner.with_context(
                    from_fulfillment_api=True,
                    mail_notify_force_send=False,
                ).message_post(
                    body=content,
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                    author_id=author_id,
                )
                _logger.info(
                    '[Webhook][message] Posted to partner chatter %s', partner.name
                )
                _send_inbox_notifications(partner, message, internal_users, author_id)
                _bus_refresh_chatter(
                    partner, message, internal_users, author_id,
                    extra={'partner_id': partner.id,
                           'partner_name': data.get('sender_name', 'Fulfillment')},
                )
            except Exception as exc:
                _logger.error(
                    '[Webhook][message] message_post failed on partner %s: %s',
                    partner.name if partner else '?', exc,
                )

        # ── Deduplication record — ONLY when message was actually posted ──────
        if ext_id and message:
            try:
                track_vals = {
                    'partner_id': partner.id if partner else False,
                    'external_id': ext_id,
                    'direction': 'in',
                }
                if posted_to_picking and picking_rec:
                    track_vals['picking_id'] = picking_rec.id
                env['fulfillment.message'].sudo().create(track_vals)
            except Exception as exc:
                _logger.warning(
                    '[Webhook][message] Dedup record creation failed: %s', exc
                )

        if not message:
            _logger.warning(
                '[Webhook][message] Message NOT posted — transfer=%s partner=%s',
                ref_id, sender_fid,
            )

        return {'status': 'ok'}

    @http.route('/fulfillment/status', type='http', auth='public')
    def status(self):
        return request.make_response(
            '{"status": "ok"}',
            headers=[('Content-Type', 'application/json')]
        )

    @http.route(
        '/fulfillment/webhook/transfer/status',
        type='json',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def transfer_status_webhook(self, **kwargs):
        """
        Dedicated webhook for transfer status changes from the Fulfillment API.
        Called when the fulfillment center validates receipt of goods (status → done).

        This route always runs regardless of the allow_auto_import setting —
        delivery status tracking is a core feature, not an optional import.

        Payload schema:
            {
                "transfer_id": "<uuid>",
                "status":      "done" | "draft" | "confirmed" | "assigned" | "cancel",
                "fulfillment_id": "<uuid>",   # optional: which side triggered it
            }
        """
        data = request.get_json_data() or {}
        transfer_id = data.get('transfer_id')
        status = data.get('status')

        _logger.info(
            '[Webhook][transfer/status] transfer_id=%s status=%s',
            transfer_id, status,
        )

        if not transfer_id or not status:
            return {'status': 'error', 'reason': 'missing transfer_id or status'}

        env = request.env
        profile = env['fulfillment.profile'].sudo().search([], limit=1)
        if not profile:
            _logger.warning('[Webhook][transfer/status] No fulfillment profile configured')
            return {'status': 'error', 'reason': 'no_profile'}

        try:
            _update_transfer_delivery_status(env, transfer_id, status)
        except Exception:
            _logger.exception('[Webhook][transfer/status] Handler raised for %s', transfer_id)

        return {'status': 'ok'}

    @http.route(
        '/fulfillment/webhook/sync',
        type='json',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def sync_webhook(self, **kwargs):
        """
        Receives push notifications from the Fulfillment API whenever any
        resource is created or updated.

        Payload schema:
            {
                "event":          "sync",
                "fulfillment_id": "<uuid>",   # which Fulfillment account triggered it
                "resource":       "transfer" | "order" | "product" | "stock",

                # resource-specific IDs (one of):
                "transfer_id":    "<uuid>",
                "order_id":       "<uuid>",
                "product_id":     "<uuid>",
                "stock_id":       "<uuid>",
                "warehouse_id":   "<uuid>",   # present for stock
            }

        Returns 200 regardless of import outcome so the API does not retry.
        All failures are captured in logs.
        """
        data = request.get_json_data() or {}
        event = data.get('event')
        resource = data.get('resource')

        _logger.info(
            '[Webhook] sync event: resource=%s ids=%s',
            resource,
            {k: v for k, v in data.items() if k.endswith('_id')},
        )

        if event != 'sync':
            return {'status': 'ignored', 'reason': 'unknown_event'}

        env = request.env
        profile = env['fulfillment.profile'].sudo().search([], limit=1)

        if not profile:
            _logger.warning('[Webhook] No fulfillment profile configured')
            return {'status': 'error', 'reason': 'no_profile'}

        # Transfer delivery status is always updated, even if auto-import is off.
        # This ensures merchants always see whether fulfillment received their goods.
        if resource == 'transfer':
            transfer_id = data.get('transfer_id')
            if transfer_id:
                try:
                    _update_transfer_delivery_status(env, transfer_id, status=None, profile=profile)
                except Exception:
                    _logger.exception('[Webhook][sync] Delivery status update raised for %s', transfer_id)

        if not profile.allow_auto_import:
            return {'status': 'disabled'}

        handler = _RESOURCE_HANDLERS.get(resource)
        if handler is None:
            _logger.info('[Webhook] No handler for resource=%s — ignoring', resource)
            return {'status': 'ignored', 'reason': f'unsupported resource: {resource}'}

        try:
            handler(env, profile, data)
        except Exception:
            _logger.exception('[Webhook] Handler for resource=%s raised', resource)

        return {'status': 'ok'}


# ─────────────────────────────────────────────────────────────────────────────
# Resource handlers
# Each receives (env, profile, payload_dict) and returns nothing.
# ─────────────────────────────────────────────────────────────────────────────

def _update_transfer_delivery_status(env, transfer_id, status=None, profile=None):
    """Update fulfillment_delivery_status on a local picking when the remote
    transfer status changes.

    This runs independently of allow_auto_import so that merchants always see
    whether the fulfillment center has received their goods.

    If `status` is None the current status is fetched from the API.
    If `status` == 'done' and the local picking has fulfillment_delivery_status
    == 'delivering', it is upgraded to 'delivered'.
    """
    picking = env['stock.picking'].sudo().search(
        [('fulfillment_transfer_id', '=', transfer_id)], limit=1
    )
    if not picking:
        _logger.debug(
            '[DeliveryStatus] Local picking not found for transfer %s — skipping',
            transfer_id,
        )
        return

    if status is None:
        if not profile:
            profile = env['fulfillment.profile'].sudo().search([], limit=1)
        if not profile:
            return
        try:
            from ..lib.api_client import FulfillmentAPIClient
            client = FulfillmentAPIClient(profile)
            response = client.transfer.get(transfer_id)
            data = response.get('data') or {}
            status = data.get('status')
        except Exception as exc:
            _logger.warning(
                '[DeliveryStatus] Could not fetch transfer %s from API: %s',
                transfer_id, exc,
            )
            return

    _logger.info(
        '[DeliveryStatus] transfer=%s status=%s picking=%s delivery_status=%s',
        transfer_id, status, picking.name, picking.fulfillment_delivery_status,
    )

    if status == 'done' and picking.fulfillment_delivery_status == 'delivering':
        picking.with_context(skip_fulfillment_push=True).write(
            {'fulfillment_delivery_status': 'delivered'}
        )
        _logger.info(
            '[DeliveryStatus] Set delivered for picking %s (transfer %s)',
            picking.name, transfer_id,
        )
    elif status == 'cancel' and picking.fulfillment_delivery_status == 'delivering':
        picking.with_context(skip_fulfillment_push=True).write(
            {'fulfillment_delivery_status': False}
        )
        _logger.info(
            '[DeliveryStatus] Cleared delivery status for cancelled transfer %s',
            transfer_id,
        )


def _handle_transfer(env, profile, data):
    """Fetch the single updated transfer and import it."""
    transfer_id = data.get('transfer_id')
    if not transfer_id:
        _logger.warning('[Webhook][transfer] No transfer_id in payload')
        return

    bus = env['bus.utils'].sudo()
    bus.send_sync_status(running=True)
    try:
        from ..lib.api_client import FulfillmentAPIClient
        client = FulfillmentAPIClient(profile)
        response = client.transfer.get(transfer_id)
        transfer_data = response.get('data')
        if not transfer_data:
            _logger.warning('[Webhook][transfer] API returned no data for %s', transfer_id)
            return

        env['stock.picking'].sudo().with_context(
            skip_fulfillment_push=True
        )._import_transfer(transfer_data)
        _logger.info('[Webhook][transfer] Imported transfer %s', transfer_id)
    finally:
        bus.send_sync_status(running=False)


def _handle_order(env, profile, data):
    """
    An order was created/updated.
    Orders are pushed Odoo→API, not imported back, so we trigger a
    targeted transfer import for the fulfillment account that owns this order.
    The transfer(s) for this order will arrive via their own 'transfer' webhook;
    here we do a lightweight incremental sync to catch anything in-flight.
    """
    fulfillment_id = data.get('fulfillment_id')
    if not fulfillment_id:
        return

    from ..lib.api_client import FulfillmentAPIClient
    client = FulfillmentAPIClient(profile)
    Picking = env['stock.picking'].sudo().with_context(skip_fulfillment_push=True)

    # Fetch only the first page of recent transfers for this fulfillment account.
    # The cursor-based cron will catch any that arrive slightly later.
    try:
        response = client.transfer.list(fulfillment_id=fulfillment_id, limit=20)
        for transfer in response.get('data', []):
            try:
                Picking._import_transfer(transfer)
            except Exception:
                _logger.exception(
                    '[Webhook][order] Failed to import transfer %s', transfer.get('id')
                )
    except Exception:
        _logger.exception('[Webhook][order] Failed to list transfers for %s', fulfillment_id)


def _handle_product(env, profile, data):
    """
    A product was created or updated in the Fulfillment API.

    Products are NOT created here proactively.  They are created implicitly
    when a transfer or order that contains them is imported into this Odoo
    instance (_find_or_create_product inside _create_transfer_items).

    This handler only updates fields on products that already exist locally
    (e.g. name/SKU/barcode changed on the API side).
    """
    product_id = data.get('product_id')
    if not product_id:
        return

    ProductTmpl = env['product.template'].sudo()
    existing = ProductTmpl.search(
        [('fulfillment_product_id', '=', product_id)], limit=1
    )
    if not existing:
        _logger.debug(
            '[Webhook][product] Product %s not in Odoo yet — skipping (will be created on first transfer)',
            product_id,
        )
        return

    from ..lib.api_client import FulfillmentAPIClient
    client = FulfillmentAPIClient(profile)
    response = client.product.get(product_id)
    product_data = response.get('data')
    if not product_data:
        return

    update_vals = {}
    if product_data.get('name') and product_data['name'] != existing.name:
        update_vals['name'] = product_data['name']
    if product_data.get('sku') and product_data['sku'] != existing.default_code:
        update_vals['default_code'] = product_data['sku']
    if product_data.get('barcode') and product_data['barcode'] != existing.barcode:
        update_vals['barcode'] = product_data['barcode']

    # Sync image if the local product has none and the API has a URL
    remote_img_url = product_data.get('img_url')
    if (
        not existing.image_1920
        and remote_img_url
        and remote_img_url.startswith(('http://', 'https://'))
    ):
        image_b64 = env['stock.picking'].sudo()._fetch_image_b64(remote_img_url)
        if image_b64:
            update_vals['image_1920'] = image_b64

    if update_vals:
        existing.with_context(skip_fulfillment_push=True).write(update_vals)
        _logger.info(
            '[Webhook][product] Updated existing product %s: %s',
            existing.name, list(update_vals.keys()),
        )


def _handle_stock(env, profile, data):
    """
    Stock levels changed in the Fulfillment API.
    Re-import stock for the affected warehouse.
    """
    warehouse_id = data.get('warehouse_id')

    filters = {}
    if warehouse_id:
        filters['warehouse_ids'] = [warehouse_id]

    env['stock.quant'].sudo().import_stock(filters=filters)
    _logger.info('[Webhook][stock] Stock import triggered (warehouse=%s)', warehouse_id or 'all')


# Registry — maps resource name → handler function
_RESOURCE_HANDLERS = {
    'transfer': _handle_transfer,
    'order':    _handle_order,
    'product':  _handle_product,
    'stock':    _handle_stock,
}


def _on_demand_import_transfer(env, transfer_api_id):
    """Fetch a specific transfer from the Fulfillment API and import it locally.

    Called from the message webhook when the picking is not yet in the local
    database — this can happen when a message arrives before the periodic cron
    has had a chance to sync the transfer.
    """
    from ..lib.api_client import FulfillmentAPIClient

    profile = env['fulfillment.profile'].sudo().search([], limit=1)
    if not profile:
        _logger.warning('[Webhook][on-demand] No fulfillment profile found')
        return

    client = FulfillmentAPIClient(profile)
    response = client.transfer.get(transfer_api_id)
    transfer_data = response.get('data') if isinstance(response, dict) else None

    if not transfer_data:
        _logger.warning(
            '[Webhook][on-demand] API returned no data for transfer %s', transfer_api_id
        )
        return

    # Use the same import logic as the cron
    env['fulfillment.transfers'].sudo().with_context(
        skip_fulfillment_push=True
    )._import_transfer(transfer_data)


def _send_inbox_notifications(thread, message, internal_users, author_id):
    """Force Inbox (top-bar Discuss icon) + real-time chatter update for all
    internal users, bypassing their personal notification_type preference.

    Uses a savepoint so that any SQL failure (e.g. duplicate mail.notification
    from a race condition) rolls back only this sub-transaction and leaves the
    outer request transaction intact.
    """
    if not internal_users or not message:
        return

    # Exclude partners that already have a notification for this message
    # (created by Odoo's standard _notify_thread for existing followers)
    # to avoid unique-constraint violations.
    try:
        existing_partner_ids = set(
            thread.env['mail.notification'].sudo().search([
                ('mail_message_id', '=', message.id),
            ]).mapped('res_partner_id').ids
        )
    except Exception:
        existing_partner_ids = set()

    recipients_data = [
        {
            'id': user.partner_id.id,
            'uid': user.id,
            'notif': 'inbox',
            'type': 'user',
            'share': False,
            'active': True,
        }
        for user in internal_users
        if (
            user.partner_id
            and user.partner_id.id != (author_id or 0)
            and user.partner_id.id not in existing_partner_ids
        )
    ]

    if not recipients_data:
        return

    try:
        with thread.env.cr.savepoint():
            thread._notify_thread_by_inbox(
                message,
                recipients_data,
                msg_vals={'model': message.model, 'res_id': message.res_id},
            )
    except Exception as exc:
        _logger.warning('[Webhook][message] inbox notify failed: %s', exc)


def _bus_refresh_chatter(thread, message, internal_users, author_id, extra=None):
    """Send fulfillment_new_message bus event so open chatter views refresh.

    The JS notifications.js receives this and calls thread.fetchNewMessages()
    for the matching thread, giving real-time chatter updates without page reload.
    extra: optional dict of additional payload fields (picking_id, partner_id…)
    """
    try:
        from odoo.tools import html2plaintext
        plain = html2plaintext(message.body or '').strip()[:200]
        payload = {
            'content': plain,
            'model': message.model,
            'res_id': message.res_id,
            'message_id': message.id,
        }
        if extra:
            payload.update(extra)
        bus = thread.env['bus.bus'].sudo()
        for user in internal_users:
            if user.partner_id and user.partner_id.id != (author_id or 0):
                bus._sendone(user.partner_id, 'fulfillment_new_message', payload)
    except Exception as exc:
        _logger.warning('[Webhook][message] bus refresh failed: %s', exc)
