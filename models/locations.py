# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError
from datetime import datetime

_logger = logging.getLogger(__name__)

_ICONS = ('📦 ', '🔑 ', '🏠 ', '🟢 ', '🔵 ', '🟠 ')


class StockLocation(models.Model):
    _inherit = "stock.location"

    fulfillment_location_id = fields.Char(
        string='Fulfillment Location ID',
        help='External location ID from Fulfillment system',
        index=True,
        copy=False,
        readonly=True,
    )

    def _get_fulfillment_icon(self):
        """Return an icon prefix if this location belongs to a fulfillment warehouse."""
        warehouse = self.env['stock.warehouse'].search([
            ('view_location_id', 'parent_of', self.id),
        ], limit=1)
        if not warehouse or not warehouse.fulfillment_warehouse_id:
            return ''
        role = warehouse.warehouse_role or 'own'
        return {'rented': '📦', 'leased_out': '🔑', 'own': '🏠'}.get(role, '📦')

    def name_get(self):
        # Call Odoo's base name_get directly (skip any other inherited overrides
        # from this module that may have already added icons).
        result = super().name_get()
        new_result = []
        for rec_id, name in result:
            # Strip any icon that a previous override may have added
            for icon in _ICONS:
                if name.startswith(icon):
                    name = name[len(icon):]
                    break

            rec = self.browse(rec_id)
            ff_icon = rec._get_fulfillment_icon()
            if ff_icon:
                name = f"{ff_icon} {name}"
            elif rec.usage == 'internal':
                name = "🟢 " + name
            elif rec.usage == 'fulfillment':
                name = "🔵 " + name
            elif rec.usage == 'supplier':
                name = "🟠 " + name

            new_result.append((rec_id, name))
        return new_result

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        # Strip any leading icon so search still works
        stripped = name
        for icon in _ICONS:
            if stripped.startswith(icon):
                stripped = stripped[len(icon):]
                break

        result = super().name_search(
            name=stripped,
            args=args,
            operator=operator,
            limit=limit,
        )

        new_result = []
        for rec_id, display_name in result:
            for icon in _ICONS:
                if display_name.startswith(icon):
                    display_name = display_name[len(icon):]
                    break

            rec = self.browse(rec_id)
            ff_icon = rec._get_fulfillment_icon()
            if ff_icon:
                display_name = f"{ff_icon} {display_name}"
            elif rec.usage == 'internal':
                display_name = "🟢 " + display_name
            elif rec.usage == 'customer':
                display_name = "🔵 " + display_name
            elif rec.usage == 'supplier':
                display_name = "🟠 " + display_name

            new_result.append((rec_id, display_name))

        return new_result

    # =============================
    # CREATE
    # =============================
    @api.model_create_multi
    def create(self, vals_list):
        _logger.info(f"[create]")
        records = super().create(vals_list)
        if self.env.context.get('skip_api_sync'):
            return records
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT] Профиль не найден — создание без синхронизации.")
            return records
        client = FulfillmentAPIClient(profile)
        api = client.location
        for rec in records:
            warehouse = self.env['stock.warehouse'].search([('view_location_id', '=', rec.id)], limit=1)
            if warehouse and warehouse.fulfillment_warehouse_id:
                try:
                    payload = {
                        "name": rec.name,
                        "warehouse_id": warehouse.fulfillment_warehouse_id,
                    }
                    response = api.create(payload)
                    _logger.info(f"[create] {payload}")
                    data = response.get('data', {}) if response else {}
                    _logger.info(f"[create] {data}")
                    if data.get('id'):
                        rec.with_context(skip_api_sync=True).write({
                            'fulfillment_location_id': data['id']
                        })
                        _logger.info("[FULFILLMENT] Создана внешняя корневая локация: %s → %s",
                                    rec.name, rec.fulfillment_location_id)
                except FulfillmentAPIError as e:
                    _logger.error("[FULFILLMENT] Ошибка создания корневой локации: %s (%s)", rec.name, str(e))
            child_locations = self.env['stock.location'].search([
                ('id', 'child_of', rec.id),
                ('id', '!=', rec.id)
            ])
            for child in child_locations:
                if child.fulfillment_location_id:
                    continue
                try:
                    payload = {
                        "warehouse_id": warehouse.fulfillment_warehouse_id,
                        "name": child.name,
                        "address": child.complete_name or child.name,
                    }
                    response = api.create(payload)
                    data = response.get('data', {}) if response else {}
                    if data.get('id'):
                        child.with_context(skip_api_sync=True).write({
                            'fulfillment_location_id': data['id']
                        })
                        _logger.info("[FULFILLMENT] Создана внешняя дочерняя локация: %s → %s",
                                    child.name, child.fulfillment_location_id)
                except FulfillmentAPIError as e:
                    _logger.error("[FULFILLMENT] Ошибка создания дочерней локации: %s (%s)", child.name, str(e))
        return records

    # =============================
    # WRITE
    # =============================
    def write(self, vals):
        _logger.info(f"[write]")
        res = super().write(vals)
        if self.env.context.get('skip_api_sync'):
            return res
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            return res
        client = FulfillmentAPIClient(profile)
        api = client.location
        for rec in self:
            warehouse = self.env['stock.warehouse'].search([
                ('view_location_id', 'parent_of', rec.id)
            ], limit=1)
            if not (warehouse and warehouse.fulfillment_warehouse_id):
                continue
            if not any(f in vals for f in ['name', 'complete_name']):
                continue
            payload = {
                "warehouse_id": warehouse.fulfillment_warehouse_id,
                "name": rec.name,
                "address": rec.complete_name or rec.name,
            }
            try:
                if rec.fulfillment_location_id:
                    api.update(rec.fulfillment_location_id, payload)
                    _logger.info("[FULFILLMENT] PATCH: %s (%s)", rec.name, rec.fulfillment_location_id)
                else:
                    response = api.create(payload)
                    data = response.get('data', {}) if response else {}
                    if data.get('id'):
                        rec.with_context(skip_api_sync=True).write({
                            'fulfillment_location_id': data['id']
                        })
                        _logger.info("[FULFILLMENT] POST+link: %s → %s", rec.name, rec.fulfillment_location_id)
            except FulfillmentAPIError as e:
                _logger.error("[FULFILLMENT] Ошибка write-sync: %s", str(e))
        return res

    # =============================
    # UNLINK
    # =============================
    def unlink(self):
        _logger.info(f"[unlink]")
        for rec in self:
            _logger.warning(
                "[FULFILLMENT] Удалена локация: name=%s, id=%s, fulfillment_location_id=%s",
                rec.name, rec.id, rec.fulfillment_location_id or '—'
            )
        return super().unlink()
