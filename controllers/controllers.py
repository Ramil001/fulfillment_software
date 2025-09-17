from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)


class FulfillmentWebHookAPI(http.Controller):

    # Общий принцип: у нас есть endpoint на стороне odoo который принимает данные которые нужно обновить по action push, odoo1 -> api -> odoo2
    # Тут указываю какие ресурсы доступны для обнволения через API
    VALID_RESOURCES = {"transfers", "warehouses", "purchase"}

    @http.route(
        '/fulfillment_software/api/v1/fulfillments/<string:fulfillment_id>/resource/<string:resource>/update',
        type='http', auth='public', methods=['POST'], csrf=False
    )
    
    def update_resource(self, fulfillment_id, resource, **kwargs): 
        
        try:
            data = request.httprequest.get_json(force=True, silent=True)
        except Exception:
            data = request.httprequest.data.decode("utf-8")

        if resource not in self.VALID_RESOURCES:
            return request.make_json_response(
                {"status": "error", "message": f"Invalid resource '{resource}'"},
                status=400
            )

        # Диспетчеризация по типу ресурса
        handler = getattr(self, f"_process_{resource}", None)
        if not handler:
            return request.make_json_response(
                {"status": "error", "message": f"No handler for '{resource}'"},
                status=500
            )

        try:
            result = handler(fulfillment_id, data)
        except Exception as e:
            _logger.exception("Error processing resource %s", resource)
            return request.make_json_response(
                {"status": "error", "message": str(e)},
                status=500
            )

        return request.make_json_response({
            "status": "ok",
            "fulfillment_id": fulfillment_id,
            "resource": resource,
            "result": result,
        })


    # Обработчики которые будут вызываться из models конкретного ресурса.

    def _process_transfers(self, fulfillment_id, payload):
        """Обновление перемещений (stock.picking)"""
        return "transfers handler not yet implemented"

    def _process_warehouses(self, fulfillment_id, payload):
        """Обновление складов (stock.warehouse)"""
        return "warehouses handler not yet implemented"

    def _process_purchase(self, fulfillment_id, payload):
        """Обновление закупок (purchase.order)"""
        return "purchase handler not yet implemented"
