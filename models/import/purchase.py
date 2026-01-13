from odoo import models


class FulfillmentImportPurchase(models.AbstractModel):
    _name = "fulfillment.import.purchase"
    _description = "Fulfillment Import Warehouse"
    
    def run(self):
        profile = self._get_profile()
        self.env["fulfillment.warehouse"].import_from_api(profile)