from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)


class FulfillmentProfile(models.Model):
    _name = 'fulfillment.profile'
    _description = 'Fulfillment Profile'
    
    # Название компании
    name = fields.Char(string="Fulfillment name")
    # Выбор города
    country_id = fields.Many2one(
        'res.country',
        string="Country",
        default=lambda self: self.env.ref('base.de').id
    )
    
    address = fields.Char(string="Address")
    phone = fields.Char(string="Phone number")
    email = fields.Char(string="Email")
    capabilities_id = fields.Many2one(
        'fulfillment.profile.capabilities',
        string= "Capabilities",
        ondelete='cascade'
    )
    
    @api.model
    def get_my_profile_action(self):
        profile = self.search([], limit=1)
        if not profile:
            profile = self.create({'name': 'My Company'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'fulfillment.profile',
            'view_mode': 'form',
            'res_id': profile.id,
            'views': [(self.env.ref('fulfillment_software.view_fulfillment_profile_form').id, 'form')],
            'target': 'current',
            'flags': {'form': {'action_buttons': True}},
            'context': {'create': False},
        }
    
    
class FulfillmentProfileCapabilities(models.Model): 
    _name = 'fulfillment.profile.capabilities'
    _description = 'Fulfillment Profile Capabilities'
    
    version = fields.Char(string="Version capabilities")
    picking_outgoing = fields.Boolean(string="Picking Outgoing")
    picking_returns = fields.Boolean(string="Picking Returns")
    picking_dropshipping = fields.Boolean(string="Picking Dropshipping")
    picking_crossdock = fields.Boolean(string="Picking Crossdock")
    picking_internal = fields.Boolean(string="Picking Internal")
   
    fulfillment_api_key = fields.Char(string="X-Filfillment-API-Key")