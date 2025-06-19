from odoo import models, fields, api
import requests
import logging


_logger = logging.getLogger(__name__)

class FulfillmentPartners(models.Model):
   _name = 'fulfillment.partners'
   _description = 'Fulfillment Partners'
   
   name = fields.Char(string="Fulfillemnt name")
   
   