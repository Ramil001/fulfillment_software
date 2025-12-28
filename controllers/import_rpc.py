# -*- coding: utf-8 -*-
import logging
import requests

from odoo import models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SendAction(models.AbstractModel):
    _name = "send.action"
    _description = "Send action to update"

    def push_update(self, fulfillment_id):
        if not fulfillment_id:
            raise UserError(_("Fulfillment ID is required"))

        partner = self.env['fulfillment.partners'].search(
            [('fulfillment_id', '=', fulfillment_id)],
            limit=1
        )

        if not partner:
            raise UserError(
                _("Fulfillment partner with ID %s not found") % fulfillment_id
            )

        if not partner.webhook_domain:
            raise UserError(
                _("Webhook domain is not set for partner %s") % partner.name
            )

        url = f"https://{partner.webhook_domain}/fulfillment/run_import_all"

        headers = {}
        if partner.fulfillment_api_key:
            headers['X-Fulfillment-API-Key'] = partner.fulfillment_api_key

        try:
            _logger.info("Sending fulfillment update to %s", url)

            response = requests.post(
                url,
                headers=headers,
                timeout=20
            )

            response.raise_for_status()

            _logger.info(
                "Fulfillment update successfully triggered for %s",
                partner.name
            )

            return True

        except requests.exceptions.RequestException as e:
            _logger.exception("Fulfillment update failed")
            raise UserError(
                _("Failed to push update to fulfillment partner:\n%s") % str(e)
            )
