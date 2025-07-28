class WarehousesAPI:
    def __init__(self, client: FulfillmentAPIClient):
        self.client = client

    def create_warehouse(self, fulfillment_id, payload):
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses"
        return self.client._request('POST', url, payload)

    def update_warehouse(self, fulfillment_id, warehouse_id, payload):
        url = f"https://{self.client.domain}/api/v1/fulfillments/{fulfillment_id}/warehouses/{warehouse_id}"
        return self.client._request('PATCH', url, payload)

    def get_warehouses(self):
        url = f"https://{self.client.domain}/api/v1/fulfillments/{self.client.profile_id}/warehouses"
        return self.client._request('GET', url)
