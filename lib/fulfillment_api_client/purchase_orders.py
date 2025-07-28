class PurchaseOrdersAPI:
    def __init__(self, client: FulfillmentAPIClient):
        self.client = client

    def get_purchase_orders(self):
        url = f"https://{self.client.domain}/api/v1/purchase/{self.client.profile_id}"
        return self.client._request('GET', url)

    def create_purchase_order(self, payload, warehouse_id=None):
        wh_id = warehouse_id or self.client.profile_id
        url = f"https://{self.client.domain}/api/v1/purchase/{wh_id}"
        return self.client._request('POST', url, payload)
