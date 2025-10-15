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
        records = super().create(vals_list)
        profile = self.env['fulfillment.profile'].search([], limit=1)
        client = FulfillmentAPIClient(profile)
        api = client.location  # экземпляр LocationAPI

        for rec in records:
            # Получаем склад, к которому относится локация
            warehouse = self.env['stock.warehouse'].search([
                ('view_location_id', 'parent_of', rec.id)
            ], limit=1)

            # Проверяем, связан ли склад с Fulfillment
            if warehouse and warehouse.fulfillment_warehouse_id:
                try:
                    payload = {
                        "warehouse_id": warehouse.fulfillment_warehouse_id,
                        "name": rec.name,
                        "address": rec.complete_name or rec.name,
                    }
                    response = api.create(payload)
                    data = response.get('data', {}) if response else {}

                    if data.get('location_id'):
                        rec.fulfillment_location_id = data['location_id']
                        _logger.info(
                            "[FULFILLMENT] Создана локация во внешней системе: name=%s, id=%s, external_id=%s",
                            rec.name, rec.id, rec.fulfillment_location_id
                        )
                    else:
                        _logger.warning(
                            "[FULFILLMENT] Ответ Fulfillment без location_id: %s (name=%s, id=%s)",
                            response, rec.name, rec.id
                        )

                except FulfillmentAPIError as e:
                    _logger.error(
                        "[FULFILLMENT] Ошибка при создании локации во Fulfillment (name=%s, id=%s): %s",
                        rec.name, rec.id, str(e)
                    )
            else:
                _logger.info(
                    "[FULFILLMENT] Создана локально без внешнего ID: name=%s, id=%s (склад без Fulfillment)",
                    rec.name, rec.id
                )

        return records

    # =============================
    # WRITE (обновление)
    # =============================
    def write(self, vals):
        res = super().write(vals)
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[FULFILLMENT] Профиль не найден — синхронизация отключена.")
            return res

        client = FulfillmentAPIClient(profile)
        api = client.location

        for rec in self:
            changed_fields = ', '.join(vals.keys()) if vals else '(нет изменений)'
            _logger.info(
                "[FULFILLMENT] Обновлена локация: name=%s, id=%s, changed=%s",
                rec.name, rec.id, changed_fields
            )

            warehouse = self.env['stock.warehouse'].search([
                ('view_location_id', 'parent_of', rec.id)
            ], limit=1)

            # Синхронизация с Fulfillment
            if warehouse and warehouse.fulfillment_warehouse_id:
                try:
                    payload = {
                        "warehouse_id": warehouse.fulfillment_warehouse_id,
                        "name": rec.name,
                        "address": rec.complete_name or rec.name,
                    }

                    if rec.fulfillment_location_id:
                        # ✅ Локация уже существует во Fulfillment → обновляем
                        response = api.update(rec.fulfillment_location_id, payload)
                        _logger.info(
                            "[FULFILLMENT] Локация обновлена во внешней системе: name=%s, external_id=%s",
                            rec.name, rec.fulfillment_location_id
                        )
                    else:
                        # ❌ Внешнего ID нет → создаём новую запись
                        response = api.create(payload)
                        data = response.get('data', {}) if response else {}
                        if data.get('location_id'):
                            rec.fulfillment_location_id = data['location_id']
                            _logger.info(
                                "[FULFILLMENT] Для обновляемой локации создан внешний объект: %s (external_id=%s)",
                                rec.name, rec.fulfillment_location_id
                            )

                except FulfillmentAPIError as e:
                    _logger.error(
                        "[FULFILLMENT] Ошибка синхронизации локации (name=%s, id=%s): %s",
                        rec.name, rec.id, str(e)
                    )

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
