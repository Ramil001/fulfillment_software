class PurchaseAPI:
    def __init__(self, client):
        self.client = client

    def get_purchase_orders(self):
        url = f"https://{self.client.domain}/api/v1/purchase/{self.client.profile_id}"
        return self.client._request('GET', url)

    def create_purchase_order(self, payload, warehouse_id=None):
        whId = warehouse_id
        url = f"https://{self.client.domain}/api/v1/purchase/{whId}"
        return self.client._request('POST', url, payload)
