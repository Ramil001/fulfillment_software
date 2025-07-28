import requests
import logging
from .warehouse import WarehouseAPI
from .purchase import PurchaseAPI
_logger = logging.getLogger(__name__)

class FulfillmentAPIClient:
    def __init__(self, profile):
        self.api_key = profile.fulfillment_api_key
        self.domain = profile.domain
        self.profile_id = profile.fulfillment_profile_id

        self.warehouse = WarehouseAPI(self)
        self.purchase = PurchaseAPI(self)

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'X-Fulfillment-API-Key': self.api_key
        }

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
