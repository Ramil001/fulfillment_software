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
    'depends': ['base', 'stock'],
    'data': [
        'security/ir.model.access.csv',  # Права доступа
        'views/views.xml',               # Представления
        'views/fulfillment_main.xml',    # Действие
        'views/menu.xml',                # Меню (после действия)
    ],
    'demo': [
        'demo/demo.xml',
    ],
}