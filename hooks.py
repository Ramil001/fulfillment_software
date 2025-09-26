from odoo import api, SUPERUSER_ID

def post_init_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # Включаем Storage Locations
    env['ir.config_parameter'].sudo().set_param('stock.multi_locations', '1')
    env.ref('stock.group_stock_multi_locations').users |= env.ref('base.user_admin')

    # Включаем Multi-Step Routes
    env['ir.config_parameter'].sudo().set_param('stock.group_adv_location', '1')
    env.ref('stock.group_adv_location').users |= env.ref('base.user_admin')

    # Дополнительно: включаем многошаговую логику на всех складах
    warehouses = env['stock.warehouse'].search([])
    for wh in warehouses:
        wh.write({
            'reception_steps': 'two_steps',       # или 'three_steps'
            'delivery_steps': 'pick_pack_ship',  # или 'pick_ship'
        })
