from odoo import http
from odoo.http import request
import json

class FulfillmentWebHookAPI(http.Controller):

    @http.route('/fulfillment/api/v1/updates', type='http', auth='public', methods=['POST'], csrf=False)
    def trigger_sync(self, **kwargs):
        api_key = request.httprequest.headers.get("X-Fulfillment-API-Key")

        profile = request.env["fulfillment.profile"].sudo().search([
            ("fulfillment_api_key", "=", api_key)
        ], limit=1)

        if not profile:
            return http.Response(
                json.dumps({"status": "error", "message": "Invalid API Key"}),
                content_type="application/json",
                status=401
            )

        profile_id = kwargs.get("profile_id")
        partners_model = request.env['fulfillment.partners'].sudo()
        
        try:
            partners_model.sync_from_api(profile)
            return http.Response(
                json.dumps({
                    "status": "ok",
                    "message": f"Синхронизация выполнена для профиля {profile.name}, profile_id={profile_id}"
                }),
                content_type="application/json"
            )
        except Exception as e:
            return http.Response(
                json.dumps({"status": "error", "message": f"Sync failed: {str(e)}"}),
                content_type="application/json",
                status=500
            )
