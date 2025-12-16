import json
from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)

class FulfillmentWebhookController(http.Controller):

    
    @http.route('/fulfillment/status', type="http", auth="public")
    def status(self):
        return request.make_response(
            '{"status": "ok"}',
            headers=[('Content-Type', 'application/json')]
        )
        