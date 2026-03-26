import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class FulfillmentWebhookController(http.Controller):

    @http.route('/fulfillment/status', type='http', auth='public')
    def status(self):
        return request.make_response(
            '{"status": "ok"}',
            headers=[('Content-Type', 'application/json')]
        )

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
