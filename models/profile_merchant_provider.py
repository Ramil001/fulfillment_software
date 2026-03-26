# -*- coding: utf-8 -*-
"""Extended profile blocks: Client (merchant) vs Fulfillment center (provider)."""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class FulfillmentProfileMerchant(models.Model):
    _name = "fulfillment.profile.merchant"
    _description = "Client / merchant fulfillment settings"

    profile_id = fields.Many2one(
        "fulfillment.profile",
        string="Fulfillment profile",
        required=True,
        ondelete="cascade",
        index=True,
    )
    internal_code = fields.Char(
        string="Internal code",
        help="Unique code for this seller in your WMS / integrations.",
    )
    trade_brand_name = fields.Char(
        string="Trade / brand name",
        help="Commercial or brand name if different from legal entity.",
    )
    business_type = fields.Selection(
        [
            ("b2b", "B2B"),
            ("b2c", "B2C"),
            ("marketplace", "Marketplace"),
            ("hybrid", "Hybrid"),
        ],
        string="Business type",
        default="b2c",
    )
    # Returns
    return_address = fields.Text(string="Return shipping address")
    return_policy_notes = fields.Text(
        string="Return policy & acceptance rules",
        help="Where returns go, how defects are accepted, RMA rules.",
    )
    preferred_carrier_ids = fields.Many2many(
        "fulfillment.shipping.carrier",
        "fulfillment_merchant_carrier_rel",
        "merchant_id",
        "carrier_id",
        string="Preferred carriers (CP)",
    )
    default_packaging_type = fields.Selection(
        [
            ("safe_bag", "Safe bag / poly mailer"),
            ("box_s", "Box S"),
            ("box_m", "Box M"),
            ("box_l", "Box L"),
            ("bubble", "Bubble wrap / padded"),
            ("custom", "Custom / per SOP"),
        ],
        string="Default packaging",
        default="box_m",
    )
    insert_rules = fields.Text(
        string="Insert / packing rules",
        help="Invoice, promos, samples — what must be included in each parcel.",
    )
    branding_requirements = fields.Text(
        string="Branding requirements",
        help="Branded tape, inserts, neutral outer box, etc.",
    )
    # Integration
    shop_webhook_url = fields.Char(string="Shop webhook URL (inbound)")
    external_erp_id = fields.Char(
        string="External ERP / shop ID",
        help="Customer ID in external Odoo, Shopify, etc.",
    )
    stock_alert_threshold = fields.Float(
        string="Low-stock alert threshold",
        digits=(16, 2),
        help="Alert when sellable qty falls below this value (per SKU or global — define in SOP).",
    )
    # Finance
    settlement_currency_id = fields.Many2one(
        "res.currency",
        string="Settlement currency",
        default=lambda self: self.env.ref("base.EUR", raise_if_not_found=False),
    )
    tariff_plan_ref = fields.Char(
        string="Tariff plan ID",
        help="Reference to your pricing plan (storage, pick, pack).",
    )
    priority_level = fields.Integer(
        string="Picking priority",
        default=10,
        help="Lower number = higher priority in the pick queue (e.g. VIP = 1).",
    )
    qos_level = fields.Selection(
        [
            ("standard", "Standard"),
            ("high", "High"),
            ("critical", "Critical (e.g. MQTT QoS1 / guaranteed delivery)"),
        ],
        string="Integration QoS level",
        default="standard",
        help="Target reliability for status / event delivery to the shop.",
    )
    order_error_rate = fields.Float(
        string="Order error rate (rolling)",
        digits=(5, 2),
        default=0.0,
        help="Placeholder for analytics: % of orders with bad data from client.",
    )

    _sql_constraints = [
        (
            "fulfillment_profile_merchant_profile_uniq",
            "unique(profile_id)",
            "Only one merchant settings block per fulfillment profile.",
        ),
    ]


