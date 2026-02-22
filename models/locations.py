# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api
from ..lib.api_client import FulfillmentAPIClient, FulfillmentAPIError
from datetime import datetime

_logger = logging.getLogger(__name__)

class FulfillmentLocations(models.Model):
    _inherit = 'stock.location'
    fulfillment_location_id = fields.Char(
        string='Fulfillment Location ID',
        help='External location ID from Fulfillment system',
        index=True,
        copy=False,
        readonly=True,
    )
    
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
    # WRITE (обновление)
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
    # UNLINK (удаление)
    # =============================
    def unlink(self):
        _logger.info(f"[unlink]")
        for rec in self:
            _logger.warning(
                "[FULFILLMENT] Удалена локация: name=%s, id=%s, fulfillment_location_id=%s",
                rec.name, rec.id, rec.fulfillment_location_id or '—'
            )
        return super().unlink()
