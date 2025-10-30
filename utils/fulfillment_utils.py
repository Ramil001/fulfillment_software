# -*- coding: utf-8 -*-
from odoo import models

class FulfillmentUtils(models.AbstractModel):
    _name = "fulfillment.utils"
    _description = "Fulfillment helper utils"

    def is_partner_fulfillment(self, partner_id):
        """Проверяет, связан ли партнёр с Fulfillment."""
        if not partner_id:
            return False

        partner = self.env["res.partner"].browse(partner_id)
        if not partner.exists():
            return False

        # 1. Проверяем прямую связь через поле
        if getattr(partner, "fulfillment_partner_id", False):
            return True

        # 2. Проверяем родителя
        parent = partner.parent_id
        if parent and getattr(parent, "fulfillment_partner_id", False):
            return True

        # 3. Проверяем категорию
        if partner.category_id.filtered(lambda c: c.name == "Fulfillment"):
            return True

        return False



    def get_fulfillment_profile_name(self, profile_id=None):
        if profile_id:
            profile = self.env['fulfillment.profile'].browse(profile_id)
        else:
            profile = self.env['fulfillment.profile'].search([], limit=1)
            
        if profile and profile.exists():
            return profile.name or "Fulfillment name not found"
        return False
    