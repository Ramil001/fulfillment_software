import logging

_logger = logging.getLogger(__name__)


class ServiceCatalogAPI:
    def __init__(self, client):
        self.client = client

    def _base(self):
        return f"https://{self.client.api_domain}/api/v1/service-catalog"

    def get(self, fulfillment_id):
        return self.client._request("GET", f"{self._base()}/{fulfillment_id}")

    def upsert(self, fulfillment_id, data):
        return self.client._request("PUT", f"{self._base()}/{fulfillment_id}", payload=data)
