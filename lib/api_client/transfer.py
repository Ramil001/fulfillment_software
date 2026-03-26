import logging

_logger = logging.getLogger(__name__)


class TransferAPI:
    def __init__(self, client):
        self.client = client

    def list(self, page=None, limit=None, fulfillment_id=None, next_page_token=None):
        """Список трансферов с курсорной пагинацией (next_page_token) и фолбэком на page."""
        url = f"https://{self.client.api_domain}/api/v1/transfers"
        params = {}
        if fulfillment_id:
            params['fulfillment_id'] = fulfillment_id
        if limit is not None:
            params['limit'] = limit
        if next_page_token:
            params['next_page_token'] = next_page_token
        elif page is not None:
            params['page'] = page

        response = self.client._request('GET', url, params=params)
        _logger.info('[TransferAPI][list] response=%s', response)
        return response

    def create(self, payload: dict):
        """Создать новый трансфер."""
        url = f"https://{self.client.api_domain}/api/v1/transfers"
        _logger.debug('POST %s payload=%s', url, payload)
        return self.client._request('POST', url, payload)

    def get(self, transfer_id: str):
        """Получить детали трансфера."""
        url = f"https://{self.client.api_domain}/api/v1/transfers/{transfer_id}"
        _logger.debug('GET %s', url)
        return self.client._request('GET', url)

    def update(self, transfer_id: str, payload: dict):
        """Обновить трансфер (например, статус или комментарий)."""
        url = f"https://{self.client.api_domain}/api/v1/transfers/{transfer_id}"
        _logger.debug('PATCH %s payload=%s', url, payload)
        return self.client._request('PATCH', url, payload)

    def delete(self, transfer_id: str):
        """Отменить трансфер."""
        url = f"https://{self.client.api_domain}/api/v1/transfers/{transfer_id}"
        _logger.info('DELETE %s', url)
        return self.client._request('DELETE', url)
