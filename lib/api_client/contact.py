import logging

_logger = logging.getLogger(__name__)

class ContactAPI:
    def __init__(self, client):
        self.client = client

    # --- Base CRUD ---

    def list(self, filters: dict = None):
        """Получить список контактов (с пагинацией: page, limit)"""
        url = f"https://{self.client.api_domain}/api/v1/contacts"
        _logger.debug(f"GET {url} params={filters}")
        return self.client._request("GET", url, params=filters)

    def get(self, contact_id: str):
        """Получить контакт по ID"""
        url = f"https://{self.client.api_domain}/api/v1/contacts/{contact_id}"
        _logger.debug(f"GET {url}")
        return self.client._request("GET", url)

    def create(self, payload: dict):
        """Создать контакт"""
        url = f"https://{self.client.api_domain}/api/v1/contacts"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    def update(self, contact_id: str, payload: dict):
        """Обновить контакт"""
        url = f"https://{self.client.api_domain}/api/v1/contacts/{contact_id}"
        _logger.debug(f"PUT {url} payload={payload}")
        return self.client._request("PUT", url, payload)

    def delete(self, contact_id: str):
        """Удалить контакт"""
        url = f"https://{self.client.api_domain}/api/v1/contacts/{contact_id}"
        _logger.info(f"DELETE {url}")
        return self.client._request("DELETE", url)

    # --- Parent/Children Relations ---

    def get_children(self, contact_id: str):
        """Получить всех дочерних контактов"""
        url = f"https://{self.client.api_domain}/api/v1/contacts/{contact_id}/children"
        _logger.debug(f"GET {url}")
        return self.client._request("GET", url)

    def add_child(self, contact_id: str, payload: dict):
        """Создать дочерний контакт"""
        url = f"https://{self.client.api_domain}/api/v1/contacts/{contact_id}/children"
        _logger.debug(f"POST {url} payload={payload}")
        return self.client._request("POST", url, payload)

    # --- Linking to Orders/Transfers if needed later ---
    # Можем добавить методы позже, как только появится API

