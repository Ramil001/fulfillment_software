from odoo import http
import json


class FulfillmentWebHookAPI(http.Controller):

    @http.route('/fulfillment/sync', type='json', auth='public', methods=['GET'], csrf=False)
    def trigger_sync(self, **kwargs):
        """
        Вызов из вне: POST /fulfillment/sync
        Body: {"profile_id": 1} или пусто для всех
        """
        profile_id = kwargs.get("profile_id")
        if profile_id:
            profile = request.env['fulfillment.profile'].browse(profile_id)
            if not profile.exists():
                return {"status": "error", "message": f"Profile {profile_id} not found"}
            profile._sync_with_fulfillment_api()
        else:
            profiles = request.env['fulfillment.profile'].search([])
            for p in profiles:
                p._sync_with_fulfillment_api()

        return {"status": "ok", "message": "Синхронизация выполнена"}