import logging

_logger = logging.getLogger(__name__)

class LocationAPI:
    def __init__(self, client):
        self.client = client


    def create(self, payload: dict):
        """Создать новый фулфилмент"""
        url = f"https://{self.client.api_domain}/api/v1/locations"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)
