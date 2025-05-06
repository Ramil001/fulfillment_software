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
    'version': '0.2',
    'license': "OEEL-1",
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'views/fulfillment_main.xml', 
        'views/views.xml',
        'views/menu.xml',
        'views/templates.xml',
    ],
    'demo': [
        'demo/demo.xml',
    ],
}