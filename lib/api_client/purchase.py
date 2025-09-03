import logging

_logger = logging.getLogger(__name__)


class PurchaseAPI:
    """ API для работы с закупками """
    
    def __init__(self, client):
        self.client = client

    def get(self):
        """
        Безопасный вызов GET /purchases.
        Если API вернул ошибку, логируем и возвращаем None.
        """
        url = f"https://{self.client.domain}/api/v1/purchases/{self.client.profile_id}"
        try:
            return self.client._request('GET', url)
        except Exception as e:
            _logger.error(f"[PurchaseAPI] Не удалось получить закупку {self.client.profile_id}: {e}")
            return None

    def create(self, payload, warehouse_id=None):
        """
        Безопасный вызов POST /purchases.
        """
        fulfillment_warehouse_id = warehouse_id
        url = f"https://{self.client.domain}/api/v1/purchases/{fulfillment_warehouse_id}"
        try:
            return self.client._request('POST', url, payload)
        except Exception as e:
            _logger.error(f"[PurchaseAPI] Не удалось создать закупку {fulfillment_warehouse_id}: {e}")
            return None
