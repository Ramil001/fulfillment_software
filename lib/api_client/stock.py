import logging

_logger = logging.getLogger(__name__)

class StockAPI:
    def __init__(self, client):
        self.client = client

    def get(self, payload: dict):
        """Создать новый фулфилмент"""
        url = f"https://{self.client.api_domain}/api/v1/stock/availability"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)


    def create(self, payload: dict):
        url = f"https://{self.client.api_domain}/api/v1/stock"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)
    
    
    def update(self, stock_id: str, payload: dict):
        url = f"https://{self.client.api_domain}/api/v1/stock/{stock_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        return self.client._request("PATCH", url, payload)

    def delete(self, stock_id: str):
        url = f"https://{self.client.api_domain}/api/v1/stock/{stock_id}"
        _logger.debug(f"DELETE {url}")
        return self.client._request("DELETE", url)