class FulfillmentProfileProvider(models.Model):
    _name = "fulfillment.profile.provider"
    _description = "Fulfillment center / warehouse provider profile"

    profile_id = fields.Many2one(
        "fulfillment.profile",
        string="Fulfillment profile",
        required=True,
        ondelete="cascade",
        index=True,
    )
    geo_lat = fields.Float(string="Latitude", digits=(10, 7))
    geo_lng = fields.Float(string="Longitude", digits=(10, 7))
    warehouse_address = fields.Text(string="Warehouse address (for last-mile / routing)")
    timezone = fields.Char(
        string="Timezone",
        help="IANA zone, e.g. Europe/Berlin",
        default="Europe/Berlin",
    )
    processing_sla_pick_minutes = fields.Integer(
        string="SLA: order → pick started (min)",
        help="Target from order acceptance to pick start.",
    )
    processing_sla_ship_minutes = fields.Integer(
        string="SLA: order → shipped (min)",
        help="Target from order to shipped / handover.",
    )
    cut_off_time = fields.Char(
        string="Cut-off time",
        help="HH:MM — orders packed before this ship same day.",
        default="17:00",
    )
    working_schedule = fields.Text(
        string="Working schedule",
        help="Days and hours of operation affecting SLA.",
    )
    delivery_pick_up_time = fields.Text(
        string="Courier pick-up windows",
        help="Per-carrier arrival windows at the dock.",
    )
    storage_zone_types = fields.Text(
        string="Storage zone types",
        help="Mezzanine, pallet, cold, high-value cage, etc.",
    )
    daily_order_capacity = fields.Integer(
        string="Daily order capacity (cap)",
        help="Max orders/day this FC commits to before throttling.",
    )
    safety_stock_limit = fields.Float(
        string="Safety stock shelf share",
        digits=(16, 2),
        help="Reserved capacity or generic safety stock policy for clients.",
    )
    supports_dangerous_goods = fields.Boolean(string="ADR / dangerous goods")
    supports_food = fields.Boolean(string="Food / perishables")
    supports_oversized = fields.Boolean(string="Oversized / KGT")
    iot_gateway_ids = fields.Char(
        string="Gateway / scanner IDs",
        help="TSD / IoT gateway identifiers (optional).",
    )
    label_printer_status = fields.Selection(
        [
            ("ok", "OK"),
            ("warning", "Warning"),
            ("offline", "Offline / unknown"),
        ],
        string="Label printer status",
        default="ok",
    )
    sla_score = fields.Float(
        string="SLA score",
        digits=(5, 2),
        default=100.0,
        help="Internal score for on-time pick/pack (for dashboards).",
    )

    _sql_constraints = [
        (
            "fulfillment_profile_provider_profile_uniq",
            "unique(profile_id)",
            "Only one provider settings block per fulfillment profile.",
        ),
    ]


class FulfillmentProfileExtended(models.Model):
    _inherit = "fulfillment.profile"

    merchant_setting_id = fields.Many2one(
        "fulfillment.profile.merchant",
        string="Merchant settings",
        copy=False,
        ondelete="cascade",
    )
    provider_setting_id = fields.Many2one(
        "fulfillment.profile.provider",
        string="Provider settings",
        copy=False,
        ondelete="cascade",
    )

    def _ensure_merchant_provider_records(self):
        Merchant = self.env["fulfillment.profile.merchant"].sudo()
        Provider = self.env["fulfillment.profile.provider"].sudo()
        ctx = dict(self.env.context, skip_merchant_provider_ensure=True)
        for rec in self:
            if not rec.merchant_setting_id:
                m = Merchant.create({"profile_id": rec.id})
                rec.with_context(ctx).write({"merchant_setting_id": m.id})
            if not rec.provider_setting_id:
                p = Provider.create({"profile_id": rec.id})
                rec.with_context(ctx).write({"provider_setting_id": p.id})

    def _ensure_capabilities_record(self):
        Cap = self.env["fulfillment.profile.capabilities"].sudo()
        ctx = dict(self.env.context, skip_capabilities_ensure=True)
        for rec in self:
            if not rec.capabilities_id:
                try:
                    c = Cap.create({"version": "1.1"})
                    rec.with_context(ctx).write({"capabilities_id": c.id})
                except Exception:
                    _logger.exception(
                        "fulfillment_software: could not create capabilities for profile id=%s "
                        "(upgrade module or check DB schema)",
                        rec.id,
                    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._ensure_merchant_provider_records()
        records._ensure_capabilities_record()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("skip_merchant_provider_ensure"):
            self._ensure_merchant_provider_records()
        if not self.env.context.get("skip_capabilities_ensure"):
            self._ensure_capabilities_record()
        return res
