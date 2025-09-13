from odoo import api

def get_default_domain_host(env):
    """
    Возвращает домен текущей базы из параметров Odoo.
    """
    return env['ir.config_parameter'].sudo().get_param('web.base.url')
