# -*- coding: utf-8 -*-
import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)

PACKING_MATERIAL = [
    ("corrugated", "Corrugated cardboard"),
    ("double_wall", "Double-wall cardboard"),
    ("wood", "Wooden crate"),
    ("plastic", "Plastic container"),
    ("poly_bag", "Poly bag"),
    ("bubble_mailer", "Bubble mailer"),
    ("custom", "Custom / Other"),
]

STORAGE_UNIT = [
    ("pallet", "Pallet (EUR 120×80 cm)"),
    ("sqm", "Square metre (m²)"),
    ("cbm", "Cubic metre (m³)"),
    ("shelf", "Shelf position"),
    ("item", "Per item"),
]

SECURITY_LEVEL = [
    ("standard", "Standard"),
    ("high", "High (access control + CCTV)"),
    ("cage", "Caged / locked vault"),
]

SAMPLING_RATE = [
    ("10", "10 %"),
    ("20", "20 %"),
    ("50", "50 %"),
    ("100", "100 % (full inspection)"),
    ("custom", "Custom (specify in description)"),
]

DEFECT_TOLERANCE = [
    ("zero", "Zero tolerance"),
    ("aql_1", "AQL 1.0"),
    ("aql_2_5", "AQL 2.5"),
    ("aql_4", "AQL 4.0"),
    ("custom", "Custom / per agreement"),
]


