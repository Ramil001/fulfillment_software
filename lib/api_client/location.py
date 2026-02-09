import logging

_logger = logging.getLogger(__name__)

class LocationAPI:
    """API для работы с локациями Fulfillment сервиса"""

    def __init__(self, client):
        self.client = client

    def create(self, payload: dict):
        url = f"https://{self.client.api_domain}/api/v1/locations"
        _logger.debug(f"POST {url} payload={payload}")
        response = self.client._request("POST", url, payload)
        _logger.debug(f"[Location][Create] Response: {response}")
        return response

    def update(self, location_id, payload: dict):
        """Обновить данные существующей локации"""
        url = f"https://{self.client.api_domain}/api/v1/locations/{location_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        response = self.client._request("PATCH", url, payload)
        _logger.debug(f"Response: {response}")
        return response

    def getProductsByLocationId(self, location_id):
        """_summary_ Get products in location

        Args:
            location_id (_type_): _description_
        """
        url = f"https://{self.client.api_domain}/api/v1/locations/{location_id}/products"
        response =self.client._request("GET", url)
        return response