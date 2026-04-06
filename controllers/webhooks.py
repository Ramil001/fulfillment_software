# -*- coding: utf-8 -*-
import logging
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _update_transfer_delivery_status(env, transfer_id, status=None, profile=None):
    """Update fulfillment_delivery_status on a local picking when the API reports a status change."""
    picking = env['stock.picking'].sudo().search(
        [('fulfillment_transfer_id', '=', transfer_id)], limit=1
    )
    if not picking:
        _logger.info("[webhook] No local picking found for transfer_id=%s", transfer_id)
        return

    if status is None and profile:
        try:
            from ..lib.api_client import FulfillmentAPIClient
            client = FulfillmentAPIClient(profile)
            response = client.transfer.get(transfer_id)
            status = (response.get('data') or {}).get('status')
        except Exception as e:
            _logger.warning("[webhook] Could not fetch transfer status from API: %s", e)
            return

    if status == 'done' and picking.fulfillment_delivery_status == 'delivering':
        picking.with_context(skip_fulfillment_push=True).write(
            {'fulfillment_delivery_status': 'delivered'}
        )
        _logger.info("[webhook] Transfer %s marked as delivered", transfer_id)
    elif status == 'cancel' and picking.fulfillment_delivery_status == 'delivering':
        picking.with_context(skip_fulfillment_push=True).write(
            {'fulfillment_delivery_status': False}
        )
        _logger.info("[webhook] Transfer %s delivery cancelled", transfer_id)


class FulfillmentWebhookController(http.Controller):

    @http.route('/fulfillment/webhook/message', type='json', auth='public', methods=['POST'], csrf=False)
    def message_webhook(self, **kwargs):
        data = request.get_json_data() or {}
        _logger.info("[webhook/message] received: %s", data)
        return {"status": "ok"}

    @http.route('/fulfillment/webhook/sync', type='json', auth='public', methods=['POST'], csrf=False)
    def sync_webhook(self, **kwargs):
        """General sync webhook — handles transfer status updates and optional auto-import."""
        data = request.get_json_data() or {}
        resource = data.get('resource')
        event = data.get('event', '')
        _logger.info("[webhook/sync] resource=%s event=%s", resource, event)

        env = request.env
        profile = env['fulfillment.profile'].sudo().search([], limit=1)

        if resource == 'transfer':
            transfer_id = (
                data.get('transfer_id')
                or data.get('id')
                or (data.get('data') or {}).get('id')
            )
            status = (data.get('data') or {}).get('status') or data.get('status')
            if transfer_id:
                _update_transfer_delivery_status(env, str(transfer_id), status=status, profile=profile)

            if not profile or not profile.allow_auto_import:
                return {"status": "ok", "auto_import": "disabled"}

            try:
                fulfillment_id = data.get('fulfillment_id')
                env['stock.picking'].sudo().with_context(skip_fulfillment_push=True).import_transfers(
                    fulfillment_id=fulfillment_id
                )
            except Exception as e:
                _logger.error("[webhook/sync] import_transfers failed: %s", e)

        return {"status": "ok"}

    @http.route('/fulfillment/webhook/transfer/status', type='json', auth='public', methods=['POST'], csrf=False)
    def transfer_status_webhook(self, **kwargs):
        """Dedicated webhook for transfer receipt confirmation.

        Always runs regardless of allow_auto_import so delivery status
        tracking works even when auto-import is disabled.
        """
        data = request.get_json_data() or {}
        transfer_id = (
            data.get('transfer_id')
            or data.get('id')
            or (data.get('data') or {}).get('id')
        )
        status = (data.get('data') or {}).get('status') or data.get('status')
        _logger.info("[webhook/transfer/status] transfer_id=%s status=%s", transfer_id, status)

        if not transfer_id:
            return {"status": "error", "reason": "missing transfer_id"}

        env = request.env
        profile = env['fulfillment.profile'].sudo().search([], limit=1)
        _update_transfer_delivery_status(env, str(transfer_id), status=status, profile=profile)
        return {"status": "ok"}

    @http.route('/fulfillment/webhook/warehouse', type='json', auth='public', methods=['POST'], csrf=False)
    def warehouse_webhook(self, **kwargs):
        """Webhook for warehouse create/update events from the fulfillment API.

        Expected payload:
            {
                "event": "warehouse.created" | "warehouse.updated",
                "fulfillment_id": "<uuid of the fulfillment partner>",
                "warehouse_id": "<uuid of the warehouse in the API>"
            }

        Triggers import_warehouses for the matching fulfillment partner, which
        also auto-registers any local own warehouses that are not yet in the API.
        """
        data = request.get_json_data() or {}
        event = data.get("event", "")
        fulfillment_id = (
            data.get("fulfillment_id")
            or (data.get("warehouse") or {}).get("fulfillment_id")
        )
        _logger.info("[webhook/warehouse] event=%s fulfillment_id=%s", event, fulfillment_id)

        if event not in ("warehouse.created", "warehouse.updated"):
            return {"status": "ignored", "reason": "unhandled event"}

        if not fulfillment_id:
            return {"status": "error", "reason": "missing fulfillment_id"}

        env = request.env
        partner = env["fulfillment.partners"].sudo().search(
            [("fulfillment_id", "=", fulfillment_id)], limit=1
        )
        if not partner:
            _logger.warning("[webhook/warehouse] No fulfillment.partners for id=%s", fulfillment_id)
            return {"status": "error", "reason": "fulfillment partner not found"}

        try:
            env["stock.warehouse"].sudo().with_context(skip_api_sync=True).import_warehouses(partner)
            _logger.info("[webhook/warehouse] import_warehouses done for %s", partner.name)
        except Exception as e:
            _logger.exception("[webhook/warehouse] import_warehouses failed: %s", e)
            return {"status": "error", "reason": str(e)}

        return {"status": "ok", "event": event}
