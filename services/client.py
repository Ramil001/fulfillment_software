import requests
import logging

_logger = logging.getLogger(__name__)

class FulfillmentAPIClient:

    def __init__(self, profile):
        self.api_key = profile.fulfillment_api_key
        self.domain = profile.domain
        self.profile_id = profile.fulfillment_profile_id

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'X-Fulfillment-API-Key': self.api_key
        }

    def create_warehouse(self, fulfillment_id, payload):
        url = f"https://{self.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses"
        return self._request('POST', url, payload)

    def update_warehouse(self, fulfillment_id, warehouse_id, payload):
        url = f"https://{self.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses/{warehouse_id}"
        return self._request('PATCH', url, payload)

    def get_warehouses(self):
        url = f"https://{self.domain}/api/v1/fulfillments/{self.profile_id}/warehouses"
        return self._request('GET', url)


    def get_purchase_orders(self):
        url = f"https://{self.domain}/api/v1/purchase/{self.profile_id}"
        return self._request('GET', url)
    
    
    def create_purchase_order(self, payload, fulfillment_id=None):
        fid = fulfillment_id
        url = f"https://self.domain/api/v1/purchase/{fid}"
        return self._request('POST', url, payload)

    def _request(self, method, url, payload=None):
        try:
            _logger.info(f"[Fulfillment API] {method} {url} payload={payload}")
            if method == 'GET':
                response = requests.get(url, headers=self._headers(), timeout=10)
            elif method == 'POST':
                response = requests.post(url, json=payload, headers=self._headers(), timeout=10)
            elif method == 'PATCH':
                response = requests.patch(url, json=payload, headers=self._headers(), timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            _logger.error(f"[Fulfillment API] {method} {url} failed: {e}")
            raise


