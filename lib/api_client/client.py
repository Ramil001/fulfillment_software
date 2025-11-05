import requests
import logging
from .warehouse import WarehouseAPI
from .purchase import PurchaseAPI
from .transfer import TransferAPI
from .product import ProductAPI
from .fulfillment import FulfillmentAPI
from .location import LocationAPI
from .stock import StockAPI
from .order import OrderAPI


_logger = logging.getLogger(__name__)

class FulfillmentAPIError(Exception):
    """Custom exception for Fulfillment API errors."""
    pass


class FulfillmentAPIClient:
    """
    Main client for interacting with Fulfillment API.
    Initializes endpoint modules like warehouse and purchase.
    """

    def __init__(self, profile):
        
        self.api_key = profile.fulfillment_api_key
        self.api_domain = profile.api_domain
        self.profile_id = profile.fulfillment_profile_id

        self.warehouse = WarehouseAPI(self)
        self.purchase = PurchaseAPI(self)
        self.transfer = TransferAPI(self)
        self.product = ProductAPI(self)
        self.fulfillment = FulfillmentAPI(self)
        self.location = LocationAPI(self)
        self.stock = StockAPI(self)
        self.order = OrderAPI(self)

        _logger.info(f"[FULFILLMENT] Client initialized for api_domain: {self.api_domain}")

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'X-Fulfillment-API-Key': self.api_key
        }

    def _request(self, method, url, payload=None, params=None):
        try:
            _logger.info(f"[Fulfillment API] {method} {url} | Payload: {payload} | Params: {params}")
            if method == 'GET':
                response = requests.get(url, headers=self._headers(), params=params, timeout=10)
            elif method == 'POST':
                response = requests.post(url, json=payload, headers=self._headers(), timeout=10)
            elif method == 'PATCH':
                response = requests.patch(url, json=payload, headers=self._headers(), timeout=10)
            elif method == 'PUT':
                response = requests.put(url, json=payload, headers=self._headers(), timeout=10)
            elif method == 'DELETE':
                response = requests.delete(url, headers=self._headers(), timeout=10)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            _logger.debug(f"[Fulfillment API] Response [{response.status_code}]: {response.text}")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            _logger.error(f"[Fulfillment API] {method} {url} failed: {e}")
            raise FulfillmentAPIError(f"{method} {url} failed: {str(e)}")

