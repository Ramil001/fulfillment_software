# -*- coding: utf-8 -*-


# from odoo import http


# class FulfillmentSoftware(http.Controller):
#     @http.route('/fulfillment_software/fulfillment_software', auth='public')
#     def index(self, **kw):
#         return "Hello, world" 

#     @http.route('/fulfillment_software/fulfillment_software/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('fulfillment_software.listing', {
#             'root': '/fulfillment_software/fulfillment_software',
#             'objects': http.request.env['fulfillment_software.fulfillment_software'].search([]),
#         })

# @http.route('/fulfillment_software/fulfillment_software/objects/<model("fulfillment_software.fulfillment_software"):obj>', auth='public')
# def object(self, obj, **kw):
#     return http.request.render('fulfillment_software.object', {
#         'object': obj
#     })
