# -*- coding: utf-8 -*-
import logging
from datetime import datetime
from odoo import models, api
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError

_logger = logging.getLogger(__name__)


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

   
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info(f"[create]")
        warehouses = super().create(vals_list)

        for wh in warehouses:
            try:
                self._update_return_types_for_warehouse(wh)
            except Exception as e:
                _logger.error("[Warehouse][CREATE][ERROR] %s", e)

        return warehouses

    
    def write(self, vals):
        _logger.info(f"[write]")
        res = super().write(vals)
        trigger_fields = {
            "fulfillment_warehouse_id",
            "partner_id",
            "company_id",
            "fulfillment_owner_id",
            "fulfillment_client_id",
        }

        if trigger_fields.intersection(vals.keys()):
            changed = trigger_fields.intersection(vals.keys())

            for wh in self:
                _logger.info(
                    "[Warehouse][WRITE] Recomputing picking types for %s (changed: %s)",
                    wh.name,
                    list(changed),
                )
                try:
                    self._update_return_types_for_warehouse(wh)
                except Exception as e:
                    _logger.error("[Warehouse][WRITE][ERROR] %s", e)

        return res

    def _update_return_types_for_warehouse(self, warehouse):
        _logger.info(f"[_update_return_types_for_warehouse]")

        picking_types = self.env["stock.picking.type"].search([
            ('code', '=', 'outgoing'),
            ('warehouse_id', '=', warehouse.id),
        ])

        if not picking_types:
            _logger.warning("[Warehouse][NO OUT] No outgoing picking types for warehouse %s", warehouse.name)
            return

        warehouse_location = warehouse.lot_stock_id
        relevant_picking_types = picking_types.filtered(
            lambda pt: pt.default_location_src_id == warehouse_location
        )

        if not relevant_picking_types:
            _logger.warning("[Warehouse][NO RELEVANT OUT] No outgoing picking types with source location for warehouse %s", warehouse.name)
            return

        return_type = relevant_picking_types[:1]

        for pt in relevant_picking_types:
            pt.return_picking_type_id = return_type.id
            _logger.info(
                "[Warehouse][SET] %s → return_picking_type_id = %s",
                pt.name,
                return_type.name
            )

    @api.model
    def import_warehouses(self, fulfillment_partner):
        """Import warehouses from the API for a given fulfillment partner.

        After importing, automatically registers any local own warehouses
        (those without a fulfillment_warehouse_id) so they are visible in
        the API and can be used as warehouse_out in outgoing transfers.
        """
        _logger.info("[import_warehouses] partner=%s", fulfillment_partner.name)
        try:
            profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            if not profile or not profile.fulfillment_api_key:
                _logger.error("[import_warehouses] No active fulfillment profile with API key")
                return
            client = FulfillmentAPIClient(profile)
            response = client.fulfillment.list_warehouses(fulfillment_partner.fulfillment_id)
            warehouses_data = response.get("data") or []

            existing = self.search([("fulfillment_warehouse_id", "in", [w.get("id") for w in warehouses_data])])
            existing_map = {w.fulfillment_warehouse_id: w for w in existing}

            for wh in warehouses_data:
                try:
                    wh_id = wh.get("id")
                    _logger.info("[import_warehouses] Processing %s (%s)", wh.get("name"), wh_id)

                    warehouse = existing_map.get(wh_id)

                    # Generate a unique code (avoid collisions)
                    code = wh.get("code") or wh.get("short_name") or wh.get("name") or "WH"
                    original_code = code
                    suffix = 1
                    while self.search_count([("code", "=", code), ("id", "!=", warehouse.id if warehouse else 0)]):
                        code = f"{original_code}_{suffix}"
                        suffix += 1

                    # Use the fulfillment partner's name as warehouse name (neutral, no arrows)
                    base_name = fulfillment_partner.partner_id.name or wh.get("name") or "Fulfillment"
                    unique_name = base_name
                    suffix = 1
                    while self.search_count([("name", "=", unique_name), ("id", "!=", warehouse.id if warehouse else 0)]):
                        unique_name = f"{base_name} ({suffix})"
                        suffix += 1

                    vals = {
                        "name": unique_name,
                        "code": code,
                        "fulfillment_warehouse_id": wh_id,
                        "active": True,
                    }

                    if warehouse:
                        warehouse.with_context(skip_api_sync=True, from_fulfillment_import=True).write(vals)
                    else:
                        warehouse = self.with_context(skip_api_sync=True, from_fulfillment_import=True).create(vals)

                    parent_partner = fulfillment_partner.partner_id
                    child_contact, _ = warehouse._get_or_create_warehouse_contact(parent_partner, warehouse.name)
                    if child_contact:
                        warehouse.with_context(skip_api_sync=True, from_fulfillment_import=True).write(
                            {"partner_id": child_contact.id}
                        )

                    owner_fp = self.env["fulfillment.partners"].search(
                        [("fulfillment_id", "=", wh.get("fulfillment_id"))], limit=1
                    )
                    client_fp = False
                    if wh.get("fulfillment_client_id") and wh.get("fulfillment_client_id") != wh.get("fulfillment_id"):
                        client_fp = self.env["fulfillment.partners"].search(
                            [("fulfillment_id", "=", wh.get("fulfillment_client_id"))], limit=1
                        )
                    else:
                        _logger.warning("[import_warehouses] owner == client for %s", wh.get("name"))

                    warehouse.with_context(skip_api_sync=True, from_fulfillment_import=True).write({
                        "fulfillment_owner_id": owner_fp.id if owner_fp else False,
                        "fulfillment_client_id": client_fp.id if client_fp else False,
                        "last_update": datetime.now(),
                    })

                    if child_contact:
                        child_contact.with_context(skip_api_sync=True).write({
                            "fulfillment_warehouse_id": wh_id,
                            "linked_warehouse_id": warehouse.id,
                        })

                    _logger.info("[import_warehouses] Done: %s (%s)", warehouse.name, wh_id)

                except Exception as e:
                    _logger.exception("[import_warehouses] Error processing %s: %s", wh, e)
                    self.env.cr.rollback()

            # After importing partner warehouses, ensure our own warehouses are
            # registered in the API so they appear as warehouse_out in transfers.
            self._register_own_warehouses(profile, client)

            _logger.info("[import_warehouses] Done for partner %s", fulfillment_partner.name)

        except Exception as e:
            _logger.exception("[import_warehouses] Fatal error: %s", e)
            self.env.cr.rollback()

    @api.model
    def _register_own_warehouses(self, profile, client):
        """Register local warehouses that have no fulfillment_warehouse_id into the API.

        This makes them visible as warehouse_out when the handler sends stock
        to a fulfillment partner warehouse.
        """
        my_fulfillment_id = profile.fulfillment_profile_id
        if not my_fulfillment_id:
            return

        my_fp = self.env['fulfillment.partners'].search(
            [('fulfillment_id', '=', my_fulfillment_id)], limit=1
        )

        unregistered = self.search([
            ('fulfillment_warehouse_id', '=', False),
            ('active', '=', True),
        ])

        for wh in unregistered:
            try:
                _logger.info("[_register_own_warehouses] Registering %s in API", wh.name)
                payload = {
                    "name": wh.name,
                    "code": wh.code or wh.name,
                    "short_name": (wh.code or wh.name or "")[:50].upper(),
                    "location": (wh.partner_id.city or "") if wh.partner_id else "",
                    "fulfillment_client_id": my_fulfillment_id,
                }
                response = client.warehouse.create(
                    fulfillment_id=my_fulfillment_id,
                    payload=payload,
                )
                data = response.get("data") or {}
                api_wh_id = data.get("id")
                if not api_wh_id:
                    _logger.warning("[_register_own_warehouses] No id in API response for %s", wh.name)
                    continue

                wh.with_context(skip_api_sync=True, from_fulfillment_import=True).write({
                    "fulfillment_warehouse_id": api_wh_id,
                    "fulfillment_owner_id": my_fp.id if my_fp else False,
                    "fulfillment_client_id": my_fp.id if my_fp else False,
                    "last_update": datetime.now(),
                })
                _logger.info("[_register_own_warehouses] Registered %s → %s", wh.name, api_wh_id)

            except FulfillmentAPIError as e:
                _logger.error("[_register_own_warehouses] API error for %s: %s", wh.name, e)
            except Exception as e:
                _logger.exception("[_register_own_warehouses] Unexpected error for %s: %s", wh.name, e)

    @api.model
    def _get_or_create_warehouse_contact(self, parent_partner, warehouse_name):
        """Find or create a child contact for this warehouse under the given parent partner."""
        if not parent_partner or not parent_partner.exists():
            return False, None

        child_name = f"{parent_partner.name} ({warehouse_name})"
        child = self.env['res.partner'].search([
            ('parent_id', '=', parent_partner.id),
            ('name', '=', child_name),
        ], limit=1)

        if child:
            fp = self.env['fulfillment.partners'].search([('partner_id', '=', child.id)], limit=1)
            return child, fp

        tag = self.env['res.partner.category'].search([('name', '=', 'Warehouse')], limit=1)
        if not tag:
            tag = self.env['res.partner.category'].create({'name': 'Warehouse'})

        vals = {
            'name': child_name,
            'parent_id': parent_partner.id,
            'type': 'delivery',
            'is_company': False,
            'category_id': [(6, 0, [tag.id])],
        }
        if parent_partner.country_id:
            vals['country_id'] = parent_partner.country_id.id

        child = self.env['res.partner'].with_context(
            skip_api_sync=True, skip_warehouse_contact=True
        ).create(vals)

        fp = self.env['fulfillment.partners'].search([('partner_id', '=', child.id)], limit=1)
        return child, fp



