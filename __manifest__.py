# -*- coding: utf-8 -*-
{
    'name': "Fulfillment Software",
    'summary': "Fulfillment Management System",
    'description': """
        Comprehensive tools for order fulfillment and inventory management
    """,
    'author': "Fulfillment Software",
    'website': "https://fulfillment.software/",
    'application': True,
    'category': 'Inventory',
    'version': '0.3',
    'license': "OEEL-1",
    'depends': ['base', 'contacts', 'account' ,'web','mail', 'purchase', 'stock', 'product', 'sale_management', 'account_invoice_extract'],
    'assets': {
        'web.assets_backend': [
            'fulfillment_software/static/src/js/systray.js',
            'fulfillment_software/static/src/js/notifications.js',
            'fulfillment_software/static/src/css/systray.css',
            'fulfillment_software/static/src/xml/systray.xml',
        ],
    },
    'data': [
        'security/ir.model.access.csv',     
        'views/fulfillment_profile.xml',     
        'views/views.xml',                  
        'views/fulfillment_warehouses.xml',  
        'views/fulfillment_transfers.xml',    
        'views/fulfillment_partners.xml',    
        'views/fulfillment_contacts.xml',    
        'views/fulfillment_order.xml',
        'views/fulfillment_products.xml',
        'views/fulfillment_locations.xml',
        'data/action_import_all.xml',
        'views/stock_quant_views.xml',
        'views/partner_list_in_warehouse.xml',
        'views/fulfillment_main.xml',        
        'views/menu.xml',                   
    ],
    'demo': [
        'demo/demo.xml',
    ],
}