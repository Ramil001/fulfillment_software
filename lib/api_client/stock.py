import logging

_logger = logging.getLogger(__name__)

class StockAPI:
    def __init__(self, client):
        self.client = client

    def list(self, filters=None):
        url = f"https://{self.client.api_domain}/api/v1/stocks/availability"
        payload = {
            "filters": filters or {},
            "include_reserved": True
        }
        
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def get(self, payload: dict):
        url = f"https://{self.client.api_domain}/api/v1/stocks/availability"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def create(self, payload: dict):
        url = f"https://{self.client.api_domain}/api/v1/stocks"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)
    
    def update(self, stock_id: str, payload: dict):
        url = f"https://{self.client.api_domain}/api/v1/stocks/{stock_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        return self.client._request("PATCH", url, payload)

    def delete(self, stock_id: str):
        url = f"https://{self.client.api_domain}/api/v1/stocks/{stock_id}"
        _logger.debug(f"DELETE {url}")
        return self.client._request("DELETE", url)