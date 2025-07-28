class PurchaseAPI:
    """ Purchase  """
    
    def __init__(self, client):
        self.client = client

    def get(self):
        url = f"https://{self.client.domain}/api/v1/purchase/{self.client.profile_id}"
        return self.client._request('GET', url)

    def create(self, payload, warehouse_id=None):
        fulfillment_warehouse_id = warehouse_id
        url = f"https://{self.client.domain}/api/v1/purchase/{fulfillment_warehouse_id}"
        return self.client._request('POST', url, payload)
