from odoo import http
from odoo.http import request, Response
import json

class ImportFulfillmentController(http.Controller):

    @http.route(
        "/fulfillment/run_import_all",
        type="http",
        auth="public",
        methods=["POST"],   
        csrf=False         
    )
    def run_import_all(self):
        request.env["fulfillment.partners"].sudo().button_run_import_all()
        return Response(
            json.dumps({"status": "ok"}),
            content_type="application/json",
        )


    @http.route(
        "/fulfillment/import/<string:resource>",
        type="http",
        auth="public",
        methods=["POST"],   
        csrf=False         
        )
    
    def import_stock(self, resource):
        return Response(
            json.dumps({"status": "import stock", "resource": resource}),
            content_type="application/json",
        )