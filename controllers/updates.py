import json
from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class FulfillmentUpdateController(http.Controller):

    @http.route('/fulfillment/updates', type='json', auth='public', methods=['POST'], csrf=False)
    def set_updates(self, **kwargs):
        data = request.get_json_data() or {}
        return {"status": "ok", "sent": data}
    
    
    @http.route('/fulfillment/status', type="http", auth="public")
    def status(self):
        return request.make_response(
            '{"status": "ok"}',
            headers=[('Content-Type', 'application/json')]
        )
        