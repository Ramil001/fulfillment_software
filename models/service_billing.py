# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)

SERVICE_TYPES = [
    ("storage", "Storage"),
    ("packing", "Packing"),
    ("shipping", "Shipping"),
    ("inspection", "Pre-shipment Inspection"),
]


class FulfillmentServicePrice(models.Model):
    _name = "fulfillment.service.price"
    _description = "Fulfillment service tariff (synced from API)"
    _order = "service_type, id"

    partner_id = fields.Many2one(
        "fulfillment.partners",
        string="Partner (publisher)",
        ondelete="cascade",
        index=True,
    )
    profile_id = fields.Many2one(
        "fulfillment.profile",
        string="My Profile",
        ondelete="cascade",
        index=True,
    )
    is_mine = fields.Boolean(compute="_compute_is_mine", store=True)

    @api.depends("profile_id", "partner_id")
    def _compute_is_mine(self):
        for rec in self:
            rec.is_mine = bool(rec.profile_id)

    external_id = fields.Char(string="API id", index=True)
    service_type = fields.Selection(SERVICE_TYPES, required=True)
    unit = fields.Char(string="Unit", default="flat")
    amount = fields.Float(string="Amount", digits=(16, 4))
    currency = fields.Char(string="Currency", default="USD")
    warehouse_ext_id = fields.Char(string="Warehouse (API id)")
    last_sync_at = fields.Datetime(string="Last sync")

    _sql_constraints = [
        (
            "fulfillment_service_price_partner_ext_uniq",
            "unique(partner_id, external_id)",
            "A price line with this API id already exists for this partner.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.is_mine and not self.env.context.get("skip_api_sync"):
                rec._push_to_api()
        return records

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get("skip_api_sync"):
            for rec in self.filtered("is_mine"):
                rec._push_to_api()
        return res

    def unlink(self):
        if not self.env.context.get("skip_api_sync"):
            for rec in self.filtered(lambda r: r.is_mine and r.external_id):
                rec._delete_from_api()
        return super().unlink()

    def _push_to_api(self):
        self.ensure_one()
        if not self.profile_id or not self.profile_id.fulfillment_profile_id:
            return
        client = FulfillmentAPIClient(self.profile_id)
        payload = {
            "fulfillment_id": self.profile_id.fulfillment_profile_id,
            "service_type": self.service_type,
            "unit": self.unit or "flat",
            "amount": self.amount,
            "currency": self.currency or "USD",
        }
        if self.warehouse_ext_id:
            payload["warehouse_id"] = self.warehouse_ext_id

        try:
            # API doesn't support PATCH for prices yet, so we recreate if it exists
            if self.external_id:
                self._delete_from_api()
            
            resp = client.service_billing.create_price(payload)
            data = resp.get("data", {})
            if data and data.get("id"):
                self.with_context(skip_api_sync=True).write({"external_id": data["id"]})
        except Exception as e:
            _logger.error("[ServicePrice] Failed to push price to API: %s", e)

    def _delete_from_api(self):
        self.ensure_one()
        if not self.profile_id or not self.external_id:
            return
        client = FulfillmentAPIClient(self.profile_id)
        try:
            client.service_billing.delete_price(self.external_id, self.profile_id.fulfillment_profile_id)
        except Exception as e:
            _logger.error("[ServicePrice] Failed to delete price from API: %s", e)

    @api.model
    def _upsert_from_api_row(self, partner, row):
        """Upsert a price row fetched from API for a partner (read-only side)."""
        ext = row.get("id")
        if not ext:
            return self.browse()
        vals = {
            "service_type": row.get("service_type") or "storage",
            "unit": row.get("unit") or "flat",
            "amount": float(row.get("amount", 0) or 0),
            "currency": (row.get("currency") or "USD")[:3],
            "warehouse_ext_id": row.get("warehouse_id") or False,
            "last_sync_at": fields.Datetime.now(),
        }
        rec = self.search(
            [("partner_id", "=", partner.id), ("external_id", "=", ext)], limit=1
        )
        if rec:
            rec.with_context(skip_api_sync=True).write(vals)
            return rec
        vals["partner_id"] = partner.id
        vals["external_id"] = ext
        return self.with_context(skip_api_sync=True).create(vals)

    @api.model
    def _upsert_own_from_api_row(self, profile, row):
        """Upsert a price row fetched from API for own profile (editable side)."""
        ext = row.get("id")
        if not ext:
            return self.browse()
        vals = {
            "service_type": row.get("service_type") or "storage",
            "unit": row.get("unit") or "flat",
            "amount": float(row.get("amount", 0) or 0),
            "currency": (row.get("currency") or "USD")[:3],
            "warehouse_ext_id": row.get("warehouse_id") or False,
            "last_sync_at": fields.Datetime.now(),
        }
        rec = self.search(
            [("profile_id", "=", profile.id), ("external_id", "=", ext)], limit=1
        )
        if rec:
            rec.with_context(skip_api_sync=True).write(vals)
            return rec
        vals["profile_id"] = profile.id
        vals["external_id"] = ext
        return self.with_context(skip_api_sync=True).create(vals)


class FulfillmentServiceUsage(models.Model):
    _name = "fulfillment.service.usage"
    _description = "Fulfillment service billing line (synced from API)"
    _order = "api_created_at desc, id desc"

    external_id = fields.Char(string="API id", required=True, index=True)
    creditor_fulfillment_id = fields.Char(string="Creditor fulfillment UUID", index=True)
    debtor_fulfillment_id = fields.Char(string="Debtor fulfillment UUID", index=True)
    creditor_partner_id = fields.Many2one(
        "fulfillment.partners",
        string="Creditor (partner)",
        compute="_compute_partners",
        store=True,
    )
    debtor_partner_id = fields.Many2one(
        "fulfillment.partners",
        string="Debtor (partner)",
        compute="_compute_partners",
        store=True,
    )
    service_type = fields.Selection(SERVICE_TYPES, required=True)
    quantity = fields.Float(string="Quantity", digits=(16, 6))
    unit_price = fields.Float(string="Unit price", digits=(16, 4))
    line_total = fields.Float(string="Line total", digits=(16, 4))
    currency = fields.Char(string="Currency", default="USD")
    warehouse_ext_id = fields.Char(string="Warehouse (API id)")
    transfer_ref_id = fields.Char(string="Transfer ref")
    source_ref = fields.Char(string="Source ref")
    status = fields.Char(string="Status")
    api_created_at = fields.Datetime(string="API created at")

    _sql_constraints = [
        (
            "fulfillment_service_usage_ext_uniq",
            "unique(external_id)",
            "This API usage line was already imported.",
        ),
    ]

    @api.depends("creditor_fulfillment_id", "debtor_fulfillment_id")
    def _compute_partners(self):
        Partner = self.env["fulfillment.partners"].sudo()
        for rec in self:
            rec.creditor_partner_id = (
                Partner.search(
                    [("fulfillment_id", "=", rec.creditor_fulfillment_id)], limit=1
                )
                if rec.creditor_fulfillment_id
                else False
            )
            rec.debtor_partner_id = (
                Partner.search(
                    [("fulfillment_id", "=", rec.debtor_fulfillment_id)], limit=1
                )
                if rec.debtor_fulfillment_id
                else False
            )

    @api.model
    def _parse_api_dt(self, s):
        if not s:
            return False
        if isinstance(s, str) and len(s) >= 19:
            try:
                from datetime import datetime

                return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass
        return False

    @api.model
    def upsert_from_api_row(self, row):
        ext = row.get("id")
        if not ext:
            return self.browse()
        vals = {
            "creditor_fulfillment_id": row.get("creditor_fulfillment_id") or "",
            "debtor_fulfillment_id": row.get("debtor_fulfillment_id") or "",
            "service_type": row.get("service_type") or "storage",
            "quantity": float(row.get("quantity", 0) or 0),
            "unit_price": float(row.get("unit_price", 0) or 0),
            "line_total": float(row.get("line_total", 0) or 0),
            "currency": (row.get("currency") or "USD")[:3],
            "warehouse_ext_id": row.get("warehouse_id") or False,
            "transfer_ref_id": row.get("transfer_ref_id") or False,
            "source_ref": row.get("source_ref") or False,
            "status": row.get("status") or "confirmed",
            "api_created_at": self._parse_api_dt(row.get("created_at")),
        }
        rec = self.search([("external_id", "=", ext)], limit=1)
        if rec:
            rec.write(vals)
            return rec
        vals["external_id"] = ext
        return self.create(vals)

    @api.model
    def sync_all_for_active_profile(self):
        profile = self.env["fulfillment.partners"]._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            raise UserError(_("Configure Fulfillment profile first."))
        my_id = profile.fulfillment_profile_id.strip()
        client = FulfillmentAPIClient(profile)
        n = 0
        try:
            for role in ("debtor", "creditor"):
                resp = client.service_billing.list_usages(my_id, role, limit=500)
                rows = resp.get("data", [])
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    self.upsert_from_api_row(row)
                    n += 1
        except FulfillmentAPIError as e:
            raise UserError(str(e)) from e
        return n

    @api.model
    def action_open_payables(self):
        profile = self.env["fulfillment.partners"]._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            raise UserError(_("Configure Fulfillment profile first."))
        my_id = profile.fulfillment_profile_id.strip()
        return {
            "type": "ir.actions.act_window",
            "name": _("I owe (payables)"),
            "res_model": "fulfillment.service.usage",
            "view_mode": "list,form",
            "domain": [("debtor_fulfillment_id", "=", my_id)],
        }

    @api.model
    def action_open_receivables(self):
        profile = self.env["fulfillment.partners"]._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            raise UserError(_("Configure Fulfillment profile first."))
        my_id = profile.fulfillment_profile_id.strip()
        return {
            "type": "ir.actions.act_window",
            "name": _("Owed to me (receivables)"),
            "res_model": "fulfillment.service.usage",
            "view_mode": "list,form",
            "domain": [("creditor_fulfillment_id", "=", my_id)],
        }

    @api.model
    def action_sync_from_api_menu(self):
        self.sync_all_for_active_profile()
        return {
            "type": "ir.actions.act_window",
            "name": _("Service billing lines"),
            "res_model": "fulfillment.service.usage",
            "view_mode": "list,form",
        }


class FulfillmentPartnersServiceBilling(models.Model):
    _inherit = "fulfillment.partners"

    service_price_ids = fields.One2many(
        "fulfillment.service.price",
        "partner_id",
        string="Service prices",
    )

    def action_sync_service_prices_from_api(self):
        profile = self.env["fulfillment.partners"]._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            raise UserError(_("Configure Fulfillment profile first."))
        client = FulfillmentAPIClient(profile)
        Price = self.env["fulfillment.service.price"].sudo()
        for partner in self:
            if not partner.fulfillment_id:
                continue
            try:
                resp = client.service_billing.list_prices(partner.fulfillment_id.strip())
            except FulfillmentAPIError as e:
                raise UserError(str(e)) from e
            rows = resp.get("data", [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                Price._upsert_from_api_row(partner, row)
        return True
