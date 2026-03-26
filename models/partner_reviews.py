# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class FulfillmentPartnerReview(models.Model):
    _name = "fulfillment.partner.review"
    _description = "Fulfillment Partner Review"
    _order = "create_date desc"

    partner_id = fields.Many2one(
        "fulfillment.partners",
        string="Partner",
        required=True,
        ondelete="cascade",
        index=True,
    )
    rating = fields.Integer(string="Rating", required=True)
    comment = fields.Text(string="Review")
    author_id = fields.Many2one(
        "res.users",
        string="Author",
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
    )

    @api.constrains("rating")
    def _check_rating(self):
        for rec in self:
            if rec.rating < 1 or rec.rating > 5:
                raise ValidationError(_("Rating must be between 1 and 5."))
