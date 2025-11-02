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
        'security/ir.model.access.csv',     # Права доступа
        'views/views.xml',                  # Представления
        'views/fulfillment_warehouses.xml',  # Представление для изменения страницы создания скалада
        'views/fulfillment_transfers.xml',   # Представление для изменения страницы создания скалада
        'views/fulfillment_partners.xml',    # Предстваление старницы партнёра
        'views/fulfillment_contacts.xml',    # Предстваление старницы contact
        'views/fulfillment_order.xml',
        'views/fulfillment_products.xml',
        'views/fulfillment_locations.xml',
        'views/stock_quant_views.xml',
        'views/partner_list_in_warehouse.xml',
        'views/fulfillment_main.xml',        # Действие
        'views/menu.xml',                   # Меню (после действия)
    ],
    # "post_init_hook": "post_init_hook",
    'demo': [
        'demo/demo.xml',
    ],
}