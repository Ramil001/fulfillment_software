from odoo.addons.bus.models.bus import dispatch
from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class FulfillmentWebHookAPI(http.Controller):
    VALID_RESOURCES = {"transfers", "warehouses", "purchase"}

    @http.route(
        '/fulfillment_software/api/v1/fulfillments/<string:fulfillment_id>/resource/<string:resource>/update',
        type='json', auth='public', methods=['POST'], csrf=False
    )
    def update_resource(self, fulfillment_id, resource, **kwargs):
        data = request.httprequest.get_json(force=True, silent=True) or {}
        if resource not in self.VALID_RESOURCES:
            return {"status": "error", "message": f"Invalid resource '{resource}'"}

        # отправляем push в UI всем администраторам
        self._send_push(f"Получено обновление ресурса: {resource}", "info")

        handler = getattr(self, f"_process_{resource}", None)
        if not handler:
            return {"status": "error", "message": f"No handler for '{resource}'"}

        try:
            result = handler(fulfillment_id, data)
        except Exception as e:
            _logger.exception("Error processing resource %s", resource)
            self._send_push(f"Ошибка при обновлении ресурса: {e}", "danger")
            return {"status": "error", "message": str(e)}

        self._send_push(f"✅ Ресурс '{resource}' успешно обновлён", "success")

        return {"status": "ok", "result": result}

    def _send_push(self, message, level="info"):
        """Отправка уведомления всем активным пользователям"""
        dispatch(
            request.env.cr.dbname,
            "fulfillment_notify_channel",
            {
                "message": message,
                "type": level,
            }
        )

    def _process_transfers(self, fulfillment_id, payload):
        return "transfers handler not yet implemented"

    def _process_warehouses(self, fulfillment_id, payload):
        return "warehouses handler not yet implemented"

    def _process_purchase(self, fulfillment_id, payload):
        return "purchase handler not yet implemented"
