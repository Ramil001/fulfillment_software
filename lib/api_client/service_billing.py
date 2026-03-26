import logging

_logger = logging.getLogger(__name__)


class ServiceBillingAPI:
    def __init__(self, client):
        self.client = client

    def _base(self):
        return f"https://{self.client.api_domain}/api/v1/service-billing"

    def list_prices(self, fulfillment_id):
        return self.client._request(
            "GET",
            f"{self._base()}/prices",
            params={"fulfillment_id": fulfillment_id},
        )

    def create_price(self, payload):
        return self.client._request("POST", f"{self._base()}/prices", payload=payload)

    def delete_price(self, price_id, fulfillment_id):
        return self.client._request(
            "DELETE",
            f"{self._base()}/prices/{price_id}",
            params={"fulfillment_id": fulfillment_id},
        )

    def list_usages(self, fulfillment_id, role, limit=200):
        return self.client._request(
            "GET",
            f"{self._base()}/usages",
            params={
                "fulfillment_id": fulfillment_id,
                "role": role,
                "limit": limit,
            },
        )

    def create_usage(self, payload):
        return self.client._request("POST", f"{self._base()}/usages", payload=payload)

    def summary(self, fulfillment_id):
        return self.client._request(
            "GET",
            f"{self._base()}/summary",
            params={"fulfillment_id": fulfillment_id},
        )
