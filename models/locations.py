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
        # если импорт идёт из Fulfillment или явно отключена синхронизация — просто создаём
        if self.env.context.get("skip_api_sync"):
            return super().create(vals_list)

        records = super().create(vals_list)
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT] Профиль не найден — создание без синхронизации.")
            return records

        client = FulfillmentAPIClient(profile)
        api = client.location

        for rec in records:
            warehouse = self.env['stock.warehouse'].search([
                ('view_location_id', 'parent_of', rec.id)
            ], limit=1)

            if not (warehouse and warehouse.fulfillment_warehouse_id):
                _logger.debug("[FULFILLMENT] Локация без внешнего склада — пропуск sync: %s", rec.name)
                continue

            try:
                payload = {
                    "warehouse_id": warehouse.fulfillment_warehouse_id,
                    "name": rec.name,
                    "address": rec.complete_name or rec.name,
                }
                response = api.create(payload)
                data = response.get('data', {}) if response else {}

                if data.get('location_id'):
                    # ⚠️ важно — контекст skip_api_sync, чтобы write() не вызвал PATCH повторно
                    rec.with_context(skip_api_sync=True).write({
                        'fulfillment_location_id': data['location_id']
                    })
                    _logger.info("[FULFILLMENT] Создана внешняя локация: %s → %s",
                                rec.name, rec.fulfillment_location_id)
                else:
                    _logger.warning("[FULFILLMENT] Ответ Fulfillment без ID: %s", response)

            except FulfillmentAPIError as e:
                _logger.error("[FULFILLMENT] Ошибка создания локации: %s (%s)", rec.name, str(e))

        return records

    # =============================
    # WRITE (обновление)
    # =============================
    def write(self, vals):
        # защита от рекурсивного вызова
        if self.env.context.get("skip_api_sync"):
            return super().write(vals)

        res = super().write(vals)

        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT] Профиль не найден — синхронизация отключена.")
            return res

        client = FulfillmentAPIClient(profile)
        api = client.location

        for rec in self:
            warehouse = self.env['stock.warehouse'].search([
                ('view_location_id', 'parent_of', rec.id)
            ], limit=1)

            if not (warehouse and warehouse.fulfillment_warehouse_id):
                continue

            # если не менялись важные поля — пропускаем PATCH
            if not any(f in vals for f in ['name', 'complete_name', 'location_id']):
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
                    if data.get('location_id'):
                        rec.with_context(skip_api_sync=True).write({
                            'fulfillment_location_id': data['location_id']
                        })
                        _logger.info("[FULFILLMENT] POST+link: %s → %s",
                                    rec.name, rec.fulfillment_location_id)
            except FulfillmentAPIError as e:
                _logger.error("[FULFILLMENT] Ошибка write-sync: %s", str(e))

        return res



    # =============================
    # UNLINK (удаление)
    # =============================
    def unlink(self):
        for rec in self:
            _logger.warning(
                "[FULFILLMENT] Удалена локация: name=%s, id=%s, fulfillment_location_id=%s",
                rec.name, rec.id, rec.fulfillment_location_id or '—'
            )
        return super().unlink()
