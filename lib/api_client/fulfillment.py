import logging

_logger = logging.getLogger(__name__)

class FulfillmentAPI:
    def __init__(self, client):
        self.client = client

    def list(self, filters: dict = None):
        """Получить список фулфилментов (пагинация: page, limit)"""
        url = f"https://{self.client.domain}/api/v1/fulfillments"
        _logger.debug(f"GET {url} params={filters}")
        return self.client._request("GET", url, params=filters)

    def get(self, fulfillment_id: str):
        """Получить детали фулфилмента"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}"
        _logger.debug(f"GET {url}")
        return self.client._request("GET", url)

    def create(self, payload: dict):
        """Создать новый фулфилмент"""
        url = f"https://{self.client.domain}/api/v1/fulfillments"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def update(self, fulfillment_id: str, payload: dict):
        """Обновить данные фулфилмента"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        return self.client._request("PATCH", url, payload)

    def delete(self, fulfillment_id: str):
        """Удалить фулфилмент"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}"
        _logger.info(f"DELETE {url}")
        return self.client._request("DELETE", url)

    # --- Warehouses ---
    def list_warehouses(self, fulfillment_id: str):
        """Получить склады для фулфилмента"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses"
        _logger.debug(f"GET {url}")
        return self.client._request("GET", url)

    def create_warehouse(self, fulfillment_id: str, payload: dict):
        """Создать склад для фулфилмента"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def update_warehouse(self, fulfillment_id: str, warehouse_id: str, payload: dict):
        """Обновить склад"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses/{warehouse_id}"
        _logger.debug(f"PATCH {url} payload={payload}")
        return self.client._request("PATCH", url, payload)

    # --- Webhooks ---
    def set_webhook(self, fulfillment_id: str, url_param: str):
        """Задать вебхук"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/webhook"
        _logger.debug(f"POST {url} params={{'url': {url_param}}}")
        return self.client._request("POST", url, params={"url": url_param})

    def delete_webhook(self, fulfillment_id: str):
        """Удалить вебхук"""
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/webhook"
        _logger.info(f"DELETE {url}")
        return self.client._request("DELETE", url)

    def get_transfers_by_fulfillment(self, fulfillment_id, params=None):
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/transfers"
        return self.client._request("GET", url, params=params)
