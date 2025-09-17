import logging

_logger = logging.getLogger(__name__)

class ProductAPI:
    def __init__(self, client):
        """
        client - это объект, который умеет делать HTTP-запросы.
        Например, может быть обёртка с методом _request(method, url, payload=None, params=None)
        """
        self.client = client

    def create(self, payload: dict):
        """Создать новый продукт"""
        url = f"https://{self.client.api_domain}/api/v1/products"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def list(self, filters: dict = None):
        """Получить список продуктов с фильтрами и пагинацией"""
        url = f"https://{self.client.api_domain}/api/v1/products"
        _logger.debug(f"GET {url} params={filters}")
        return self.client._request("GET", url, params=filters)

    def get(self, product_id: str):
        """Получить детали продукта по ID"""
        url = f"https://{self.client.api_domain}/api/v1/products/{product_id}"
        _logger.debug(f"GET {url}")
        return self.client._request("GET", url)

    def update(self, product_id: str, payload: dict):
        """Обновить продукт по ID"""
        url = f"https://{self.client.api_domain}/api/v1/products/{product_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        return self.client._request("PATCH", url, payload)

    def delete(self, product_id: str):
        """Удалить продукт по ID"""
        url = f"https://{self.client.api_domain}/api/v1/products/{product_id}"
        _logger.info(f"DELETE {url}")
        return self.client._request("DELETE", url)
