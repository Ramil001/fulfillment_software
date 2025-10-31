from odoo import models

class RouteUtils(models.AbstractModel):
 _name = 'route.utils'
 _description = "Route helper"
 
 def getFulfillmentByRoute(self, route_id):
  return route_id