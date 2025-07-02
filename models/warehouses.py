from odoo import models, fields, api
import requests

class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    is_fulfillment = fields.Boolean(string="Is this a fulfillment warehouse?")
    fulfillment_creator_id = fields.Char(string="Fulfillment created Id")
    fulfillment_software_internal_id = fields.Integer(string="Fulfillment Software internal Id resource")
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Linked fulfillment partner")

    @api.model
    def create(self, vals):
        warehouse = super(FulfillmentWarehouses, self).create(vals)
        if vals.get('is_fulfillment'):
            warehouse._send_to_api()
        return warehouse

    def write(self, vals):
        res = super(FulfillmentWarehouses, self).write(vals)
        for warehouse in self:
            if warehouse.is_fulfillment:
                warehouse._send_to_api()
        return res

    def _send_to_api(self):
        """Отправляем склад в API"""
        api_key = self.env['ir.config_parameter'].sudo().get_param('fulfillment.api_key')
        url = f"https://api.fulfillment.software/api/v1/fulfillments/{self.fulfillment_owner_id.fulfillment_id}/warehouses/"
        headers = {
            "Content-Type": "application/json",
            "X-Fulfillment-API-Key": api_key
        }
        payload = {
            "name": self.name,
            "code": self.code,
            "location": self.partner_id.country_id.code if self.partner_id else "N/A"
        }
        if self.fulfillment_creator_id:
            # Если есть ID — обновляем
            url = f"{url}{self.fulfillment_creator_id}/"
            response = requests.put(url, headers=headers, json=payload)
        else:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code in (200, 201):
                data = response.json().get("data")
                if data:
                    self.fulfillment_creator_id = str(data.get("id"))

        if not response.ok:
            raise UserError(f"API error: {response.status_code} - {response.text}")
           
               
    @api.model
    def synchronize_warehouses(self, partner, warehouses_data):
        for wh in warehouses_data:
            external_id = str(wh["id"])
            # Ищем по внешнему ID
            existing = self.search([("fulfillment_creator_id", "=", external_id)], limit=1)

            # Если не нашли по внешнему ID — ищем по имени + компании
            if not existing:
                existing = self.search([
                    ("name", "=", wh["name"]),
                    ("company_id", "=", self.env.company.id),
                ], limit=1)

            vals = {
                "name": wh["name"],
                "code": wh["code"],
                "is_fulfillment": True,
                "fulfillment_creator_id": external_id,
                "fulfillment_software_internal_id": wh["id"],
                "fulfillment_owner_id": partner.id,
                "company_id": self.env.company.id,
            }

            if existing:
                existing.write(vals)
            else:
                self.create(vals)
