from odoo import models, api, _
import logging
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)

class FulfillmentContactSync(models.Model):
    _name = 'fulfillment.contact.sync'
    _description = 'Sync Contacts from Fulfillment API'

    @api.model
    def import_contacts(self):
        _logger.info(f"[import_contacts]")
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT][IMPORT] Профиль интеграции не найден")
            return

        client = FulfillmentAPIClient(profile)

        try:
            response = client.contact.list()  # Получаем список контактов
            contacts = response.get('contacts') or response
        except FulfillmentAPIError as e:
            _logger.error(f"[FULFILLMENT][IMPORT] Ошибка API: {e}")
            return
        except Exception as e:
            _logger.exception(f"[FULFILLMENT][IMPORT] Неожиданная ошибка: {e}")
            return

        for c in contacts:
            external_id = c.get('id')
            name = c.get('name')
            email = c.get('email')
            phone = c.get('phone')
            street = c.get('street')
            street2 = c.get('street2')
            city = c.get('city')
            zip_code = c.get('zip')
            country_name = c.get('country')
            is_company = c.get('isCompany', False)
            company_name = c.get('companyName', False)

            partner = self.env['res.partner'].search([('fulfillment_contact_id', '=', external_id)], limit=1)
            if partner:
                partner.write({
                    'name': name,
                    'email': email,
                    'phone': phone,
                    'street': street,
                    'street2': street2,
                    'city': city,
                    'zip': zip_code,
                    'is_company': is_company,
                    'company_name': company_name,
                })
                _logger.info(f"[FULFILLMENT][IMPORT] Обновлён партнёр {name}")
            else:
                country = self.env['res.country'].search([('name', '=', country_name)], limit=1)
                partner_vals = {
                    'name': name,
                    'email': email,
                    'phone': phone,
                    'street': street,
                    'street2': street2,
                    'city': city,
                    'zip': zip_code,
                    'country_id': country.id if country else False,
                    'is_company': is_company,
                    'company_name': company_name,
                    'fulfillment_contact_id': external_id,
                }
                self.env['res.partner'].create(partner_vals)
                _logger.info(f"[FULFILLMENT][IMPORT] Создан новый партнёр {name}")
