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
        copy=False
    )

    # =============================
    # CREATE
    # =============================
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.fulfillment_location_id:
                _logger.info(
                    "[FULFILLMENT] Создана локация с внешним ID: %s (name=%s, id=%s)",
                    rec.fulfillment_location_id, rec.name, rec.id
                )
            else:
                _logger.info(
                    "[FULFILLMENT] Создана локация без внешнего ID: name=%s, id=%s",
                    rec.name, rec.id
                )
        return records

    # =============================
    # WRITE (обновление)
    # =============================
    def write(self, vals):
        res = super().write(vals)
        for rec in self:
            changed_fields = ', '.join(vals.keys()) if vals else '(нет изменений)'
            _logger.info(
                "[FULFILLMENT] Обновлена локация: name=%s, id=%s, changed=%s",
                rec.name, rec.id, changed_fields
            )

            if 'fulfillment_location_id' in vals:
                _logger.info(
                    "[FULFILLMENT] Локация %s → новый fulfillment_location_id=%s",
                    rec.name, rec.fulfillment_location_id
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
