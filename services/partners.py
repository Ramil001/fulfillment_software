from odoo import models, fields, api
import logging
from datetime import datetime
from odoo.exceptions import UserError
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


# Модель которая отвечает за импорт партнеров из api
class ImportPartners(models.Model):
    _inherit = 'res.partner'
    
    
    