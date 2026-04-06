# -*- coding: utf-8 -*-
import logging
from odoo import models, api, fields, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class FulfillmentOrderLine(models.Model):
    _name = "fulfillment.order.line"
    _description = "Fulfillment Order Line"

    order_id = fields.Many2one("fulfillment.order", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", string="Product", required=True)
    product_uom_qty = fields.Float("Quantity", default=1.0, required=True)
    product_uom = fields.Many2one(
        "uom.uom",
        string="Unit of Measure",
        related="product_id.uom_id",
        store=True,
        readonly=False,
    )


class FulfillmentOrder(models.Model):
    _name = "fulfillment.order"
    _description = "Fulfillment Order"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(
        string="Reference",
        default="New",
        readonly=True,
        copy=False,
        tracking=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("confirmed", "Confirmed"),
            ("done", "Done"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        string="Status",
        tracking=True,
    )

    fulfillment_partner_id = fields.Many2one(
        "fulfillment.partners",
        string="Fulfillment Partner",
        required=True,
        tracking=True,
    )
    source_warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Source (Fulfillment) Warehouse",
        domain="[('warehouse_role', 'in', ['rented', 'leased_out']), ('fulfillment_warehouse_id', '!=', False)]",
        tracking=True,
        help="The fulfillment partner's warehouse from which goods will be sent to you.",
    )
    dest_warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Destination Warehouse",
        domain="[('warehouse_role', '=', 'own')]",
        tracking=True,
        help="Your local warehouse where goods will arrive.",
    )
    order_line_ids = fields.One2many("fulfillment.order.line", "order_id", string="Products")
    line_count = fields.Integer(
        string="Products",
        compute="_compute_line_count",
        store=True,
    )

    picking_id = fields.Many2one("stock.picking", string="Receipt", readonly=True, copy=False)
    picking_state = fields.Selection(related="picking_id.state", string="Receipt Status")
    fulfillment_transfer_id = fields.Char(
        related="picking_id.fulfillment_transfer_id",
        string="API Transfer ID",
        readonly=True,
    )

    @api.depends("order_line_ids")
    def _compute_line_count(self):
        for order in self:
            order.line_count = len(order.order_line_ids)

    notes = fields.Html("Notes")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", "New") == "New":
                vals["name"] = self.env["ir.sequence"].next_by_code("fulfillment.order") or "FO/0001"
        return super().create(vals_list)

    def action_confirm(self):
        """Confirm the order: create an incoming stock picking from the fulfillment warehouse."""
        for order in self:
            if order.state != "draft":
                raise UserError(_("Only draft orders can be confirmed."))
            if not order.order_line_ids:
                raise UserError(_("Please add at least one product line before confirming."))
            if not order.source_warehouse_id:
                raise UserError(_("Please select a source (fulfillment) warehouse."))
            if not order.dest_warehouse_id:
                raise UserError(_("Please select a destination warehouse."))

            picking = order._create_incoming_picking()
            order.write({"state": "confirmed", "picking_id": picking.id})
            order.message_post(body=_("Order confirmed. Receipt %s created.") % picking.name)
        return True

    def _create_incoming_picking(self):
        """Create an incoming stock.picking from fulfillment warehouse → our warehouse.

        Prefers a picking type explicitly marked with
        fulfillment_operation_type = 'request_from_fulfillment' and linked to
        the correct partner, falling back to any incoming type for the destination
        warehouse.
        """
        self.ensure_one()

        # 1. Prefer dedicated "Request from Fulfillment" operation type
        in_type = self.env["stock.picking.type"].search(
            [
                ("fulfillment_operation_type", "=", "request_from_fulfillment"),
                ("fulfillment_partner_id", "=", self.fulfillment_partner_id.id),
                ("warehouse_id", "=", self.dest_warehouse_id.id),
            ],
            limit=1,
        )
        # 2. Any "request_from_fulfillment" type for the destination warehouse
        if not in_type:
            in_type = self.env["stock.picking.type"].search(
                [
                    ("fulfillment_operation_type", "=", "request_from_fulfillment"),
                    ("warehouse_id", "=", self.dest_warehouse_id.id),
                ],
                limit=1,
            )
        # 3. Plain incoming type for the destination warehouse (legacy fallback)
        if not in_type:
            in_type = self.env["stock.picking.type"].search(
                [("code", "=", "incoming"), ("warehouse_id", "=", self.dest_warehouse_id.id)],
                limit=1,
            )
        if not in_type:
            raise UserError(
                _("No incoming operation type found for warehouse '%s'. "
                  "Please create a 'Request from Fulfillment' operation type first.")
                % self.dest_warehouse_id.name
            )

        src_location = self.source_warehouse_id.lot_stock_id
        dest_location = self.dest_warehouse_id.lot_stock_id

        move_vals = []
        for line in self.order_line_ids:
            move_vals.append((0, 0, {
                "name": line.product_id.display_name,
                "product_id": line.product_id.id,
                "product_uom_qty": line.product_uom_qty,
                "product_uom": line.product_uom.id or line.product_id.uom_id.id,
                "location_id": src_location.id,
                "location_dest_id": dest_location.id,
            }))

        picking = self.env["stock.picking"].create({
            "picking_type_id": in_type.id,
            "location_id": src_location.id,
            "location_dest_id": dest_location.id,
            "origin": self.name,
            "move_ids": move_vals,
            "fulfillment_partner_id": self.fulfillment_partner_id.id,
        })
        _logger.info("[FulfillmentOrder] Created picking %s for order %s", picking.name, self.name)
        return picking

    def _sync_state_from_picking(self):
        """Called when the linked picking changes state. Updates the order state accordingly."""
        for order in self:
            if order.state not in ('confirmed',):
                continue
            picking = order.picking_id
            if not picking:
                continue
            if picking.state == 'done':
                order.with_context(skip_fulfillment_push=True).write({'state': 'done'})
                order.message_post(body=_("Order marked as Done — receipt %s validated.") % picking.name)
            elif picking.state == 'cancel':
                order.with_context(skip_fulfillment_push=True).write({'state': 'cancelled'})
                order.message_post(body=_("Order cancelled — receipt %s was cancelled.") % picking.name)

    def action_cancel(self):
        for order in self:
            if order.state == "done":
                raise UserError(_("Done orders cannot be cancelled."))
            if order.picking_id and order.picking_id.state not in ("cancel", "draft"):
                order.picking_id.action_cancel()
            order.write({"state": "cancelled"})
        return True

    def action_reset_draft(self):
        for order in self:
            if order.state != "cancelled":
                raise UserError(_("Only cancelled orders can be reset to draft."))
            order.write({"state": "draft", "picking_id": False})
        return True

    def action_view_picking(self):
        self.ensure_one()
        if not self.picking_id:
            return False
        return {
            "type": "ir.actions.act_window",
            "name": _("Receipt"),
            "res_model": "stock.picking",
            "res_id": self.picking_id.id,
            "view_mode": "form",
            "target": "current",
        }
