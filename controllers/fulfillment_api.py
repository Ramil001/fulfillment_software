from odoo import http
from odoo.http import request
import requests
import json

class FulfillmentAPIController(http.Controller):
    @http.route('/fulfillments', type='json', auth='user')
    def get_fulfillments(self, **kwargs):
        API_URL = "https://api.fulfillment.software/api/v1/fulfillments?page=1&limit=100"
        API_KEY = "e2vlLo1LM6zFBOnv95jCyZ0jlIib04acYLLL1rXmhlQ"
        
        headers = {"X-Fulfillment-API-Key": API_KEY}
        
        try:
            response = requests.get(API_URL, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Очистка предыдущих данных
            request.env['fulfillment.list'].search([]).unlink()
            
            # Запись новых данных
            for item in data.get('data', []):
                request.env['fulfillment.list'].create({
                    'fulfillment_id': item.get('fulfillmentId'),
                    'name': item.get('name'),
                    'domain': item.get('domain'),
                    'web_hook_url': item.get('webHookUrl'),
                    'created_at': item.get('createdAt'),
                    'user_id': item.get('userId'),
                })
            return True
        except Exception as e:
            return {'error': str(e)}