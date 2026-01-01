from odoo import models


class FulfillmentImportWarehouse(models.AbstractModel):
    _name = "fulfillment.import.warehouse"
    _description = "Fulfillment Import Warehouse"
    
    def run(self):
        profile = self._get_profile()
        self.env["fulfillment.warehouse"].import_from_api(profile)