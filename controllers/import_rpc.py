from odoo import http
from odoo.http import request

class ImportFulfillmentController(http.Controller):
    @http.route("/fulfillment/run_import_all", type="json", auth="user")
    def run_import_all(self):
        request.env["res.partner"].sudo().button_run_import_all()
        return {"status": "ok"}