class FulfillmentServiceCatalog(models.Model):
    _name = "fulfillment.service.catalog"
    _description = "Fulfillment Service Catalog"
    _rec_name = "profile_id"

    def _valid_field_parameter(self, field, name):
        return name == 'placeholder' or super()._valid_field_parameter(field, name)

    # ── Ownership: either own profile OR a partner's catalog ──────────────
    profile_id = fields.Many2one("fulfillment.profile", string="My Profile",
                                  ondelete="cascade", index=True)
    partner_id = fields.Many2one("fulfillment.partners", string="Partner",
                                  ondelete="cascade", index=True)
    is_mine = fields.Boolean(compute="_compute_is_mine", store=True)

    @api.depends("profile_id")
    def _compute_is_mine(self):
        for rec in self:
            rec.is_mine = bool(rec.profile_id)

    external_id = fields.Char(string="API id", index=True)
    last_sync_at = fields.Datetime(string="Last synced")

    # ── PACKING ───────────────────────────────────────────────────────────
    packing_active = fields.Boolean(string="Offer packing services")
    packing_currency = fields.Char(string="Currency", default="USD")
    packing_min_order_price = fields.Float(string="Min. order price", digits=(16, 2))
    packing_description = fields.Text(string="Description / additional info")
    packing_option_ids = fields.One2many(
        "fulfillment.service.packing.option", "catalog_id", string="Box configurations"
    )
    packing_fragile = fields.Boolean(string="Fragile item handling")
    packing_fragile_price = fields.Float(string="Fragile surcharge (per item)", digits=(16, 2))
    packing_custom_branding = fields.Boolean(string="Custom branding / inserts")
    packing_custom_branding_price = fields.Float(string="Branding price (per item)", digits=(16, 2))
    packing_kitting = fields.Boolean(string="Kitting / bundling")
    packing_kitting_price = fields.Float(string="Kitting price (per kit)", digits=(16, 2))
    packing_assembly = fields.Boolean(string="Product assembly required")
    packing_assembly_price = fields.Float(string="Assembly price (per item)", digits=(16, 2))
    packing_label_printing = fields.Boolean(string="Label printing included")
    packing_returns = fields.Boolean(string="Returns / repack handling")
    packing_returns_price = fields.Float(string="Returns surcharge (per item)", digits=(16, 2))

    # ── STORAGE ───────────────────────────────────────────────────────────
    storage_active = fields.Boolean(string="Offer storage services")
    storage_currency = fields.Char(string="Currency", default="USD")
    storage_description = fields.Text(string="Description / additional info")
    storage_ambient_active = fields.Boolean(string="Ambient (dry) storage")
    storage_ambient_unit = fields.Selection(STORAGE_UNIT, string="Billing unit",
                                             default="pallet")
    storage_ambient_price_day = fields.Float(string="Price per unit / day", digits=(16, 4))
    storage_ambient_price_month = fields.Float(string="Price per unit / month", digits=(16, 2))
    storage_cold_active = fields.Boolean(string="Cold-chain storage")
    storage_cold_unit = fields.Selection(STORAGE_UNIT, string="Billing unit",
                                          default="pallet")
    storage_cold_temp_min = fields.Float(string="Temp min (°C)", digits=(5, 1))
    storage_cold_temp_max = fields.Float(string="Temp max (°C)", digits=(5, 1))
    storage_cold_price_day = fields.Float(string="Price per unit / day", digits=(16, 4))
    storage_cold_price_month = fields.Float(string="Price per unit / month", digits=(16, 2))
    storage_hazmat = fields.Boolean(string="Hazmat / DG storage")
    storage_hazmat_description = fields.Text(string="Hazmat capabilities / certifications")
    storage_min_period_days = fields.Integer(string="Minimum storage period (days)")
    storage_security = fields.Selection(SECURITY_LEVEL, string="Security level",
                                         default="standard")
    storage_cctv = fields.Boolean(string="24/7 CCTV")
    storage_fire_suppression = fields.Boolean(string="Fire suppression system")
    storage_insurance = fields.Boolean(string="Cargo insurance available")
    storage_max_pallet_weight_kg = fields.Float(string="Max pallet weight (kg)", digits=(7, 1))

    # ── SHIPPING ──────────────────────────────────────────────────────────
    shipping_active = fields.Boolean(string="Offer shipping services")
    shipping_currency = fields.Char(string="Currency", default="USD")
    shipping_description = fields.Text(string="Description / additional info")
    shipping_carrier_ids = fields.Many2many(
        "fulfillment.shipping.carrier",
        "fulfillment_catalog_carrier_rel",
        "catalog_id",
        "carrier_id",
        string="Carriers",
    )
    shipping_domestic = fields.Boolean(string="Domestic shipping")
    shipping_domestic_price_from = fields.Float(string="Domestic price from", digits=(16, 2))
    shipping_domestic_days = fields.Char(string="Domestic transit days",
                                          placeholder="1–3 business days")
    shipping_eu = fields.Boolean(string="EU / intra-continental shipping")
    shipping_eu_price_from = fields.Float(string="EU price from", digits=(16, 2))
    shipping_eu_days = fields.Char(string="EU transit days", placeholder="3–7 business days")
    shipping_worldwide = fields.Boolean(string="Worldwide shipping")
    shipping_worldwide_price_from = fields.Float(string="Worldwide price from", digits=(16, 2))
    shipping_worldwide_days = fields.Char(string="Worldwide transit days",
                                           placeholder="7–21 business days")
    shipping_tracking = fields.Boolean(string="Real-time tracking included")
    shipping_insurance = fields.Boolean(string="Shipment insurance available")
    shipping_insurance_percent = fields.Float(string="Insurance (% of declared value)",
                                               digits=(5, 2))
    shipping_handling_fee = fields.Float(string="Handling fee (per shipment)", digits=(16, 2))
    shipping_max_weight_kg = fields.Float(string="Max single shipment weight (kg)",
                                           digits=(7, 1))
    shipping_max_dimensions = fields.Char(string="Max dimensions (L×W×H cm)",
                                           placeholder="120×80×100")
    shipping_express = fields.Boolean(string="Express / same-day available")
    shipping_express_price_from = fields.Float(string="Express price from", digits=(16, 2))

    # ── INSPECTION ────────────────────────────────────────────────────────
    inspection_active = fields.Boolean(string="Offer pre-shipment inspection")
    inspection_currency = fields.Char(string="Currency", default="USD")
    inspection_description = fields.Text(string="Description / additional info")
    inspection_types = fields.Char(
        string="Inspection types",
        placeholder="Visual check, Functional test, Measurement, Full QC …"
    )
    inspection_price_per_item = fields.Float(string="Price per item", digits=(16, 4))
    inspection_price_per_order = fields.Float(string="Price per order (min. charge)",
                                               digits=(16, 2))
    inspection_min_price = fields.Float(string="Minimum charge", digits=(16, 2))
    inspection_sampling_rate = fields.Selection(SAMPLING_RATE, string="Default sampling rate",
                                                 default="10")
    inspection_photos = fields.Boolean(string="Photos included")
    inspection_photos_count = fields.Integer(string="Photos per item")
    inspection_video = fields.Boolean(string="Video evidence available")
    inspection_report_pdf = fields.Boolean(string="PDF report provided")
    inspection_report_language = fields.Char(string="Report language(s)",
                                              placeholder="EN, UA, DE …")
    inspection_defect_tolerance = fields.Selection(DEFECT_TOLERANCE, string="Defect tolerance",
                                                    default="aql_2_5")
    inspection_corrective_action = fields.Boolean(string="Corrective action / sorting available")
    inspection_corrective_price = fields.Float(string="Sorting / corrective (per item)",
                                                digits=(16, 4))
    inspection_certifications = fields.Char(string="Certifications / standards",
                                             placeholder="ISO 9001, BSCI, SGS …")

    # ── API sync ─────────────────────────────────────────────────────────
    def _to_api_dict(self):
        """Serialize this catalog record to the JSON payload for the API."""
        self.ensure_one()
        return {
            "packing": {
                "active": self.packing_active,
                "currency": self.packing_currency or "USD",
                "min_order_price": self.packing_min_order_price,
                "description": self.packing_description or "",
                "fragile": self.packing_fragile,
                "fragile_price": self.packing_fragile_price,
                "custom_branding": self.packing_custom_branding,
                "custom_branding_price": self.packing_custom_branding_price,
                "kitting": self.packing_kitting,
                "kitting_price": self.packing_kitting_price,
                "assembly": self.packing_assembly,
                "assembly_price": self.packing_assembly_price,
                "label_printing": self.packing_label_printing,
                "returns": self.packing_returns,
                "returns_price": self.packing_returns_price,
                "box_options": [
                    {
                        "name": opt.name,
                        "length_cm": opt.length_cm,
                        "width_cm": opt.width_cm,
                        "height_cm": opt.height_cm,
                        "max_weight_kg": opt.max_weight_kg,
                        "material": opt.material,
                        "fillers": opt.fillers or "",
                        "price_per_unit": opt.price_per_unit,
                        "notes": opt.notes or "",
                    }
                    for opt in self.packing_option_ids
                ],
            },
            "storage": {
                "active": self.storage_active,
                "currency": self.storage_currency or "USD",
                "description": self.storage_description or "",
                "ambient": {
                    "active": self.storage_ambient_active,
                    "unit": self.storage_ambient_unit or "pallet",
                    "price_day": self.storage_ambient_price_day,
                    "price_month": self.storage_ambient_price_month,
                },
                "cold_chain": {
                    "active": self.storage_cold_active,
                    "unit": self.storage_cold_unit or "pallet",
                    "temp_min": self.storage_cold_temp_min,
                    "temp_max": self.storage_cold_temp_max,
                    "price_day": self.storage_cold_price_day,
                    "price_month": self.storage_cold_price_month,
                },
                "hazmat": self.storage_hazmat,
                "hazmat_description": self.storage_hazmat_description or "",
                "min_period_days": self.storage_min_period_days,
                "security": self.storage_security or "standard",
                "cctv": self.storage_cctv,
                "fire_suppression": self.storage_fire_suppression,
                "insurance": self.storage_insurance,
                "max_pallet_weight_kg": self.storage_max_pallet_weight_kg,
            },
            "shipping": {
                "active": self.shipping_active,
                "currency": self.shipping_currency or "USD",
                "description": self.shipping_description or "",
                "carriers": [c.name for c in self.shipping_carrier_ids],
                "domestic": {
                    "active": self.shipping_domestic,
                    "price_from": self.shipping_domestic_price_from,
                    "transit_days": self.shipping_domestic_days or "",
                },
                "eu": {
                    "active": self.shipping_eu,
                    "price_from": self.shipping_eu_price_from,
                    "transit_days": self.shipping_eu_days or "",
                },
                "worldwide": {
                    "active": self.shipping_worldwide,
                    "price_from": self.shipping_worldwide_price_from,
                    "transit_days": self.shipping_worldwide_days or "",
                },
                "express": {
                    "active": self.shipping_express,
                    "price_from": self.shipping_express_price_from,
                },
                "tracking": self.shipping_tracking,
                "insurance": self.shipping_insurance,
                "insurance_percent": self.shipping_insurance_percent,
                "handling_fee": self.shipping_handling_fee,
                "max_weight_kg": self.shipping_max_weight_kg,
                "max_dimensions": self.shipping_max_dimensions or "",
            },
            "inspection": {
                "active": self.inspection_active,
                "currency": self.inspection_currency or "USD",
                "description": self.inspection_description or "",
                "types": self.inspection_types or "",
                "price_per_item": self.inspection_price_per_item,
                "price_per_order": self.inspection_price_per_order,
                "min_price": self.inspection_min_price,
                "sampling_rate": self.inspection_sampling_rate or "10",
                "photos": self.inspection_photos,
                "photos_count": self.inspection_photos_count,
                "video": self.inspection_video,
                "report_pdf": self.inspection_report_pdf,
                "report_language": self.inspection_report_language or "",
                "defect_tolerance": self.inspection_defect_tolerance or "aql_2_5",
                "corrective_action": self.inspection_corrective_action,
                "corrective_price": self.inspection_corrective_price,
                "certifications": self.inspection_certifications or "",
            },
        }

    @api.model
    def _from_api_dict(self, profile_or_partner, data, is_partner=False):
        """Upsert catalog from API JSON payload. is_partner=True for readonly partner copy."""
        if is_partner:
            domain = [("partner_id", "=", profile_or_partner.id)]
        else:
            domain = [("profile_id", "=", profile_or_partner.id)]
        rec = self.search(domain, limit=1)

        def _f(d, *keys, default=False):
            for k in keys:
                if not isinstance(d, dict):
                    return default
                d = d.get(k, default)
            return d if d is not None else default

        p = data.get("packing", {})
        s = data.get("storage", {})
        sh = data.get("shipping", {})
        ins = data.get("inspection", {})

        vals = {
            "packing_active": bool(_f(p, "active")),
            "packing_currency": _f(p, "currency", default="USD"),
            "packing_min_order_price": float(_f(p, "min_order_price", default=0) or 0),
            "packing_description": _f(p, "description", default=""),
            "packing_fragile": bool(_f(p, "fragile")),
            "packing_fragile_price": float(_f(p, "fragile_price", default=0) or 0),
            "packing_custom_branding": bool(_f(p, "custom_branding")),
            "packing_custom_branding_price": float(_f(p, "custom_branding_price", default=0) or 0),
            "packing_kitting": bool(_f(p, "kitting")),
            "packing_kitting_price": float(_f(p, "kitting_price", default=0) or 0),
            "packing_assembly": bool(_f(p, "assembly")),
            "packing_assembly_price": float(_f(p, "assembly_price", default=0) or 0),
            "packing_label_printing": bool(_f(p, "label_printing")),
            "packing_returns": bool(_f(p, "returns")),
            "packing_returns_price": float(_f(p, "returns_price", default=0) or 0),

            "storage_active": bool(_f(s, "active")),
            "storage_currency": _f(s, "currency", default="USD"),
            "storage_description": _f(s, "description", default=""),
            "storage_ambient_active": bool(_f(s, "ambient", "active")),
            "storage_ambient_unit": _f(s, "ambient", "unit", default="pallet"),
            "storage_ambient_price_day": float(_f(s, "ambient", "price_day", default=0) or 0),
            "storage_ambient_price_month": float(_f(s, "ambient", "price_month", default=0) or 0),
            "storage_cold_active": bool(_f(s, "cold_chain", "active")),
            "storage_cold_unit": _f(s, "cold_chain", "unit", default="pallet"),
            "storage_cold_temp_min": float(_f(s, "cold_chain", "temp_min", default=0) or 0),
            "storage_cold_temp_max": float(_f(s, "cold_chain", "temp_max", default=0) or 0),
            "storage_cold_price_day": float(_f(s, "cold_chain", "price_day", default=0) or 0),
            "storage_cold_price_month": float(_f(s, "cold_chain", "price_month", default=0) or 0),
            "storage_hazmat": bool(_f(s, "hazmat")),
            "storage_hazmat_description": _f(s, "hazmat_description", default=""),
            "storage_min_period_days": int(_f(s, "min_period_days", default=0) or 0),
            "storage_security": _f(s, "security", default="standard"),
            "storage_cctv": bool(_f(s, "cctv")),
            "storage_fire_suppression": bool(_f(s, "fire_suppression")),
            "storage_insurance": bool(_f(s, "insurance")),
            "storage_max_pallet_weight_kg": float(_f(s, "max_pallet_weight_kg", default=0) or 0),

            "shipping_active": bool(_f(sh, "active")),
            "shipping_currency": _f(sh, "currency", default="USD"),
            "shipping_description": _f(sh, "description", default=""),
            "_carriers_raw": _f(sh, "carriers", default=[]),
            "shipping_domestic": bool(_f(sh, "domestic", "active")),
            "shipping_domestic_price_from": float(_f(sh, "domestic", "price_from", default=0) or 0),
            "shipping_domestic_days": _f(sh, "domestic", "transit_days", default=""),
            "shipping_eu": bool(_f(sh, "eu", "active")),
            "shipping_eu_price_from": float(_f(sh, "eu", "price_from", default=0) or 0),
            "shipping_eu_days": _f(sh, "eu", "transit_days", default=""),
            "shipping_worldwide": bool(_f(sh, "worldwide", "active")),
            "shipping_worldwide_price_from": float(_f(sh, "worldwide", "price_from", default=0) or 0),
            "shipping_worldwide_days": _f(sh, "worldwide", "transit_days", default=""),
            "shipping_express": bool(_f(sh, "express", "active")),
            "shipping_express_price_from": float(_f(sh, "express", "price_from", default=0) or 0),
            "shipping_tracking": bool(_f(sh, "tracking")),
            "shipping_insurance": bool(_f(sh, "insurance")),
            "shipping_insurance_percent": float(_f(sh, "insurance_percent", default=0) or 0),
            "shipping_handling_fee": float(_f(sh, "handling_fee", default=0) or 0),
            "shipping_max_weight_kg": float(_f(sh, "max_weight_kg", default=0) or 0),
            "shipping_max_dimensions": _f(sh, "max_dimensions", default=""),

            "inspection_active": bool(_f(ins, "active")),
            "inspection_currency": _f(ins, "currency", default="USD"),
            "inspection_description": _f(ins, "description", default=""),
            "inspection_types": _f(ins, "types", default=""),
            "inspection_price_per_item": float(_f(ins, "price_per_item", default=0) or 0),
            "inspection_price_per_order": float(_f(ins, "price_per_order", default=0) or 0),
            "inspection_min_price": float(_f(ins, "min_price", default=0) or 0),
            "inspection_sampling_rate": _f(ins, "sampling_rate", default="10"),
            "inspection_photos": bool(_f(ins, "photos")),
            "inspection_photos_count": int(_f(ins, "photos_count", default=0) or 0),
            "inspection_video": bool(_f(ins, "video")),
            "inspection_report_pdf": bool(_f(ins, "report_pdf")),
            "inspection_report_language": _f(ins, "report_language", default=""),
            "inspection_defect_tolerance": _f(ins, "defect_tolerance", default="aql_2_5"),
            "inspection_corrective_action": bool(_f(ins, "corrective_action")),
            "inspection_corrective_price": float(_f(ins, "corrective_price", default=0) or 0),
            "inspection_certifications": _f(ins, "certifications", default=""),
            "last_sync_at": fields.Datetime.now(),
        }

        # Resolve carrier names → IDs
        carriers_raw = vals.pop("_carriers_raw", [])
        if isinstance(carriers_raw, list) and carriers_raw:
            Carrier = self.env["fulfillment.shipping.carrier"]
            carrier_ids = []
            for name in carriers_raw:
                c = Carrier.search([("name", "ilike", name)], limit=1)
                if not c:
                    c = Carrier.create({"name": name, "country_group": "int"})
                carrier_ids.append(c.id)
            vals["shipping_carrier_ids"] = [(6, 0, carrier_ids)]
        elif "_carriers_raw" in vals:
            vals.pop("_carriers_raw", None)

        if rec:
            rec.with_context(skip_api_sync=True).write(vals)
            # Rebuild box options
            rec.packing_option_ids.unlink()
        else:
            if is_partner:
                vals["partner_id"] = profile_or_partner.id
            else:
                vals["profile_id"] = profile_or_partner.id
            rec = self.with_context(skip_api_sync=True).create(vals)

        # Create box options
        box_opts = _f(p, "box_options", default=[])
        if isinstance(box_opts, list):
            PackingOpt = self.env["fulfillment.service.packing.option"]
            for opt in box_opts:
                PackingOpt.create({
                    "catalog_id": rec.id,
                    "name": opt.get("name") or "Box",
                    "length_cm": float(opt.get("length_cm") or 0),
                    "width_cm": float(opt.get("width_cm") or 0),
                    "height_cm": float(opt.get("height_cm") or 0),
                    "max_weight_kg": float(opt.get("max_weight_kg") or 0),
                    "material": opt.get("material") or "corrugated",
                    "fillers": opt.get("fillers") or "",
                    "price_per_unit": float(opt.get("price_per_unit") or 0),
                    "notes": opt.get("notes") or "",
                })
        return rec

    def action_push_to_api(self):
        for rec in self.filtered("is_mine"):
            if not rec.profile_id.fulfillment_profile_id:
                raise UserError(_("Profile is not connected to Fulfillment API yet."))
            client = FulfillmentAPIClient(rec.profile_id)
            try:
                client.service_catalog.upsert(
                    rec.profile_id.fulfillment_profile_id,
                    rec._to_api_dict(),
                )
                rec.with_context(skip_api_sync=True).write({"last_sync_at": fields.Datetime.now()})
            except FulfillmentAPIError as e:
                raise UserError(str(e)) from e
        return True


