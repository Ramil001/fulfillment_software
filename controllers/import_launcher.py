from odoo import http
from odoo.http import request, Response
import json


class ImportFulfillmentController(http.Controller):

    @http.route(
        "/fulfillment/import/all",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def run_import_all(self):
        env = request.env

        profile = env["fulfillment.profile"].sudo().search([], limit=1)

        if not profile:
            return Response(
                json.dumps({"error": "Fulfillment profile not found"}),
                content_type="application/json",
                status=400,
            )

        if not profile.allow_auto_import:
            return Response(
                json.dumps({
                    "status": "disabled",
                    "message": "Automation import disabled for this Odoo instance",
                }),
                content_type="application/json",
                status=403,
            )

        env["fulfillment.partners"].sudo().button_run_import_all()

        return Response(
            json.dumps({"status": "ok"}),
            content_type="application/json",
        )
