from odoo import http
from odoo.http import request, Response
import json

class ImportFulfillmentController(http.Controller):

    @http.route("/fulfillment/run_import_all", type="http", auth="public")
    def run_import_all(self):
        request.env["fulfillment.partners"].sudo().button_run_import_all()
        return Response(
            json.dumps({"status": "ok"}),
            content_type="application/json",
        )