class FulfillmentServicePackingOption(models.Model):
    _name = "fulfillment.service.packing.option"
    _description = "Packing box configuration"
    _order = "sequence, id"

    def _valid_field_parameter(self, field, name):
        return name == 'placeholder' or super()._valid_field_parameter(field, name)

    catalog_id = fields.Many2one(
        "fulfillment.service.catalog", string="Catalog", required=True, ondelete="cascade"
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string="Box name / label", required=True,
                        placeholder="Small Box, Medium Box, Custom …")
    length_cm = fields.Float(string="Length (cm)", digits=(7, 1))
    width_cm = fields.Float(string="Width (cm)", digits=(7, 1))
    height_cm = fields.Float(string="Height (cm)", digits=(7, 1))
    max_weight_kg = fields.Float(string="Max weight (kg)", digits=(7, 2))
    material = fields.Selection(PACKING_MATERIAL, string="Box material", default="corrugated")
    fillers = fields.Char(string="Included fillers",
                           placeholder="Bubble wrap, Kraft paper, Air pillows …")
    price_per_unit = fields.Float(string="Price per box", digits=(16, 2))
    notes = fields.Char(string="Notes", placeholder="Available for fragile goods, requires 24h notice …")


class FulfillmentPartnersServiceCatalog(models.Model):
    _inherit = "fulfillment.partners"

    # ── Catalog relation ──────────────────────────────────────────────────
    catalog_ids = fields.One2many(
        "fulfillment.service.catalog", "partner_id", string="Service catalog"
    )

    # ── Capability flags (stored → filterable in list) ────────────────────
    has_packing = fields.Boolean(
        compute="_compute_capabilities", store=True, string="Offers packing"
    )
    has_storage = fields.Boolean(
        compute="_compute_capabilities", store=True, string="Offers storage"
    )
    has_shipping = fields.Boolean(
        compute="_compute_capabilities", store=True, string="Offers shipping"
    )
    has_inspection = fields.Boolean(
        compute="_compute_capabilities", store=True, string="Offers inspection"
    )

    @api.depends(
        "catalog_ids.packing_active",
        "catalog_ids.storage_active",
        "catalog_ids.shipping_active",
        "catalog_ids.inspection_active",
    )
    def _compute_capabilities(self):
        for partner in self:
            cat = partner.catalog_ids[:1]
            partner.has_packing = cat.packing_active if cat else False
            partner.has_storage = cat.storage_active if cat else False
            partner.has_shipping = cat.shipping_active if cat else False
            partner.has_inspection = cat.inspection_active if cat else False

    # ── Billing amounts (non-stored, computed on demand) ──────────────────
    usage_as_creditor_ids = fields.One2many(
        "fulfillment.service.usage", "creditor_partner_id", string="Services provided"
    )
    usage_as_debtor_ids = fields.One2many(
        "fulfillment.service.usage", "debtor_partner_id", string="Services received"
    )

    amount_i_owe = fields.Float(
        compute="_compute_billing_amounts",
        string="I owe them",
        digits=(16, 2),
        help="Sum of billing lines where I am debtor and this partner is creditor.",
    )
    amount_owed_to_me = fields.Float(
        compute="_compute_billing_amounts",
        string="They owe me",
        digits=(16, 2),
        help="Sum of billing lines where this partner is debtor and I am creditor.",
    )

    def _compute_billing_amounts(self):
        profile = self.env["fulfillment.profile"].search([], limit=1)
        my_fid = (profile.fulfillment_profile_id or "").strip() if profile else ""
        for partner in self:
            fid = (partner.fulfillment_id or "").strip()
            if not my_fid or not fid:
                partner.amount_i_owe = 0.0
                partner.amount_owed_to_me = 0.0
                continue
            Usage = self.env["fulfillment.service.usage"].sudo()
            owe = Usage.search([
                ("debtor_fulfillment_id", "=", my_fid),
                ("creditor_fulfillment_id", "=", fid),
                ("status", "!=", "void"),
            ])
            recv = Usage.search([
                ("creditor_fulfillment_id", "=", my_fid),
                ("debtor_fulfillment_id", "=", fid),
                ("status", "!=", "void"),
            ])
            partner.amount_i_owe = sum(owe.mapped("line_total"))
            partner.amount_owed_to_me = sum(recv.mapped("line_total"))

    def action_open_billing_owe(self):
        self.ensure_one()
        profile = self.env["fulfillment.profile"].search([], limit=1)
        my_fid = (profile.fulfillment_profile_id or "").strip() if profile else ""
        return {
            "type": "ir.actions.act_window",
            "name": f"I owe — {self.name}",
            "res_model": "fulfillment.service.usage",
            "view_mode": "list,form",
            "domain": [
                ("debtor_fulfillment_id", "=", my_fid),
                ("creditor_fulfillment_id", "=", self.fulfillment_id),
            ],
        }

    def action_view_service_catalog(self):
        self.ensure_one()
        catalog = self.env["fulfillment.service.catalog"].search(
            [("partner_id", "=", self.id)], limit=1
        )
        if not catalog:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "No catalog",
                    "message": "No service catalog synced yet. Click 'Sync service catalog' first.",
                    "type": "warning",
                    "sticky": False,
                },
            }
        readonly_view = self.env.ref(
            "fulfillment_software.view_fulfillment_service_catalog_form_readonly",
            raise_if_not_found=False,
        )
        return {
            "type": "ir.actions.act_window",
            "name": f"{self.name} — Service Catalog",
            "res_model": "fulfillment.service.catalog",
            "view_mode": "form",
            "views": [(readonly_view.id if readonly_view else False, "form")],
            "res_id": catalog.id,
            "target": "new",
        }

    def action_sync_service_catalog_from_api(self):
        profile = self.env["fulfillment.partners"]._get_active_profile()
        if not profile or not profile.fulfillment_profile_id:
            raise UserError(_("Configure Fulfillment profile first."))
        client = FulfillmentAPIClient(profile)
        Catalog = self.env["fulfillment.service.catalog"].sudo()
        for partner in self:
            if not partner.fulfillment_id:
                continue
            try:
                resp = client.service_catalog.get(partner.fulfillment_id.strip())
            except FulfillmentAPIError as e:
                raise UserError(str(e)) from e
            api_data = resp.get("data", {})
            if not isinstance(api_data, dict):
                continue
            Catalog._from_api_dict(partner, api_data, is_partner=True)
        return True
