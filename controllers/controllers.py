from odoo import http
from odoo.http import request
import json

class FulfillmentWebHookAPI(http.Controller):

    # Route который принимает параметры для вызова обновления с fulfillment API.
    @http.route(
        'fulfillment_software/api/v1/fulfillments/<string:fulfillment_id>/resource/<string:resource>/update',
        type='http', auth='public', methods=['POST'], csrf=False
    )
    
    def trigger_sync(self, fulfillment_id, resource, **kwargs):
        try:
            profile = request.env["fulfillment.profile"].sudo().search([
                ("fulfillment_id", "=", fulfillment_id)
            ], limit=1)

            if not profile:
                return http.Response(
                    json.dumps({"status": "error", "message": "Invalid fulfillment_id"}),
                    content_type="application/json",
                    status=401
                )

            data = request.jsonrequest or {}

            if resource == "transfer":
                # вызов метода синхронизации трансферов
                request.env['inventory.transfer'].sudo().sync_from_api(profile, data)
            elif resource == "warehouse":
                request.env['warehouse'].sudo().sync_from_api(profile, data)
            else:
                return http.Response(
                    json.dumps({"status": "error", "message": f"Unknown resource '{resource}'"}),
                    content_type="application/json",
                    status=400
                )

            return http.Response(
                json.dumps({
                    "status": "ok",
                    "message": f"Синхронизация выполнена ({resource}) для профиля {profile.name}",
                    "data": data
                }),
                content_type="application/json"
            )

        except Exception as e:
            return http.Response(
                json.dumps({"status": "error", "message": f"Sync failed: {str(e)}"}),
                content_type="application/json",
                status=500
            )
