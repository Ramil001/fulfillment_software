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
                _logger.debug(
                    "[FULFILLMENT] Создана локация без внешнего ID: %s (id=%s)",
                    rec.name, rec.id
                )
        return records

    def write(self, vals):
        res = super().write(vals)
        for rec in self:
            if 'fulfillment_location_id' in vals:
                _logger.info(
                    "[FULFILLMENT] Локация обновлена: %s → fulfillment_location_id=%s",
                    rec.name, rec.fulfillment_location_id
                )
        return res
