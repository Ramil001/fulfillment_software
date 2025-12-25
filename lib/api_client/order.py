import logging
from odoo.addons.fulfillment_software import const

_logger = logging.getLogger(__name__)

class OrderAPI:
    def __init__(self, client):
        """
        client — объект, который умеет делать HTTP-запросы.
        Например, client._request(method, url, payload=None, params=None)
        """
        self.client = client

    def create(self, payload: dict):
        """
        Создать заказ с айтемами.
        Пример payload:
        {
            "items": [
                {"product_id": "abc123", "quantity": 2, "fulfillment_partner_id": "fp1"},
                {"product_id": "xyz789", "quantity": 1}
            ]
        }
        """
        url = f"https://{self.client.api_domain}/api/v1/orders"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def list(self, filters: dict = None):
        """Получить список заказов с фильтрацией и пагинацией"""
        url = f"https://{self.client.api_domain}/api/v1/orders"
        _logger.debug(f"GET {url} params={filters}")
        return self.client._request("GET", url, params=filters)

    def get(self, order_id: str):
        """Получить детали заказа по order_id"""
        url = f"https://{self.client.api_domain}/api/v1/orders/{order_id}"
        _logger.debug(f"GET {url}")
        return self.client._request("GET", url)

    def update(self, order_id: str, payload: dict):
        """Обновить заказ (например, изменить айтемы или статус)"""
        url = f"https://{self.client.api_domain}/api/v1/orders/{order_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        return self.client._request("PATCH", url, payload)

    def delete(self, order_id: str):
        """Удалить заказ"""
        url = f"https://{self.client.api_domain}/api/v1/orders/{order_id}"
        _logger.info(f"DELETE {url}")
        return self.client._request("DELETE", url)
