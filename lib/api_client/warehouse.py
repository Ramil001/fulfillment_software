import logging


_logger = logging.getLogger(__name__)

class WarehouseAPI:
    def __init__(self, client):
        self.client = client
        
    def create(self, fulfillment_id, payload):
        url = f"https://{self.client.api_domain}/api/v1/fulfillments/{fulfillment_id}/warehouses"
        return self.client._request('POST', url, payload)

    def get(self):
        url = f"https://{self.client.api_domain}/api/v1/fulfillments/{self.client.profile_id}/warehouses"
        return self.client._request('GET', url)
    
    # Get warehouse transfers by warehouse_id:
    def get_warehouse_transfers(self, warehouse_id):
        url = f"https://{self.client.api_domain}/api/v1/warehouses/{warehouse_id}/transfers"
        return self.client._request('GET', url)

    def update(self, fulfillment_id, warehouse_id, payload):
        url = f"https://{self.client.api_domain}/api/v1/fulfillments/{fulfillment_id}/warehouses/{warehouse_id}"
        return self.client._request('PATCH', url, payload)

    def delete(self, fulfillment_id, warehouse_id): 
        _logger.info(f"[TEST DELETE]: {fulfillment_id} : {warehouse_id}")
        return "TEST"