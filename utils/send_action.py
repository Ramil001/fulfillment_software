# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class SendAction(models.AbstractModel):
    _name = "send.action"
    _description = "Send action to update"

    def push_update(self, fulfillment_client):
        return True