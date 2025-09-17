import logging

_logger = logging.getLogger(__name__)

class TransferAPI:
    def __init__(self, client):
        self.client = client

    def create(self, payload: dict):
        """Создать новый трансфер"""
        url = f"https://{self.client.api_domain}/api/v1/transfers"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def list(self, filters: dict = None):
        """Получить список трансферов (с фильтрами warehouse_id, status, page)"""
        url = f"https://{self.client.api_domain}/api/v1/transfers"
        _logger.debug(f"GET {url} params={filters}")
        return self.client._request("GET", url, params=filters)

    def get(self, transfer_id: str):
        """Получить детали трансфера"""
        url = f"https://{self.client.api_domain}/api/v1/transfers/{transfer_id}"
        _logger.debug(f"GET {url}")
        return self.client._request("GET", url)

    def update(self, transfer_id: str, payload: dict):
        """Обновить трансфер (например, статус или комментарий)"""
        url = f"https://{self.client.api_domain}/api/v1/transfers/{transfer_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        return self.client._request("PATCH", url, payload)

    def delete(self, transfer_id: str):
        """Отменить трансфер"""
        url = f"https://{self.client.api_domain}/api/v1/transfers/{transfer_id}"
        _logger.info(f"DELETE {url}")
        return self.client._request("DELETE", url)
