# fulfillment_software/models/helpers.py
from odoo.http import request

def get_default_domain_host(env):
    """
    Возвращает домен текущей базы из параметров Odoo или из request.
    """
    if request and request.httprequest:
        return request.httprequest.host_url.rstrip('/')
    return env['ir.config_parameter'].sudo().get_param('web.base.url')
