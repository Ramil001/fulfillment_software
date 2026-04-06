# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError
from datetime import datetime
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    is_fulfillment = fields.Boolean(string="Fulfillment storage", compute="_compute_is_fulfillment", store=True)
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Creator fulfillment Id", readonly=True)
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Client fulfillment Id", readonly=True)
    fulfillment_warehouse_id = fields.Char(string="Fulfillment warehouse Id", readonly=True)
    last_update = fields.Datetime(string='Last Update', readonly=True)
    
    
    # ===== Onchange handler ===== 
    @api.onchange('partner_id')
    def _onchange_partner(self):
        _logger.info(f"[_onchange_partner]")
        if not self.partner_id:
            return

        partner = self.partner_id
        warehouse_name = partner.name or "(new partner)"

        if self.env['fulfillment.utils'].is_partner_fulfillment(partner.id):
            title = "Fulfillment Warehouse"
            message = f"This partner ({partner.display_name}) is managed via Fulfillment."
            try:
                self.env['bus.utils'].send_notification(
                    title=title,
                    message=message,
                    level="info",
                    sticky=False,
                )
                _logger.info("[BUS] Fulfillment notification successfully sent.")
            except Exception as e:
                _logger.exception(f"[BUS][ERROR] Error sending notification: {e}")

        self.name = warehouse_name

        
    
    
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info(f"[create]")
        
        created_warehouses = super().create(vals_list)
        
        profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
        if not profile or not profile.fulfillment_api_key:
            _logger.warning("[WAREHOUSE][CREATE] No active fulfillment.profile with API key found — skipping API sync for created warehouses")
            return created_warehouses

        owner_fulfillment_id = getattr(profile, 'fulfillment_profile_id', False)
        if not owner_fulfillment_id:
            _logger.warning("[WAREHOUSE][CREATE] fulfillment_profile_id missing in profile — skipping API sync")
            return created_warehouses

        client = FulfillmentAPIClient(profile)

        for warehouse in created_warehouses:
            try:
                _logger.info("[WAREHOUSE][CREATE][PROCESS] id=%s name=%s partner=%s", warehouse.id, warehouse.name, bool(warehouse.partner_id))

                parent_partner = warehouse.partner_id.parent_id or warehouse.partner_id
                child_contact = None
                fulfillment_partner_obj = None 

                if parent_partner:
                    child_contact, fulfillment_partner_obj = self._get_or_create_warehouse_contact(parent_partner, warehouse.name)

                    if child_contact:
                        try:
                            warehouse.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({'partner_id': child_contact.id})
                            _logger.info("[WAREHOUSE][CREATE] Relinked warehouse %s → child partner %s", warehouse.id, child_contact.id)
                        except Exception as e:
                            _logger.exception("[WAREHOUSE][CREATE] Failed to relink partner for warehouse %s: %s", warehouse.id, e)

                customer_fulfillment_id = None
                if child_contact and getattr(child_contact, 'fulfillment_partner_id', False):
                    customer_fulfillment_id = child_contact.fulfillment_partner_id
                    _logger.debug("[WAREHOUSE][CREATE] Using customer_fulfillment_id from child_contact: %s", customer_fulfillment_id)
                elif fulfillment_partner_obj and getattr(fulfillment_partner_obj, 'fulfillment_id', False):
                    customer_fulfillment_id = fulfillment_partner_obj.fulfillment_id
                    _logger.debug("[WAREHOUSE][CREATE] Using customer_fulfillment_id from fulfillment.partners object: %s", customer_fulfillment_id)
                elif parent_partner and getattr(parent_partner, 'fulfillment_partner_id', False):
                    customer_fulfillment_id = parent_partner.fulfillment_partner_id
                    _logger.debug("[WAREHOUSE][CREATE] Using customer_fulfillment_id from parent_partner: %s", customer_fulfillment_id)

                if not customer_fulfillment_id:
                    _logger.warning("[WAREHOUSE][CREATE] No customer_fulfillment_id for warehouse %s (partner=%s) — skipping API create", warehouse.name, parent_partner.id if parent_partner else None)
                    continue

                payload = {
                    "name": warehouse.name,
                    "code": warehouse.code,
                    "location": (warehouse.partner_id.city or "") if warehouse.partner_id else "",
                    "short_name": (warehouse.code or warehouse.name or "")[:50].upper(),  # короткое имя, безопасно усечь
                    "fulfillment_client_id": customer_fulfillment_id,
                }

                _logger.info("[WAREHOUSE][CREATE][API] POST → fulfillment_id=%s payload=%s", owner_fulfillment_id, payload)

                
                try:
                    response = client.warehouse.create(
                        fulfillment_id=owner_fulfillment_id,
                        payload=payload
                    )
                except FulfillmentAPIError as e:
                    _logger.error("Fulfillment API error on create for warehouse %s: %s", warehouse.name, e)
                    continue
                except Exception as e:
                    _logger.exception("Unexpected error calling API for warehouse %s: %s", warehouse.name, e)
                    continue

                
                data = response["data"]
                owner_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('fulfillment_id'))], limit=1)
                client_fp = None
                if data.get('fulfillment_client_id') and data.get('fulfillment_client_id') != data.get('fulfillment_id'):
                    client_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', data.get('fulfillment_client_id'))], limit=1)
                else:
                    _logger.warning("[WAREHOUSE][CREATE][API] owner_fulfillment_id == fulfillment_client_id for warehouse %s (api returned same id)", warehouse.name)

                try:
                    warehouse.with_context(
                        skip_api_sync=True,
                        skip_warehouse_contact=True,
                        from_fulfillment_import=True
                    ).write({
                        'fulfillment_owner_id': owner_fp.id if owner_fp else False,
                        'fulfillment_client_id': client_fp.id if client_fp else False,
                        'fulfillment_warehouse_id': data.get('id'),
                        'last_update': datetime.now(),
                    })
                except Exception as e:
                    _logger.exception("[WAREHOUSE][CREATE] Failed to write API IDs to warehouse %s: %s", warehouse.id, e)

                if child_contact:
                    try:
                        child_contact.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'fulfillment_warehouse_id': data.get('id'),
                            'linked_warehouse_id': warehouse.id,
                        })
                        _logger.info("[WAREHOUSE][CREATE] Child contact %s updated with fulfillment_warehouse_id=%s", child_contact.id, data.get('id'))
                    except Exception as e:
                        _logger.exception("[WAREHOUSE][CREATE] Failed to update child contact %s with warehouse id: %s", child_contact.id if child_contact else None, e)

                try:
                    if parent_partner:
                        parent_partner.with_context(skip_api_sync=True, skip_warehouse_contact=True).write({
                            'fulfillment_warehouse_id': data.get('id'),
                        })
                except Exception as e:
                    _logger.exception("[WAREHOUSE][CREATE] Failed to update parent partner %s with warehouse id: %s", parent_partner.id if parent_partner else None, e)

                
                
                if client_fp and client_fp.fulfillment_id:
                    self.env['send.action'].push_update(client_fp.fulfillment_id)
                    _logger.info(f"[SEND ACTION]: Отправка на фулфиллмент партнера {client_fp.fulfillment_id} ")
                

                else:
                    _logger.warning("[WAREHOUSE][CREATE][API] unexpected response for %s: %s", warehouse.name, response)

            except Exception as e:
                _logger.exception("[WAREHOUSE][CREATE] Unexpected error processing warehouse %s: %s", getattr(warehouse, 'id', None), e)
                
        _logger.info("[WAREHOUSE][CREATE][DONE] processed %s warehouses", len(created_warehouses))
        return created_warehouses

    def write(self, vals):
        _logger.info(f"[write]")

        if not self.env.context.get("from_fulfillment_import"):
            for wh in self:
                if not self._is_warehouse_creator(wh.id):
                    raise UserError("You are not the owner of this warehouse and cannot edit it.")

        if self.env.context.get('skip_api_sync'):
            _logger.info(f"[WAREHOUSE][WRITE][SKIP_API_SYNC] ids={self.ids}")
            vals['last_update'] = datetime.now()
            res = super().write(vals)
            return res

        if self.env.context.get('skip_import_warehouses'):
            vals['last_update'] = datetime.now()
            res = super().write(vals)
            _logger.info(f"[WAREHOUSE][WRITE][SKIP_IMPORT] ids={self.ids}")
            return res

        res = super().write(vals)

        try:
            profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            if not profile or not profile.fulfillment_api_key:
                _logger.warning("[Logger][Warning]: No active profile — skip API write")
                return res

            client = FulfillmentAPIClient(profile)

            for record in self:
                if not record.fulfillment_warehouse_id:
                    _logger.warning("[Logger][Warning]: Warehouse %s does not have a fulfillment_warehouse_id — API update is not possible", record.name)
                    continue

                partner = record.partner_id
                if partner and not partner.fulfillment_partner_id and partner.parent_id:
                    partner = partner.parent_id

                if not partner or not partner.fulfillment_partner_id:
                    _logger.warning("[Logger][Warning]: The warehouse %s does not have a partner with fulfillment_partner_id", record.name)
                    continue
                payload = {
                    "name": vals.get("name", record.name),
                    "code": vals.get("code", record.code),
                    "location": vals.get("location", record.partner_id.city or ""),
                    "short_name": vals.get("short_name", record.code.upper() if record.code else record.name),
                    "fulfillment_client_id": partner.fulfillment_partner_id,
                }
                _logger.info(f"[WAREHOUSE][WRITE][API] PUT → warehouse_id={record.fulfillment_warehouse_id} payload={payload}")

                response = client.warehouse.update(
                    fulfillment_id=record.fulfillment_owner_id.fulfillment_id,
                    warehouse_id=record.fulfillment_warehouse_id,
                    payload=payload
                )

                data = response["data"]

                owner_partner = self.env['fulfillment.partners'].search(
                    [('fulfillment_id', '=', data.get('fulfillment_id'))], limit=1
                )
                client_partner = None
                if data.get('fulfillment_client_id') != data.get('fulfillment_id'):
                    client_partner = self.env['fulfillment.partners'].search(
                        [('fulfillment_id', '=', data.get('fulfillment_client_id'))], limit=1
                    )
                else:
                    _logger.warning(f"[Logger][Warning]: The API returned identical fulfillment_id and fulfillment_client_id for the warehouse.{record.name}")


                record.with_context(
                    skip_import_warehouses=True,
                    from_fulfillment_import=True
                ).write({
                    'fulfillment_owner_id': owner_partner.id if owner_partner else False,
                    'fulfillment_client_id': client_partner.id if client_partner else False,
                    'fulfillment_warehouse_id': data.get('id'),
                    'last_update': datetime.now(),
                })

                _logger.info(f"[Logger][Info]: Warehouse {record.name} updated in API (ID {data.get("warehouse_id")})")
                
                if partner.fulfillment_partner_id and partner.fulfillment_partner_id:
                    self.env['send.action'].push_update(partner.fulfillment_partner_id)
                    _logger.info(f"[SEND ACTION]: Отправка на фулфиллмент партнера {partner.fulfillment_partner_id} ")

        except FulfillmentAPIError as e:
            _logger.error(f"[Logger][Error]: API error when updating the warehouse: {str(e)}")
        except Exception as e:
            _logger.error(f"[Logger][Error]: Unexpected error during warehouse update: {str(e)}")

        vals['last_update'] = datetime.now()
        _logger.info(f"[WAREHOUSE][WRITE][DONE] ids={self.ids}")
        return res


  
    @api.model
    def import_warehouses(self, fulfillment_partner):
        _logger.info(f"[import_warehouses]")
        try:
            profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            if not profile or not profile.fulfillment_api_key:
                _logger.error("[Logger][Error]: [IMPORT][WAREHOUSES] Not Fulfillment API Key")
                return
            client = FulfillmentAPIClient(profile)
            response = client.fulfillment.list_warehouses(fulfillment_partner.fulfillment_id)
            _logger.info("[Logger][Info]:[IMPORT][WAREHOUSES] API response: %s", response)


            warehouses = response.get("data")
            if not warehouses:
                _logger.error("[IMPORT][WAREHOUSES] Empty or invalid API response: %s", response)
                return


            existing = self.search([("fulfillment_warehouse_id", "in", [w.get("id") for w in warehouses])])
            existing_map = {w.fulfillment_warehouse_id: w for w in existing}

            profile_fid = getattr(profile, 'fulfillment_profile_id', None)

            for wh in warehouses:
                try:
                    wh_id = wh.get("id")
                    _logger.info("[Logger][Info]: [IMPORT][WAREHOUSE] >>> Processing %s (%s)", wh.get("name"), wh_id)

                    # Skip warehouses that belong to us — they are our own registered warehouses
                    # (e.g. WH registered via _register_own_warehouses). We don't want to rename them.
                    if profile_fid and wh.get("fulfillment_id") == profile_fid:
                        _logger.info("[IMPORT][WAREHOUSE] Skipping own warehouse %s", wh.get("name"))
                        continue

                    warehouse = existing_map.get(wh_id)

                    code = wh.get("code") or wh.get("short_name") or wh.get("name") or "WH"
                    original_code = code
                    suffix = 1
                    while self.search_count([("code", "=", code), ("id", "!=", warehouse.id if warehouse else 0)]):
                        code = f"{original_code}_{suffix}"
                        suffix += 1

                    # Use the fulfillment partner's name as the warehouse name for clarity.
                    # The API may return names with arrows (e.g. "Händler ⮕ Fulfillment") which
                    # are confusing from the handler's perspective. We prefer the partner name.
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
                        _logger.info("[Logger][Info]: [IMPORT][WAREHOUSE] Updating existing %s", warehouse.id)
                        warehouse.with_context(
                            skip_api_sync=True,
                            from_fulfillment_import=True # Flag for import 
                        ).write(vals)

                    else:
                        _logger.info("[Logger][Info]: [IMPORT][WAREHOUSE] Creating new warehouse")
                        warehouse = self.with_context(skip_api_sync=True, from_fulfillment_import=True).create(vals)

                    parent_partner = fulfillment_partner.partner_id
                    child_contact, _ = warehouse._get_or_create_warehouse_contact(parent_partner, warehouse.name)

                    if child_contact:
                        warehouse.with_context(
                        skip_api_sync=True,
                        from_fulfillment_import=True
                    ).write({"partner_id": child_contact.id
                    })

                    owner_fp = self.env["fulfillment.partners"].search(
                        [("fulfillment_id", "=", wh.get("fulfillment_id"))],
                        limit=1
                    )

                    client_fp = self.env["fulfillment.partners"].search(
                        [("fulfillment_id", "=", wh.get("fulfillment_client_id"))],
                        limit=1
                    ) if wh.get("fulfillment_client_id") else False

                    if wh.get("fulfillment_client_id") != wh.get("fulfillment_id"):
                        client_fp = self.env["fulfillment.partners"].search([("fulfillment_id", "=", wh.get("fulfillment_client_id"))], limit=1)
                    else:
                        _logger.warning(f"[Logger][Warning]: The API returned identical fulfillment_id and fulfillment_client_id for the warehouse {wh.get('name')}")

                    warehouse.with_context(
                        skip_api_sync=True,
                        from_fulfillment_import=True
                    ).write({
                        "fulfillment_owner_id": owner_fp.id if owner_fp else False,
                        "fulfillment_client_id": client_fp.id if client_fp else False,
                    })


                    if child_contact:
                        child_contact.with_context(skip_api_sync=True).write({
                            "fulfillment_warehouse_id": wh_id,
                            "linked_warehouse_id": warehouse.id,
                        })

                    _logger.info(f"[Logger][Info]: Imported warehouse {warehouse.name} ({wh_id})")

                except Exception as e:
                    _logger.exception(f"[Logger][Exception]: [IMPORT][WAREHOUSE] Error while processing {wh}: {str(e)}")
                    self.env.cr.rollback()

            _logger.info(
                "[IMPORT][WAREHOUSES][DONE] Imported: %s",
                len(warehouses)
            )

            # After importing partner warehouses, register any of our own local
            # warehouses that are not yet known to the API (e.g. the main "WH").
            try:
                self._register_own_warehouses(profile, client)
            except Exception as e:
                _logger.exception("[IMPORT][WAREHOUSES] _register_own_warehouses failed: %s", e)

        except Exception as e:
            _logger.exception(f"[Logger][Exception]: [IMPORT][WAREHOUSES] Fatal error: {str(e)}")
            self.env.cr.rollback()

    @api.model
    def _register_own_warehouses(self, profile, client):
        """Register local warehouses that have no fulfillment_warehouse_id into the API.

        This makes them visible as warehouse_out when the handler sends stock
        to a fulfillment partner warehouse.
        """
        my_fulfillment_id = getattr(profile, 'fulfillment_profile_id', None)
        if not my_fulfillment_id:
            _logger.warning("[_register_own_warehouses] No fulfillment_profile_id in profile")
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
                _logger.info("[_register_own_warehouses] Registering '%s' in API", wh.name)
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
                    _logger.warning("[_register_own_warehouses] No id in API response for '%s'", wh.name)
                    continue

                wh.with_context(skip_api_sync=True, from_fulfillment_import=True).write({
                    'fulfillment_warehouse_id': api_wh_id,
                    'fulfillment_owner_id': my_fp.id if my_fp else False,
                    'fulfillment_client_id': my_fp.id if my_fp else False,
                    'last_update': datetime.now(),
                })
                _logger.info("[_register_own_warehouses] Registered '%s' → %s", wh.name, api_wh_id)

            except FulfillmentAPIError as e:
                _logger.error("[_register_own_warehouses] API error for '%s': %s", wh.name, e)
            except Exception as e:
                _logger.exception("[_register_own_warehouses] Unexpected error for '%s': %s", wh.name, e)

    def _is_warehouse_creator(self, warehouse_id):
        _logger.info(f"[_is_warehouse_creator]")
        warehouse = self.browse(warehouse_id)
        if not warehouse.exists():
            raise UserError("Warehouse not found")

        if not warehouse.fulfillment_owner_id:
            _logger.debug(f"_is_warehouse_creator: warehouse {warehouse.id} has no owner → True")
            return True

        profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
        if not profile or not profile.fulfillment_profile_id:
            _logger.debug(f"_is_warehouse_creator: current profile missing → True")
            return True

        owner_fulfillment_id = getattr(warehouse.fulfillment_owner_id.sudo(), 'fulfillment_id', False)
        is_owner = owner_fulfillment_id == profile.fulfillment_profile_id

        
        return is_owner





    @api.model
    def _get_or_create_warehouse_contact(self, parent_partner, warehouse_name):
        if not parent_partner or not parent_partner.exists():
            return False, None

        child_name = f"{parent_partner.name} ({warehouse_name})"
        _logger.info(f"[CONTACT][LOOKUP] parent={parent_partner.id}, name={child_name}")

        child = self.env['res.partner'].search([
            ('parent_id', '=', parent_partner.id),
            ('name', '=', child_name)
        ], limit=1)

        if child:
            _logger.info(f"[CONTACT][FOUND] child {child.id} for {child_name}")
            fulfillment_partner = self.env['fulfillment.partners'].search([
                ('partner_id', '=', child.id)
            ], limit=1)
            return child, fulfillment_partner

        tag = self.env['res.partner.category'].search([('name', '=', 'Warehouse')], limit=1)
        if not tag:
            tag = self.env['res.partner.category'].create({'name': 'Warehouse'})

        vals = {
            'name': child_name,
            'parent_id': parent_partner.id,
            'type': 'delivery',
            'is_company': False,
            'category_id': [(6, 0, [tag.id])]
        }
        if parent_partner.country_id:
            vals['country_id'] = parent_partner.country_id.id

        _logger.info(f"[CONTACT][CREATE] {vals}")
        child = self.env['res.partner'].with_context(skip_api_sync=True, skip_warehouse_contact=True).create(vals)

        fulfillment_partner = self.env['fulfillment.partners'].search([
            ('partner_id', '=', child.id)
        ], limit=1)

        return child, fulfillment_partner

    @api.depends("partner_id", "partner_id.parent_id", "partner_id.category_id")
    def _compute_is_fulfillment(self):
        _logger.info(f"[_compute_is_fulfillment]")
        for warehouse in self:
            try:
                partner = warehouse.partner_id
                is_fulfillment = False

                if not partner:
                    warehouse.is_fulfillment = False
                    continue

                parent = partner.parent_id or partner

                if getattr(parent, "fulfillment_contact_warehouse_id", False):
                    is_fulfillment = True

                elif getattr(parent, "category_id", False):
                    if any(c.name == "Fulfillment" for c in parent.category_id):
                        is_fulfillment = True

                warehouse.is_fulfillment = is_fulfillment

            except Exception as e:
                warehouse.is_fulfillment = False
                _logger.error(
                    "[Fulfillment] Ошибка при вычислении is_fulfillment для склада '%s': %s",
                    warehouse.display_name or warehouse.name, e,
                )
