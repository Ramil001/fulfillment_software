from odoo import api

def get_default_domain_host(env):
    """
    Возвращает домен текущей базы из параметров Odoo.
    """
    return env['ir.config_parameter'].sudo().get_param('web.base.url')


# Заготовка хелпера общего для отправки события.
# Хотя событие и есть отправка на апи.
# Посмотреть возможность отправлять изменения через API или напрямую Odoo -> Odoo 
def _send_action(self): 
    return True