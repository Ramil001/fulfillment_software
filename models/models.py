from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)

class FulfillmentDashboard(models.Model):
    _name = 'fulfillment.dashboard'
    _description = 'Fulfillment Dashboard'

    name = fields.Char(string="Name")
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    description = fields.Text(string="Description")
    subscription_ids = fields.One2many('fulfillment.subscription', 'dashboard_id', string="Подписки")
    
    subscription_count = fields.Integer(
        string='Количество подписок',
        compute='_compute_subscription_count'
    )
    
    def action_create_subscription(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Новая подписка',
            'res_model': 'fulfillment.subscription',
            'view_mode': 'form',
            'context': {'default_dashboard_id': self.id},
            'target': 'new',
        }
    
    def action_open_subscription(self, subscription_id):
        subscription = self.env['fulfillment.subscription'].browse(subscription_id)
        return {
            'type': 'ir.actions.act_window',
            'name': 'Подписка',
            'res_model': 'fulfillment.subscription',
            'view_mode': 'form',
            'res_id': subscription.id,
            'target': 'current',
        }
    
    def _compute_subscription_count(self):
        for record in self:
            record.subscription_count = len(record.subscription_ids)


    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        string='Склады',
        compute='_compute_warehouse_ids',
        store=False
    )
    subscriptions = fields.Text(string="Подписки")

    @api.depends()
    def _compute_warehouse_ids(self):
        # Кэшируем результат для всех записей
        warehouses = self.env['stock.warehouse'].search([])
        for record in self:
            record.warehouse_ids = warehouses


    @api.model
    def create(self, vals):
        dashboard = super().create(vals)
        
        try:
            headers = {"X-Fulfillment-API-Key": "e2vlLo1LM6zFBOnv95jCyZ0jlIib04acYLLL1rXmhlQ"}
            response = requests.get(
                'https://api.fulfillment.software/api/v1/fulfillments',
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json().get("data", [])
            
            # Создаем записи пакетно
            subscriptions = []
            for item in data:
                subscriptions.append({
                    'name': item.get('name', 'New Subscription'),
                    'dashboard_id': dashboard.id
                })
            
            if subscriptions:
                self.env['fulfillment.subscription'].create(subscriptions)
                
        except requests.exceptions.RequestException as e:
            _logger.error("Network error: %s", str(e))
        except Exception as e:
            _logger.error("Error creating subscriptions: %s", str(e))
        
        return dashboard


class FulfillmentSubscription(models.Model):
    _name = 'fulfillment.subscription'
    _description = 'Fulfillment Subscription'

    name = fields.Char(string="Название подписки")
    dashboard_id = fields.Many2one('fulfillment.dashboard', string="Дашборд")
