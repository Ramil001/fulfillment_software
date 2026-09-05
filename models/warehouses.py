# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError
from datetime import datetime
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


def _normalize_fulfillment_external_id(val):
    """API / DB may mix int and str for the same id — normalize for lookups."""
    if val is None or val is False:
        return False
    return str(val).strip()


class FulfillmentWarehouses(models.Model):
    _inherit = 'stock.warehouse'

    is_fulfillment = fields.Boolean(string="Fulfillment storage", compute="_compute_is_fulfillment", store=True)
    fulfillment_owner_id = fields.Many2one('fulfillment.partners', string="Creator fulfillment Id", readonly=True)
    fulfillment_client_id = fields.Many2one('fulfillment.partners', string="Client fulfillment Id", readonly=True)
    fulfillment_warehouse_id = fields.Char(string="Fulfillment warehouse Id", readonly=True)
    last_update = fields.Datetime(string='Last Update', readonly=True)
    fulfillment_network_partner_id = fields.Many2one(
        'fulfillment.partners',
        string='Fulfillment network partner',
        help='When you create a warehouse for a fulfillment client, select the partner '
             'from Fulfillment → Partners. The warehouse will be registered in the API for that account. '
             'If empty, the address partner on the warehouse is used.',
        copy=False,
    )
    
    
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
        _logger.info("[WAREHOUSE][CREATE] Starting batch create for %s records", len(vals_list))
        created_warehouses = super().create(vals_list)

        # Fast-exit checks
        profile = self.env['fulfillment.profile'].sudo().search([('fulfillment_api_key', '!=', False)], limit=1)
        owner_fulfillment_id = getattr(profile, 'fulfillment_profile_id', False) if profile else False
        
        if not profile or not owner_fulfillment_id:
            _logger.warning("[WAREHOUSE][CREATE] No active profile or missing fulfillment_profile_id — skipping API sync")
            return created_warehouses

        client = FulfillmentAPIClient(profile)
        # Создаем базовый контекст без циклической синхронизации
        sync_context = {'skip_api_sync': True, 'skip_warehouse_contact': True, 'from_fulfillment_import': True}

        for warehouse in created_warehouses:
            try:
                _logger.info("[WAREHOUSE][CREATE][PROCESS] id=%s name=%s", warehouse.id, warehouse.name)

                # 1. Определение родительского партнера
                parent_partner = (
                    warehouse.fulfillment_network_partner_id.partner_id 
                    if warehouse.fulfillment_network_partner_id and warehouse.fulfillment_network_partner_id.partner_id 
                    else (warehouse.partner_id.parent_id or warehouse.partner_id)
                )

                # 2. Поиск или создание контакта склада
                selected_partner = warehouse.partner_id
                is_warehouse_contact = selected_partner and (
                    selected_partner.linked_warehouse_id or 
                    any(c.name == 'Warehouse' for c in selected_partner.category_id)
                )

                child_contact = None
                fulfillment_partner_obj = None

                if is_warehouse_contact:
                    child_contact = selected_partner
                    fulfillment_partner_obj = self.env['fulfillment.partners'].search([('partner_id', '=', child_contact.id)], limit=1)
                    _logger.info("[WAREHOUSE][CREATE] Reusing existing contact %s", child_contact.id)
                elif parent_partner:
                    child_contact, fulfillment_partner_obj = self._get_or_create_warehouse_contact(
                        parent_partner, warehouse.name, warehouse=warehouse
                    )
                    if child_contact:
                        warehouse.with_context(sync_context).write({'partner_id': child_contact.id})

                # 3. Извлечение customer_fulfillment_id
                customer_fulfillment_id = (
                    getattr(child_contact, 'fulfillment_partner_id', False) or
                    getattr(fulfillment_partner_obj, 'fulfillment_id', False) or
                    getattr(parent_partner, 'fulfillment_partner_id', False)
                )

                if not customer_fulfillment_id:
                    _logger.warning("[WAREHOUSE][CREATE] No customer_fulfillment_id for warehouse %s — skipping API", warehouse.name)
                    continue

                # 4. Вызов API
                payload = {
                    "name": warehouse.name,
                    "code": warehouse.code,
                    "location": warehouse.partner_id.city or "" if warehouse.partner_id else "",
                    "short_name": (warehouse.code or warehouse.name or "")[:50].upper(),
                    "fulfillment_client_id": customer_fulfillment_id,
                }

                try:
                    response = client.warehouse.create(fulfillment_id=owner_fulfillment_id, payload=payload)
                except (FulfillmentAPIError, Exception) as e:
                    _logger.exception("[WAREHOUSE][CREATE] API call failed for %s: %s", warehouse.name, e)
                    continue

                data = response.get("data")
                if not data or not isinstance(data, dict):
                    _logger.warning("[WAREHOUSE][CREATE][API] Invalid/empty payload received for %s: %s", warehouse.name, response)
                    continue

                # 5. Сопоставление с локальными партнерами
                ext_wh_id = _normalize_fulfillment_external_id(data.get('id'))
                ret_owner_id = data.get('fulfillment_id')
                ret_client_id = data.get('fulfillment_client_id')

                owner_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', ret_owner_id)], limit=1) if ret_owner_id else None
                client_fp = None
                if ret_client_id and ret_client_id != ret_owner_id:
                    client_fp = self.env['fulfillment.partners'].search([('fulfillment_id', '=', ret_client_id)], limit=1)

                # 6. Атомарные записи в модель
                warehouse.with_context(sync_context).write({
                    'fulfillment_owner_id': owner_fp.id if owner_fp else False,
                    'fulfillment_client_id': client_fp.id if client_fp else False,
                    'fulfillment_warehouse_id': ext_wh_id,
                    'last_update': fields.Datetime.now(),
                })

                if child_contact:
                    child_contact.with_context(sync_context).write({
                        'fulfillment_warehouse_id': ext_wh_id,
                        'linked_warehouse_id': warehouse.id,
                    })

                if parent_partner:
                    parent_partner.with_context(sync_context).write({'fulfillment_warehouse_id': ext_wh_id})

                # 7. Push-уведомление
                if client_fp and client_fp.fulfillment_id:
                    self.env['send.action'].push_update(client_fp.fulfillment_id)
                    _logger.info("[SEND ACTION]: Sent to partner %s", client_fp.fulfillment_id)

            except Exception as e:
                _logger.exception("[WAREHOUSE][CREATE] Critical failure for warehouse %s: %s", getattr(warehouse, 'id', None), e)

        _logger.info("[WAREHOUSE][CREATE][DONE] Processed %s warehouses", len(created_warehouses))
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
                    'fulfillment_warehouse_id': _normalize_fulfillment_external_id(data.get('id')),
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
                _logger.info("[IMPORT][WAREHOUSES] No warehouses from partner — will still register own.")
                # Even if partner has no warehouses, register our own unregistered warehouses.
                try:
                    self._register_own_warehouses(profile, client)
                except Exception as e:
                    _logger.exception("[IMPORT][WAREHOUSES] _register_own_warehouses failed: %s", e)
                return

            api_ids = [_normalize_fulfillment_external_id(w.get("id")) for w in warehouses]
            api_ids = [i for i in api_ids if i]
            existing = self.search([("fulfillment_warehouse_id", "in", api_ids)])
            existing_map = {
                _normalize_fulfillment_external_id(w.fulfillment_warehouse_id): w
                for w in existing
            }

            profile_fid = _normalize_fulfillment_external_id(getattr(profile, 'fulfillment_profile_id', None))

            for wh in warehouses:
                try:
                    with self.env.cr.savepoint():
                        wh_id = _normalize_fulfillment_external_id(wh.get("id"))
                        if not wh_id:
                            _logger.warning("[IMPORT][WAREHOUSE] Skip entry without id: %s", wh)
                            continue
                        # Re-check DB each iteration to prevent race-condition duplicates
                        # (e.g. when cron runs overlap or import is triggered multiple times)
                        if wh_id not in existing_map:
                            fresh = self.search([("fulfillment_warehouse_id", "=", wh_id)], limit=1)
                            if fresh:
                                existing_map[wh_id] = fresh
                        _logger.info("[Logger][Info]: [IMPORT][WAREHOUSE] >>> Processing %s (%s)", wh.get("name"), wh_id)

                        wh_client_id = _normalize_fulfillment_external_id(wh.get("fulfillment_client_id"))
                        wh_owner_id = _normalize_fulfillment_external_id(wh.get("fulfillment_id"))

                        is_our_client = profile_fid and wh_client_id == profile_fid
                        is_our_owner = profile_fid and wh_owner_id == profile_fid

                        if not is_our_client and not is_our_owner:
                            # Neither rented to us nor owned by us — skip entirely.
                            _logger.info(
                                "[IMPORT][WAREHOUSE] Skipping %s — not ours (client=%s, owner=%s, us=%s)",
                                wh.get("name"), wh_client_id, wh_owner_id, profile_fid,
                            )
                            continue

                        if is_our_owner and not is_our_client:
                            # We own it but it's leased to someone else — update local record to reflect leased_out.
                            existing_wh = existing_map.get(wh_id) or self.search(
                                [("fulfillment_warehouse_id", "=", wh_id)], limit=1
                            )
                            if existing_wh:
                                client_fp = self.env["fulfillment.partners"].search(
                                    [("fulfillment_id", "=", wh_client_id)], limit=1
                                )
                                existing_wh.with_context(skip_api_sync=True, from_fulfillment_import=True).write({
                                    "fulfillment_client_id": client_fp.id if client_fp else False,
                                })
                                _logger.info(
                                    "[IMPORT][WAREHOUSE] Updated own warehouse %s → leased_out to %s",
                                    existing_wh.name, wh_client_id,
                                )
                            continue  # No need to re-create our own warehouse

                        warehouse = existing_map.get(wh_id)

                        code = wh.get("code") or wh.get("short_name") or wh.get("name") or "WH"
                        original_code = code
                        suffix = 1
                        while self.search_count([("code", "=", code), ("id", "!=", warehouse.id if warehouse else 0)]):
                            code = f"{original_code}_{suffix}"
                            suffix += 1

                        # Use the API warehouse name as the primary name so that each warehouse
                        # from the same partner gets a distinct human-readable name.
                        # Strip arrow-notation (e.g. "Händler ⮕ Fulfillment") keeping only the part
                        # after the arrow when present, otherwise use the raw name.
                        api_wh_name = (wh.get("name") or "").strip()
                        for sep in (" ⮕ ", " → ", " -> ", " > "):
                            if sep in api_wh_name:
                                api_wh_name = api_wh_name.split(sep)[-1].strip()
                                break
                        partner_name = (fulfillment_partner.partner_id.name or "").strip()
                        # If the API name already contains the partner name, use it as-is;
                        # otherwise prefix with partner name for clarity.
                        if api_wh_name and partner_name and partner_name.lower() not in api_wh_name.lower():
                            base_name = f"{partner_name} – {api_wh_name}"
                        else:
                            base_name = api_wh_name or partner_name or "Fulfillment"
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
                            existing_map[wh_id] = warehouse

                        parent_partner = fulfillment_partner.partner_id
                        child_contact, _ = warehouse._get_or_create_warehouse_contact(
                            parent_partner, warehouse.name, warehouse=warehouse
                        )

                        if child_contact:
                            warehouse.with_context(
                                skip_api_sync=True,
                                from_fulfillment_import=True
                            ).write({"partner_id": child_contact.id})

                        owner_fp = self.env["fulfillment.partners"].search(
                            [("fulfillment_id", "=", wh_owner_id)],
                            limit=1
                        )

                        if wh_client_id != wh_owner_id:
                            client_fp = self.env["fulfillment.partners"].search(
                                [("fulfillment_id", "=", wh_client_id)], limit=1
                            )
                        else:
                            client_fp = False
                            _logger.warning(
                                "[Logger][Warning]: The API returned identical fulfillment_id and "
                                "fulfillment_client_id for the warehouse %s",
                                wh.get("name"),
                            )

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

                        _logger.info("[Logger][Info]: Imported warehouse %s (%s)", warehouse.name, wh_id)

                except Exception as e:
                    _logger.exception(
                        "[Logger][Exception]: [IMPORT][WAREHOUSE] Error while processing %s: %s",
                        wh, str(e),
                    )

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
        Checks the API first to avoid creating duplicates on repeated calls.
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
        if not unregistered:
            return

        # Fetch existing API warehouses owned by this instance to avoid duplicates.
        try:
            resp = client.fulfillment.list_warehouses(my_fulfillment_id)
            remote_list = (resp or {}).get("data") or []
            # Map code → id for warehouses owned (client) by this fulfillment instance.
            existing_by_code = {
                w.get("code"): w.get("id")
                for w in remote_list
                if w.get("fulfillment_client_id") == my_fulfillment_id
            }
        except Exception as e:
            _logger.warning("[_register_own_warehouses] Could not fetch existing warehouses: %s", e)
            existing_by_code = {}

        for wh in unregistered:
            try:
                wh_code = (wh.code or wh.name or "").upper()
                if wh_code in existing_by_code:
                    # Warehouse already registered — just save the ID locally.
                    api_wh_id = existing_by_code[wh_code]
                    _logger.info(
                        "[_register_own_warehouses] '%s' already in API as %s — linking locally",
                        wh.name, api_wh_id,
                    )
                else:
                    _logger.info("[_register_own_warehouses] Registering '%s' in API", wh.name)
                    payload = {
                        "name": wh.name,
                        "code": wh.code or wh.name,
                        "short_name": wh_code[:50],
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
                    # Cache so a second warehouse with the same code in this loop won't re-create.
                    existing_by_code[wh_code] = api_wh_id

                wh.with_context(skip_api_sync=True, from_fulfillment_import=True).write({
                    'fulfillment_warehouse_id': _normalize_fulfillment_external_id(api_wh_id),
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
    def _get_or_create_warehouse_contact(self, parent_partner, warehouse_name, warehouse=None):
        """Find or create a dedicated child contact for a warehouse under the given parent partner.

        Search priority:
        1. Contact already linked to this warehouse via linked_warehouse_id (survives name changes).
        2. Child contact under parent with the expected name format.
        3. Create a new child contact.

        When an existing contact is found but its name has drifted, it is silently updated
        so future look-ups keep working without creating duplicates.
        """
        if not parent_partner or not parent_partner.exists():
            return False, None

        child_name = f"{parent_partner.name} ({warehouse_name})"
        _logger.info("[CONTACT][LOOKUP] parent=%s name=%s", parent_partner.id, child_name)

        # Priority 1 — find by warehouse link (robust against name changes)
        if warehouse and warehouse.id:
            child = self.env['res.partner'].search([
                ('linked_warehouse_id', '=', warehouse.id),
                ('parent_id', '=', parent_partner.id),
            ], limit=1)
            if child:
                _logger.info("[CONTACT][FOUND by warehouse link] child=%s", child.id)
                if child.name != child_name:
                    child.with_context(skip_api_sync=True, skip_warehouse_contact=True).write(
                        {'name': child_name}
                    )
                fp = self.env['fulfillment.partners'].search(
                    [('partner_id', '=', child.id)], limit=1
                )
                return child, fp

        # Priority 2 — find by parent + name
        child = self.env['res.partner'].search([
            ('parent_id', '=', parent_partner.id),
            ('name', '=', child_name),
        ], limit=1)
        if child:
            _logger.info("[CONTACT][FOUND by name] child=%s", child.id)
            fp = self.env['fulfillment.partners'].search(
                [('partner_id', '=', child.id)], limit=1
            )
            return child, fp

        # Priority 3 — create new
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

        _logger.info("[CONTACT][CREATE] %s", vals)
        child = self.env['res.partner'].with_context(
            skip_api_sync=True, skip_warehouse_contact=True
        ).create(vals)

        fp = self.env['fulfillment.partners'].search(
            [('partner_id', '=', child.id)], limit=1
        )
        return child, fp

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
