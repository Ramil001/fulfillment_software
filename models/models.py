from odoo import models, fields, api
import logging
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)


class FulfillmentDashboard(models.Model):
    _name = 'fulfillment.dashboard'
    _description = 'Fulfillment Dashboard'

    name = fields.Char(string="Name")
    email = fields.Char(string="Email")
    phone = fields.Char(string="Phone")
    description = fields.Text(string="Description")

    subscription_ids = fields.One2many(
        'fulfillment.subscription',
        'dashboard_id',
        string="Подписки"
    )
    subscription_count = fields.Integer(
        string='Количество подписок',
        compute='_compute_subscription_count'
    )

    warehouse_ids = fields.Many2many(
        'stock.warehouse',
        string='Склады',
        compute='_compute_warehouse_ids'
    )

    @api.depends('subscription_ids')
    def _compute_subscription_count(self):
        for record in self:
            record.subscription_count = len(record.subscription_ids)

    @api.depends()
    def _compute_warehouse_ids(self):
        warehouses = self.env['stock.warehouse'].search([])
        for record in self:
            record.warehouse_ids = warehouses

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

    def action_open_subscriptions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Подписки',
            'res_model': 'fulfillment.subscription',
            'view_mode': 'tree,form',
            'domain': [('dashboard_id', '=', self.id)],
            'target': 'current',
        }

    @api.model
    def create(self, vals):
        dashboard = super().create(vals)

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("No active Fulfillment profile found, subscriptions not loaded.")
            return dashboard

        client = FulfillmentAPIClient(profile)

        try:
            # Тут мы уже не requests дергаем, а твой API client
            response = client._request("GET", f"https://{profile.api_domain}/api/v1/fulfillments")
            data = response.get("data", [])

            subscriptions = [
                {'name': item.get('name', 'New Subscription'), 'dashboard_id': dashboard.id}
                for item in data
            ]
            if subscriptions:
                self.env['fulfillment.subscription'].create(subscriptions)

        except Exception as e:
            _logger.error("Error creating subscriptions: %s", str(e))

        return dashboard


class FulfillmentSubscription(models.Model):
    _name = 'fulfillment.subscription'
    _description = 'Fulfillment Subscription'

    name = fields.Char(string="Название подписки")
    dashboard_id = fields.Many2one('fulfillment.dashboard', string="Дашборд")
