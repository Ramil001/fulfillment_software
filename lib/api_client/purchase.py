import logging

_logger = logging.getLogger(__name__)


class PurchaseAPI:
    """ API для работы с закупками """
    
    def __init__(self, client):
        self.client = client


    def get(self):
        url = f"https://{self.client.api_domain}/api/v1/purchases/{self.client.profile_id}"
        try:
            return self.client._request('GET', url)
        except Exception as e:
            _logger.error(f"[Error][Purchase][GET]: {e}")
            return None


    def create(self, payload):
        url = f"https://{self.client.api_domain}/api/v1/purchases/"
        try:
            return self.client._request('POST', url, payload)
        except Exception as e:
            _logger.error(f"[Error][Purchase][POST]: {e}")
            return None
