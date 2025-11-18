from odoo import api, SUPERUSER_ID

def apply_stock_settings(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    
    try:
        # Способ 1: Установка параметров
        config_params = env['ir.config_parameter'].sudo()
        config_params.set_param('stock.group_stock_multi_locations', 'True')
        config_params.set_param('stock.group_stock_adv_location', 'True')
        
        # Способ 2: Добавление в группы
        multi_locations_group = env.ref('stock.group_stock_multi_locations')
        adv_location_group = env.ref('stock.group_stock_adv_location')
        
        env.user.write({
            'groups_id': [(4, multi_locations_group.id), (4, adv_location_group.id)]
        })
        
        # Обновляем кэш прав
        env.user.flush_model()
        
        print("✅ Настройки склада успешно применены")
        
    except Exception as e:
        print(f"❌ Ошибка при применении настроек склада: {e}")