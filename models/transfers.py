# -*- coding: utf-8 -*-
import base64
import json
import logging

import requests as _requests

from odoo import models, api, fields, _
from odoo.exceptions import UserError
from ..lib.api_client import FulfillmentAPIClient

_logger = logging.getLogger(__name__)


# ================== Strategy: Transfer Mappers ==================
class BaseTransferMapper:
    def build(self, picking, items, from_warehouse_id, to_warehouse_id, fulfillment_out, fulfillment_in, contacts=None):
        raise NotImplementedError

class IncomingTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts=None):
        return {
            "transfer_type": "incoming",
            "warehouse_out": warehouse_out,
            "warehouse_in": warehouse_in,
            "fulfillment_out": fulfillment_out,
            "fulfillment_in": fulfillment_in,
            "reference": picking.name or picking.origin or "Odoo",
            "items": items,
            "contacts": contacts or [],
        }

class OutgoingTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts=None):
        return {
            "transfer_type": "outgoing",
            "warehouse_out": warehouse_out,
            "warehouse_in": warehouse_in,
            "fulfillment_out": fulfillment_out,
            "fulfillment_in": fulfillment_in,
            "reference": picking.name or picking.origin or "Odoo",
            "items": items,
            "contacts": contacts or [],
        }

class InternalTransferMapper(BaseTransferMapper):
    def build(self, picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts=None):
        return {
            "transfer_type": "internal",
            "warehouse_out": warehouse_out,
            "warehouse_in": warehouse_in,
            "fulfillment_out": fulfillment_out,
            "fulfillment_in": fulfillment_in,
            "reference": picking.name or picking.origin or "00000",
            "items": items,
        }

# ================== Adapter ==================
class PickingAdapter:
    _strategies = {
        "incoming": IncomingTransferMapper(),
        "outgoing": OutgoingTransferMapper(),
        "internal": InternalTransferMapper(),
    }

    @classmethod
    def to_api_payload(cls, picking, items, warehouse_out, warehouse_in,
                       fulfillment_out, fulfillment_in, contacts=None):
        _logger.info("[to_api_payload]")
        mapper = cls._strategies.get(picking.picking_type_code)
        if not mapper:
            raise ValueError(f"Unsupported picking type {picking.picking_type_code}")
        return mapper.build(picking, items, warehouse_out, warehouse_in, fulfillment_out, fulfillment_in, contacts)

class FulfillmentItemBuilder:
    def __init__(self, client):
        self.client = client

    def build_items(self, moves):
        _logger.info("[build_items]")
        items = []
        for move in moves:
            product_tmpl = move.product_id.product_tmpl_id
            fulfillment_id = self._ensure_remote_product(product_tmpl)
            if not fulfillment_id:
                continue
            items.append({
                "product_id": fulfillment_id,
                "quantity": float(move.product_uom_qty or 0.0),
                "unit": move.product_uom.name if move.product_uom else 'Units',
            })
        return items

    def _ensure_remote_product(self, tmpl):
        _logger.info("[_ensure_remote_product]")
        if not getattr(tmpl, "fulfillment_product_id", None):
            product_payload = {
                "name": tmpl.name,
                "sku": tmpl.default_code or f"SKU-{tmpl.id}",
                "barcode": tmpl.barcode or str(tmpl.id).zfill(6),
            }
            try:
                resp = self.client.product.create(product_payload)
                if resp and resp.get("data", {}).get("id"):
                    tmpl.product_variant_id.fulfillment_product_id = resp["data"].get("id")
                    _logger.info("[Fulfillment][Create] Remote product %s -> %s",
                                 tmpl.name, tmpl.fulfillment_product_id)
            except Exception as e:
                _logger.error("[Fulfillment][Create] Exception creating product %s: %s", tmpl.name, e)
        return tmpl.fulfillment_product_id

class FulfillmentTransfers(models.Model):
    _inherit = 'stock.picking'

    fulfillment_partner_id = fields.Many2one(
        'fulfillment.partners',
        string="Fulfillment Partner",
        index=True,
        ondelete='set null'
    )

    fulfillment_transfer_id = fields.Char(
        string="Transfer ID",
        default="Empty",
        help="Fulfillment transfer ID",
        readonly=True
    )

    fulfillment_transfer_owner_id = fields.Char(
        string="Resource owner",
        default="Empty",
        help="Fulfillment owner ID (fulfillment_in)",
        readonly=True
    )

    fulfillment_transfer_out_id = fields.Char(
        string="Fulfillment Out",
        default="Empty",
        help="Fulfillment ID of the outgoing partner (fulfillment_out)",
        readonly=True
    )

    fulfillment_warehouse_id = fields.Char(
        string="Fulfillment Warehouse ID",
        help="External Warehouses ID",
        copy=False
    )

    fulfillment_delivery_status = fields.Selection([
        ('delivering', 'Sent to fulfillment (awaiting confirmation)'),
        ('delivered', 'Received by fulfillment'),
    ], string='Fulfillment Delivery Status', readonly=True, copy=False, index=True)

    # True when THIS instance is the sender of the cross-instance transfer.
    # Used in views to show context-appropriate status banners.
    is_cross_instance_sender = fields.Boolean(
        string='I am the Sender',
        compute='_compute_is_cross_instance_sender',
        store=False,
    )

    @api.depends('fulfillment_transfer_out_id')
    def _compute_is_cross_instance_sender(self):
        profile = self.env['fulfillment.profile'].search([], limit=1)
        my_id = profile.fulfillment_profile_id if profile else None
        for rec in self:
            rec.is_cross_instance_sender = bool(my_id and rec.fulfillment_transfer_out_id == my_id)

    # Computed: expose the fulfillment_operation_type of the picking type for use in views
    fulfillment_operation_type = fields.Selection(
        related='picking_type_id.fulfillment_operation_type',
        string='Fulfillment Operation',
        store=False,
        readonly=True,
    )

    @api.onchange('picking_type_id', 'fulfillment_partner_id')
    def _onchange_fulfillment_picking_type(self):
        """When a fulfillment operation type is selected, auto-fill locations and partner."""
        op_type = self.picking_type_id.fulfillment_operation_type if self.picking_type_id else None
        if not op_type:
            return

        # Auto-fill fulfillment_partner_id from the picking type if not set
        if not self.fulfillment_partner_id and self.picking_type_id.fulfillment_partner_id:
            self.fulfillment_partner_id = self.picking_type_id.fulfillment_partner_id

        fp = self.fulfillment_partner_id
        if not fp:
            return

        # Find the partner's warehouse
        partner_wh = self.env['stock.warehouse'].search([
            ('fulfillment_owner_id', '=', fp.id),
            ('fulfillment_warehouse_id', '!=', False),
        ], limit=1)
        # Find our own warehouse
        own_wh = self.picking_type_id.warehouse_id

        if op_type == 'send_to_fulfillment':
            if own_wh:
                self.location_id = own_wh.lot_stock_id
            if partner_wh:
                self.location_dest_id = partner_wh.lot_stock_id
        elif op_type == 'request_from_fulfillment':
            if partner_wh:
                self.location_id = partner_wh.lot_stock_id
            if own_wh:
                self.location_dest_id = own_wh.lot_stock_id

    def _fetch_product_from_api(self, fulfillment_product_id):
        _logger.info("[_fetch_product_from_api]")
        if not fulfillment_product_id:
            return None
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found for product fetch")
            return None
        client = FulfillmentAPIClient(profile)
        try:
            response = client.product.get(fulfillment_product_id)
            return response.get("data")
        except Exception as e:
            _logger.error("[Fulfillment] Failed to fetch product %s: %s", fulfillment_product_id, e)
            return None

    @api.onchange('partner_id')
    def _onchange_partner(self):
        _logger.info("[_onchange_partner]")
        if not self.partner_id:
            return
        message = f"В документе {self.name or '(новый документ)'} изменён партнёр на {self.partner_id.display_name}."
        payload = {
            "type": "fulfillment_notification",
            "payload": {
                "message": message,
                "title": "Изменение партнёра",
                "level": "info",
                "sticky": False,
            },
        }
        bus = self.env["bus.bus"].sudo()
        users = self.env["res.users"].sudo().search([])
        for user in users:
            partner = user.partner_id
            if not partner:
                continue
            try:
                bus._sendone(partner, "fulfillment_notification", payload)
            except Exception as e:
                _logger.error("[BUS][ERROR] Не удалось отправить уведомление %s: %s", partner.name, e)

    @api.model_create_multi
    def create(self, vals_list):
        _logger.info("[create]")
        _logger.info(f"[Fulfillment][Transfer][Create]: [self]: {self} | [vals_list]: {vals_list}")
        try:
            records = super(FulfillmentTransfers, self).create(vals_list)
            _logger.info("[Fulfillment] Records created: %s", records.ids)
        except Exception as e:
            _logger.error("[Fulfillment] CRASH during super().create: %s", str(e), exc_info=True)
            raise
        if not self.env.context.get("skip_fulfillment_push"):
            for rec in records:
                fulfillment_transfer_id = rec.fulfillment_transfer_id
                _logger.info("[Fulfillment] Processing %s (current f_id: '%s')", rec.name, fulfillment_transfer_id)
                if not fulfillment_transfer_id or fulfillment_transfer_id == "Empty":
                    _logger.info("[Fulfillment] New record detected. Triggering API Push for %s", rec.name)
                    try:
                        rec._push_to_fulfillment_api()
                    except Exception as e:
                        _logger.error("[Fulfillment] Error during push for %s: %s", rec.name, str(e), exc_info=True)
                else:
                    existing = self.search([
                        ("fulfillment_transfer_id", "=", fulfillment_transfer_id),
                        ("id", "!=", rec.id)
                    ], limit=1)
                    if not existing:
                        _logger.info("[Fulfillment] Unique UUID detected. Updating %s in API", rec.name)
                        rec._push_to_fulfillment_api()
                    else:
                        _logger.warning("[Fulfillment] SKIP: UUID '%s' is already used by record %s", fulfillment_transfer_id, existing.id)
        return records

    def _log_state_transition(self, rec, old_state, new_state, source):
        _logger.info("[_log_state_transition]")
        if old_state == new_state:
            return
        _logger.info(
            "[Fulfillment][STATE CHANGE][%s] %s: %s → %s",
            source, rec.name or f"id={rec.id}", old_state, new_state
        )
        if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
            _logger.warning("[Fulfillment][STATE CHANGE] Skipped API push — no fulfillment_transfer_id")
            return
        rec._push_status_update(new_state)

    def write(self, vals):
        _logger.info(f"[write]: {vals}")
        res = super().write(vals)
        if "state" in vals:
            pickings = self.filtered(
                lambda p: p.state in ("waiting", "assigned")
                and (not p.fulfillment_transfer_id or p.fulfillment_transfer_id == "Empty")
            )
            if pickings:
                picking_ids = pickings.ids
                def after_commit():
                    env = api.Environment(self.env.cr, self.env.uid, self.env.context)
                    records = env["stock.picking"].browse(picking_ids)
                    for picking in records:
                        try:
                            picking._push_to_fulfillment_api()
                        except Exception:
                            _logger.exception(
                                "[Fulfillment] Postcommit sync failed for %s",
                                picking.name
                            )
                self.env.cr.postcommit.add(after_commit)
        return res

    def action_confirm(self):
        _logger.info("[action_confirm]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_confirm()
        if self.env.context.get('skip_fulfillment_push'):
            return res
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_confirm")
            if not rec.fulfillment_transfer_id or rec.fulfillment_transfer_id == "Empty":
                _logger.info("[Fulfillment] Triggering API Push for %s during action_confirm", rec.name)
                try:
                    rec._push_to_fulfillment_api()
                except Exception as e:
                    _logger.error("[Fulfillment] Push failed during confirm: %s", str(e))
        return res

    def action_assign(self):
        _logger.info("[action_assign]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_assign()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_assign")
        return res

    def _is_transfer_to_fulfillment(self):
        """Return True if this picking sends goods toward a fulfillment partner center.

        - outgoing: any picking that was pushed to the fulfillment API
        - internal: destination warehouse is owned by a fulfillment partner (not this instance)
        """
        if self.picking_type_code == 'outgoing':
            return (
                bool(self.fulfillment_transfer_id)
                and self.fulfillment_transfer_id not in ('Empty', '')
            )
        if self.picking_type_code == 'internal':
            dest_wh = self.env['stock.warehouse'].search([
                '|',
                ('lot_stock_id', '=', self.location_dest_id.id),
                ('view_location_id', 'parent_of', self.location_dest_id.id),
            ], limit=1)
            if not dest_wh or not dest_wh.fulfillment_warehouse_id:
                return False
            if not dest_wh.fulfillment_owner_id:
                # Has fulfillment_warehouse_id but no owner — imported externally
                return True
            profile = self.env['fulfillment.profile'].search([], limit=1)
            my_id = getattr(profile, 'fulfillment_profile_id', None)
            owner_fid = getattr(dest_wh.fulfillment_owner_id, 'fulfillment_id', None)
            return bool(owner_fid) and owner_fid != my_id
        return False

    def _is_managed_by_fulfillment(self):
        """Return True if this picking's stock is managed by a remote fulfillment partner.

        This is the case for:
        - Receipts (incoming) whose destination is a RENTED warehouse (managed remotely).
        - Outgoing/internal transfers whose source is a rented warehouse.

        When True, the picking should NOT be validated locally — only the fulfillment
        partner can confirm physical movement, which auto-validates via webhook.
        """
        dest_wh = self.env['stock.warehouse'].search([
            '|',
            ('lot_stock_id', '=', self.location_dest_id.id),
            ('view_location_id', 'parent_of', self.location_dest_id.id),
        ], limit=1)
        src_wh = self.env['stock.warehouse'].search([
            '|',
            ('lot_stock_id', '=', self.location_id.id),
            ('view_location_id', 'parent_of', self.location_id.id),
        ], limit=1)

        if self.picking_type_code == 'incoming' and dest_wh and dest_wh.warehouse_role == 'rented':
            return True
        if self.picking_type_code in ('outgoing', 'internal') and src_wh and src_wh.warehouse_role == 'rented':
            return True
        # Internal transfer going TO a rented warehouse: Fulfillment must confirm receipt.
        if self.picking_type_code == 'internal' and dest_wh and dest_wh.warehouse_role == 'rented':
            return True
        return False

    def button_validate(self):
        _logger.info("[button_validate]")
        old_states = {rec.id: rec.state for rec in self}

        # Block manual validation for pickings managed by a remote fulfillment partner.
        if not self.env.context.get('skip_fulfillment_push') and not self.env.context.get('from_fulfillment_import'):
            for rec in self:
                if (
                    rec._is_managed_by_fulfillment()
                    and rec.fulfillment_delivery_status not in ('delivering', 'delivered')
                ):
                    raise UserError(
                        f"Transfer {rec.name} is managed by a remote fulfillment partner. "
                        f"Validation is only allowed after the fulfillment partner confirms the operation."
                    )

        # Detect cross-instance transfers BEFORE validation so we can intercept the push.
        cross_instance_senders = {rec.id for rec in self if rec._is_transfer_to_fulfillment()}
        res = super(FulfillmentTransfers, self).button_validate()
        profile = self.env['fulfillment.profile'].search([], limit=1)
        my_fid = profile.fulfillment_profile_id if profile else None
        for rec in self:
            if rec.state == 'done' and not rec.fulfillment_delivery_status and not rec.env.context.get('from_fulfillment_import'):
                if rec.id in cross_instance_senders:
                    # Only send 'assigned' (and wait for the other side to confirm)
                    # when WE are the CLIENT (fulfillment_out ≠ us).
                    # When WE are the MANAGER (fulfillment_out == us), push 'done' directly —
                    # do NOT set fulfillment_delivery_status='delivering'.
                    is_manager = bool(my_fid and rec.fulfillment_transfer_out_id == my_fid)
                    if not is_manager:
                        # Mark as "delivering" BEFORE _log_state_transition so that
                        # _push_status_update can intercept 'done' and send 'assigned' instead.
                        rec.with_context(skip_fulfillment_push=True).write({
                            'fulfillment_delivery_status': 'delivering',
                        })
                        _logger.info(
                            "[Fulfillment] Set fulfillment_delivery_status=delivering for %s (%s) — client side",
                            rec.name, rec.picking_type_code,
                        )
                    else:
                        _logger.info(
                            "[Fulfillment] %s: we are the manager (fulfillment_out=%s) — pushing done directly",
                            rec.name, rec.fulfillment_transfer_out_id,
                        )
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "button_validate")
            # Sync linked FulfillmentOrder state
            if rec.state == 'done':
                self._sync_fulfillment_orders(rec)
        return res

    @api.model
    def _sync_fulfillment_orders(self, picking):
        """Update FulfillmentOrder state when the linked picking is validated."""
        orders = self.env['fulfillment.order'].search([
            ('picking_id', '=', picking.id),
            ('state', '=', 'confirmed'),
        ])
        if orders:
            orders._sync_state_from_picking()
            _logger.info(
                "[Fulfillment] Synced %d FulfillmentOrder(s) from picking %s",
                len(orders), picking.name,
            )

    def action_done(self):
        _logger.info("[action_done]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_done()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_done")
        return res

    def action_cancel(self):
        _logger.info("[action_cancel]")
        old_states = {rec.id: rec.state for rec in self}
        res = super(FulfillmentTransfers, self).action_cancel()
        for rec in self:
            self._log_state_transition(rec, old_states.get(rec.id), rec.state, "action_cancel")
        return res

    def message_post(self, **kwargs):
        """Forward user comments on fulfillment-linked transfers to the Fulfillment API."""
        result = super().message_post(**kwargs)
        _logger.info(
            '[FulfillmentMessage][transfer] message_post called: '
            'message_type=%s subtype=%s from_api=%s',
            kwargs.get('message_type'),
            kwargs.get('subtype_xmlid'),
            self.env.context.get('from_fulfillment_api'),
        )
        msg_type = kwargs.get('message_type', '')
        is_user_comment = msg_type in ('comment', '') or not msg_type
        if (
            not self.env.context.get('from_fulfillment_api')
            and is_user_comment
            and kwargs.get('body')
        ):
            from odoo.tools import html2plaintext
            content = html2plaintext(str(kwargs.get('body', ''))).strip()
            if content:
                profile = self.env['fulfillment.profile'].search([], limit=1)
                if profile and profile.fulfillment_profile_id:
                    for rec in self:
                        transfer_id = rec.fulfillment_transfer_id
                        my_id = profile.fulfillment_profile_id
                        receiver_id = None
                        for candidate in (rec.fulfillment_transfer_owner_id, rec.fulfillment_transfer_out_id):
                            if candidate and candidate not in ('Empty', my_id):
                                receiver_id = candidate
                                break
                        if not receiver_id:
                            linked_partner = rec.fulfillment_partner_id
                            receiver_id = linked_partner.fulfillment_id if linked_partner else None
                        if not receiver_id and transfer_id and transfer_id not in ('Empty', ''):
                            try:
                                fb_client = self.fulfillment_api
                                if fb_client:
                                    resp = fb_client.transfer.get(transfer_id)
                                    t_data = resp.get('data') or {}
                                    for fid in (t_data.get('fulfillment_in'), t_data.get('fulfillment_out')):
                                        if fid and fid not in ('Empty', my_id):
                                            receiver_id = fid
                                            if t_data.get('fulfillment_in') and t_data['fulfillment_in'] not in ('Empty', my_id):
                                                rec.with_context(skip_fulfillment_push=True).write({
                                                    'fulfillment_transfer_owner_id': t_data['fulfillment_in'],
                                                })
                                            break
                                    _logger.info('[FulfillmentMessage][transfer] API fallback receiver: %s', receiver_id)
                            except Exception as fb_err:
                                _logger.warning('[FulfillmentMessage][transfer] API fallback failed: %s', fb_err)
                        if not receiver_id:
                            if rec.fulfillment_transfer_out_id == my_id:
                                other = rec.env['fulfillment.partners'].sudo().search([
                                    ('fulfillment_id', 'not in', [my_id, 'Empty', False]),
                                ], limit=1)
                                receiver_id = other.fulfillment_id if other else None
                                if receiver_id:
                                    rec.with_context(skip_fulfillment_push=True).write({
                                        'fulfillment_transfer_owner_id': receiver_id,
                                    })
                                    _logger.info('[FulfillmentMessage][transfer] Partner fallback receiver: %s', receiver_id)
                        _logger.info(
                            '[FulfillmentMessage][transfer] rec=%s transfer_id=%s receiver_id=%s',
                            rec.name, transfer_id, receiver_id,
                        )
                        if (
                            transfer_id and transfer_id != 'Empty'
                            and receiver_id and receiver_id not in ('Empty', my_id)
                        ):
                            try:
                                client = self.fulfillment_api
                                if client:
                                    client.message.send(
                                        sender_fulfillment_id=profile.fulfillment_profile_id,
                                        receiver_fulfillment_id=receiver_id,
                                        content=content,
                                        ref_type='transfer',
                                        ref_id=transfer_id,
                                    )
                                    _logger.info('[FulfillmentMessage] Sent transfer message for %s to %s', transfer_id, receiver_id)
                            except Exception as e:
                                _logger.warning('[FulfillmentMessage] Failed to send for transfer %s: %s', transfer_id, e)
        return result

    def _push_status_update(self, new_state):
        _logger.info("[_push_status_update]")
        if self.env.context.get('skip_fulfillment_push'):
            _logger.info("[Fulfillment][API] Status push skipped (skip_fulfillment_push) for %s", self.fulfillment_transfer_id)
            return
        if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
            _logger.warning("Skip status push – no transfer_id yet")
            return
        client = self.fulfillment_api
        if not client:
            return
        # For cross-instance transfers where WE are the sender:
        # When our local picking is validated (done), we push 'assigned' to the API
        # to signal "goods dispatched, awaiting receiver confirmation".
        # 'done' is only pushed when the RECEIVER validates their incoming picking.
        api_status = new_state
        if new_state == 'done' and self.fulfillment_delivery_status == 'delivering':
            api_status = 'assigned'
            _logger.info(
                "[Fulfillment][API] Cross-instance sender: pushing 'assigned' instead of 'done' for %s — receiver must confirm receipt",
                self.fulfillment_transfer_id,
            )
        payload = {"status": api_status}
        try:
            client.transfer.update(self.fulfillment_transfer_id, payload)
            _logger.info("[Fulfillment][API] Status pushed: %s -> %s", self.fulfillment_transfer_id, api_status)
        except Exception as e:
            _logger.error("[Fulfillment][API ERROR] Failed status push: %s", e)

    def _get_partner_fulfillment_profile_id(self, partner):
        _logger.info("[_get_partner_fulfillment_profile_id]")
        if not partner:
            return None
        try:
            linked_wh = getattr(partner, "linked_warehouse_id", None)
            if linked_wh:
                owner = getattr(linked_wh, "fulfillment_owner_id", None)
                if owner and getattr(owner, "profile_id", None):
                    return owner.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment] linked_warehouse check failed: %s", e)
        try:
            fp = self.env['fulfillment.partners'].search([('partner_id', '=', partner.id)], limit=1)
            if fp and getattr(fp, "profile_id", None):
                return fp.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment] fulfillment.partners check failed: %s", e)
        try:
            contact_wh_ext = getattr(partner, "fulfillment_contact_warehouse_id", None)
            if contact_wh_ext:
                wh = self.env['stock.warehouse'].search(
                    [('fulfillment_warehouse_id', '=', contact_wh_ext)], limit=1
                )
                if wh and getattr(wh, "fulfillment_owner_id", None):
                    owner = wh.fulfillment_owner_id
                    if owner and getattr(owner, "profile_id", None):
                        return owner.profile_id.fulfillment_profile_id or None
        except Exception as e:
            _logger.debug("[Fulfillment] contact_warehouse check failed: %s", e)
        return None

    def _push_to_fulfillment_api(self):
        _logger.info("[_push_to_fulfillment_api]")
        self.ensure_one()
        _logger.info(
            "[Fulfillment][PUSH] Called for picking: %s (id=%s, transfer_id=%s, type=%s)",
            self.name, self.id, self.fulfillment_transfer_id, self.picking_type_code
        )
        if not self.move_ids:
            _logger.warning("[Fulfillment][PUSH] Skip — no move_ids for %s", self.name)
            return
        client = self.fulfillment_api
        if not client:
            _logger.error("[Fulfillment][PUSH] API client unavailable")
            return
        items = FulfillmentItemBuilder(client).build_items(self.move_ids)
        if not items:
            _logger.debug("[Fulfillment] No items to sync for %s", self.name)
            return
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found")
            return
        my_fulfillment_id = getattr(profile, "fulfillment_profile_id", None)
        warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in = self._resolve_warehouses_and_profiles(
            my_fulfillment_id
        )
        if not my_fulfillment_id:
            _logger.warning("[Fulfillment] fulfillment_profile_id not configured — skipping API push for %s.", self.name)
            return
        op_type = self.picking_type_id.fulfillment_operation_type if self.picking_type_id else None

        if op_type in ('send_to_fulfillment', 'request_from_fulfillment'):
            # For explicit fulfillment operation types we need both warehouses and fulfillment_in
            if not warehouse_out_id or not warehouse_in_id:
                raise UserError(
                    "[Fulfillment] Cannot push transfer: missing warehouse mapping. "
                    "Make sure the fulfillment partner's warehouse is imported and linked."
                )
            if not fulfillment_in:
                raise UserError(
                    "[Fulfillment] Cannot push transfer: fulfillment partner has no API ID. "
                    "Check the Fulfillment Partner record."
                )
        elif self.picking_type_code == "incoming":
            # warehouse_out_id may be None for PO receipts from external (non-fulfillment) suppliers.
            # Only warehouse_in_id (destination rented warehouse) and fulfillment_out are required.
            if not warehouse_in_id:
                _logger.debug(
                    "[Fulfillment] Skipping incoming %s — no fulfillment_warehouse_id on destination.", self.name
                )
                return
            if not fulfillment_out:
                _logger.debug(
                    "[Fulfillment] Skipping incoming %s — destination warehouse has no fulfillment owner.", self.name
                )
                return
        elif self.picking_type_code == "outgoing":
            src_wh = self.picking_type_id.warehouse_id
            if src_wh and src_wh.warehouse_role == 'own':
                _logger.debug(
                    "[Fulfillment] Skip outgoing %s — source warehouse '%s' has role='own' (not Fulfillment-managed).",
                    self.name, src_wh.code,
                )
                return
            if not warehouse_out_id:
                raise UserError("[Fulfillment] Cannot push outgoing transfer: missing fulfillment_warehouse_out.")
            if not fulfillment_out:
                raise UserError("[Fulfillment] Cannot push outgoing transfer: missing fulfillment_out.")
        elif self.picking_type_code == "internal":
            if not warehouse_out_id or not warehouse_in_id:
                _logger.debug(
                    "[Fulfillment] Internal transfer %s has no fulfillment warehouse mapping — skipping API push.",
                    self.name,
                )
                return
        payload = PickingAdapter.to_api_payload(
            self, items, warehouse_out_id, warehouse_in_id,
            fulfillment_out, fulfillment_in, []
        )
        if self.partner_id:
            contact_id = self._ensure_contact_synced(client, self.partner_id)
            if contact_id:
                payload["contacts"] = [{"contact_id": str(contact_id), "role": "DELIVERY"}]
                _logger.info("[Fulfillment] Added contact %s (%s)", contact_id, self.partner_id.name)
        _logger.info("[Fulfillment][PAYLOAD] Full data sent to API: %s", json.dumps(payload, indent=2))
        self._sync_transfer(client, payload)

    def _ensure_contact_synced(self, client, partner):
        """Return the Fulfillment API contact_id for partner, registering it if needed."""
        contact_id = getattr(partner, 'fulfillment_contact_id', None)
        if contact_id:
            return contact_id
        # Contact not registered yet — create it in the API.
        try:
            payload = {
                'name': partner.name or '',
                'email': partner.email or '',
                'phone': partner.phone or partner.mobile or '',
            }
            resp = client.contact.create(payload)
            contact_id = (resp.get('data') or resp).get('id') if resp else None
            if contact_id:
                partner.sudo().write({'fulfillment_contact_id': contact_id})
                _logger.info(
                    "[_ensure_contact_synced] Registered contact '%s' → %s",
                    partner.name, contact_id,
                )
            else:
                _logger.warning("[_ensure_contact_synced] API returned no id for '%s': %s", partner.name, resp)
        except Exception as e:
            _logger.warning("[_ensure_contact_synced] Failed to register contact '%s': %s", partner.name, e)
        return contact_id

    def _resolve_warehouses_and_profiles(self, my_fulfillment_id):
        _logger.info("[_resolve_warehouses_and_profiles]")
        warehouse_out_id = None
        warehouse_in_id = None
        fulfillment_out = None
        fulfillment_in = None

        op_type = self.picking_type_id.fulfillment_operation_type if self.picking_type_id else None
        fp = self.picking_type_id.fulfillment_partner_id if self.picking_type_id else None

        # ── Fast path: operation type is explicitly marked for fulfillment ──────────
        if op_type == 'send_to_fulfillment':
            # Our warehouse → fulfillment partner warehouse
            src_wh = self.picking_type_id.warehouse_id
            dest_wh = self.env['stock.warehouse'].search([
                '|',
                ('lot_stock_id', '=', self.location_dest_id.id),
                ('view_location_id', 'parent_of', self.location_dest_id.id),
            ], limit=1)

            # warehouse_out: our warehouse API id
            warehouse_out_id = src_wh.fulfillment_warehouse_id if src_wh else None
            # Fallback: find any own warehouse registered in API
            if not warehouse_out_id and my_fulfillment_id:
                own_wh = self.env['stock.warehouse'].with_context(active_test=False).search(
                    [('fulfillment_owner_id.fulfillment_id', '=', my_fulfillment_id),
                     ('fulfillment_warehouse_id', '!=', False)],
                    limit=1,
                )
                if own_wh:
                    warehouse_out_id = own_wh.fulfillment_warehouse_id

            # warehouse_in: fulfillment partner warehouse API id
            warehouse_in_id = dest_wh.fulfillment_warehouse_id if dest_wh else None

            # fulfillment_out = us
            fulfillment_out = my_fulfillment_id

            # fulfillment_in = partner: prefer picking.fulfillment_partner_id, then picking_type, then dest_wh owner
            picking_fp = self.fulfillment_partner_id
            fulfillment_in = (
                (picking_fp.fulfillment_id if picking_fp else None)
                or (fp.fulfillment_id if fp else None)
                or (dest_wh.fulfillment_owner_id.fulfillment_id if dest_wh and dest_wh.fulfillment_owner_id else None)
            )

            _logger.info(
                "[RESOLVE][SEND_TO_FULFILLMENT] wh_out=%s wh_in=%s ff_out=%s ff_in=%s",
                warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in,
            )
            return warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in

        if op_type == 'request_from_fulfillment':
            # Fulfillment partner warehouse → our warehouse
            src_wh = self.env['stock.warehouse'].search([
                '|',
                ('lot_stock_id', '=', self.location_id.id),
                ('view_location_id', 'parent_of', self.location_id.id),
            ], limit=1)
            dest_wh = self.picking_type_id.warehouse_id

            # warehouse_out: fulfillment partner warehouse API id
            warehouse_out_id = src_wh.fulfillment_warehouse_id if src_wh else None

            # warehouse_in: our warehouse API id
            warehouse_in_id = dest_wh.fulfillment_warehouse_id if dest_wh else None
            if not warehouse_in_id and my_fulfillment_id:
                own_wh = self.env['stock.warehouse'].with_context(active_test=False).search(
                    [('fulfillment_owner_id.fulfillment_id', '=', my_fulfillment_id),
                     ('fulfillment_warehouse_id', '!=', False)],
                    limit=1,
                )
                if own_wh:
                    warehouse_in_id = own_wh.fulfillment_warehouse_id

            # fulfillment_out = partner: prefer picking.fulfillment_partner_id, then picking_type, then src_wh owner
            picking_fp = self.fulfillment_partner_id
            fulfillment_out = (
                (picking_fp.fulfillment_id if picking_fp else None)
                or (fp.fulfillment_id if fp else None)
                or (src_wh.fulfillment_owner_id.fulfillment_id if src_wh and src_wh.fulfillment_owner_id else None)
            )

            # fulfillment_in = us
            fulfillment_in = my_fulfillment_id

            _logger.info(
                "[RESOLVE][REQUEST_FROM_FULFILLMENT] wh_out=%s wh_in=%s ff_out=%s ff_in=%s",
                warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in,
            )
            return warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in

        # ── Legacy fallback: resolve by picking_type_code and locations ────────────
        partner = self.partner_id if getattr(self, "partner_id", False) else None
        if self.picking_type_code == 'incoming':
            dest_wh = self.env['stock.warehouse'].search(
                [('lot_stock_id', '=', self.location_dest_id.id)], limit=1
            )
            warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None) if dest_wh else None

            # For receipts into a RENTED warehouse the supplier is external (no fulfillment ID).
            # The fulfillment partner that manages the rented warehouse must confirm receipt.
            if dest_wh and dest_wh.warehouse_role == 'rented' and dest_wh.fulfillment_owner_id:
                # External goods arrive at a warehouse operated by the fulfillment partner.
                # warehouse_out is null (external supplier), fulfillment_out = warehouse owner.
                warehouse_out_id = None
                fulfillment_out = dest_wh.fulfillment_owner_id.fulfillment_id
                _logger.info(
                    "[RESOLVE][INCOMING/RENTED] dest_wh=%s fulfillment_out=%s wh_in=%s",
                    dest_wh.name, fulfillment_out, warehouse_in_id,
                )
            else:
                # Regular incoming: supplier may be a fulfillment partner
                warehouse_out_id = getattr(partner, "fulfillment_warehouse_id", None)
                fulfillment_out = self._get_partner_fulfillment_profile_id(partner)

            fulfillment_in = my_fulfillment_id
        elif self.picking_type_code == 'outgoing':
            src_wh = self.picking_type_id.warehouse_id
            warehouse_out_id = src_wh.fulfillment_warehouse_id if src_wh else None
            fulfillment_out = (
                src_wh.fulfillment_owner_id.fulfillment_id
                if src_wh and src_wh.fulfillment_owner_id
                else my_fulfillment_id
            )
            if not warehouse_out_id and my_fulfillment_id:
                my_fp = self.env['fulfillment.partners'].search(
                    [('fulfillment_id', '=', my_fulfillment_id)], limit=1
                )
                fallback_wh = self.env['stock.warehouse'].with_context(active_test=False).search(
                    [('fulfillment_owner_id', '=', my_fp.id), ('fulfillment_warehouse_id', '!=', False)],
                    limit=1
                )
                if fallback_wh:
                    warehouse_out_id = fallback_wh.fulfillment_warehouse_id
                    _logger.info(
                        "[RESOLVE][OUTGOING] warehouse_out fallback to %s (%s)",
                        fallback_wh.name, warehouse_out_id,
                    )
            dest_wh = self.env['stock.warehouse'].search([
                '|',
                ('lot_stock_id', '=', self.location_dest_id.id),
                ('view_location_id', 'parent_of', self.location_dest_id.id),
            ], limit=1)
            if dest_wh and dest_wh.fulfillment_owner_id and dest_wh.fulfillment_owner_id.fulfillment_id:
                fulfillment_in = dest_wh.fulfillment_owner_id.fulfillment_id
            elif dest_wh and dest_wh.fulfillment_warehouse_id:
                linked_fp = self.env['fulfillment.partners'].search(
                    [('fulfillment_warehouse_id', '=', dest_wh.fulfillment_warehouse_id)], limit=1
                )
                fulfillment_in = linked_fp.fulfillment_id if linked_fp else my_fulfillment_id
            else:
                fulfillment_in = my_fulfillment_id
            if dest_wh and dest_wh.fulfillment_warehouse_id:
                warehouse_in_id = dest_wh.fulfillment_warehouse_id
            _logger.info(
                "[RESOLVE][OUTGOING] src_wh=%s dest_wh=%s warehouse_out=%s warehouse_in=%s fulfillment_out=%s fulfillment_in=%s",
                src_wh.name if src_wh else None,
                dest_wh.name if dest_wh else None,
                warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in,
            )
        elif self.picking_type_code == 'internal':
            src_wh = self.env['stock.warehouse'].search(
                [('lot_stock_id', '=', self.location_id.id)], limit=1
            )
            dest_wh = self.env['stock.warehouse'].search(
                [('lot_stock_id', '=', self.location_dest_id.id)], limit=1
            )
            warehouse_out_id = getattr(src_wh, "fulfillment_warehouse_id", None) if src_wh else None
            warehouse_in_id = getattr(dest_wh, "fulfillment_warehouse_id", None) if dest_wh else None
            fulfillment_out = (
                src_wh.fulfillment_owner_id.fulfillment_id
                if src_wh and getattr(src_wh, "fulfillment_owner_id", None)
                and getattr(src_wh.fulfillment_owner_id, "fulfillment_id", None)
                else my_fulfillment_id
            )
            fulfillment_in = (
                dest_wh.fulfillment_owner_id.fulfillment_id
                if dest_wh and getattr(dest_wh, "fulfillment_owner_id", None)
                and getattr(dest_wh.fulfillment_owner_id, "fulfillment_id", None)
                else my_fulfillment_id
            )
        return warehouse_out_id, warehouse_in_id, fulfillment_out, fulfillment_in

    def _sync_transfer(self, client, payload):
        _logger.info("[_sync_transfer]")
        try:
            if not self.fulfillment_transfer_id or self.fulfillment_transfer_id == "Empty":
                _logger.info("[Fulfillment][CREATE] Calling API for %s", self.name)
                response = client.transfer.create(payload)
                data_list = response.get("data")
                if not data_list:
                    _logger.error("[Fulfillment] API returned empty data list: %s", response)
                    return
                data_list = response.get('data')
                if isinstance(data_list, dict):
                    data_list = [data_list]
                api_data = data_list[0]
                remote_id = api_data.get("id")
                owner_id = api_data.get("fulfillment_in")
                if not remote_id:
                    _logger.error("[Fulfillment] Could not find 'id' in API response data")
                    return
                out_id = api_data.get("fulfillment_out") or "Empty"
                self.with_context(skip_fulfillment_push=True).write({
                    "fulfillment_transfer_id": remote_id,
                    "fulfillment_transfer_owner_id": owner_id or "Empty",
                    "fulfillment_transfer_out_id": out_id,
                })
                _logger.info("[Fulfillment] Success! Linked %s to API ID: %s", self.name, remote_id)
                # If the picking is already validated, push the current state immediately.
                odoo_to_api = {"draft": "draft", "confirmed": "confirmed", "assigned": "assigned", "done": "done"}
                api_status = odoo_to_api.get(self.state)
                if api_status and api_status != "draft":
                    try:
                        client.transfer.update(remote_id, {"status": api_status})
                        _logger.info("[Fulfillment] Post-create status push: %s → %s", remote_id, api_status)
                    except Exception as se:
                        _logger.warning("[Fulfillment] Post-create status push failed: %s", se)
            else:
                _logger.info("[Fulfillment][UPDATE] Updating existing transfer %s", self.fulfillment_transfer_id)
                client.transfer.update(self.fulfillment_transfer_id, payload)
        except Exception as e:
            _logger.error("[Fulfillment][ERROR] Sync failed for %s: %s", self.name, str(e), exc_info=True)

    @property
    def fulfillment_api(self):
        _logger.info("[fulfillment_api]")
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found")
            return None
        return FulfillmentAPIClient(profile)

    @api.model
    def import_transfers(self, fulfillment_id=None, page=1, limit=50):
        _logger.info("[import_transfers]")
        profile = self.env['fulfillment.profile'].search([], limit=1)
        if not profile:
            _logger.warning("[Fulfillment] Profile not found")
            return False
        # Default to this instance's own profile ID so the API returns
        # all transfers involving us (as sender or receiver).
        if not fulfillment_id:
            fulfillment_id = getattr(profile, 'fulfillment_profile_id', None)
        if not fulfillment_id:
            _logger.warning("[Fulfillment] No fulfillment_profile_id — cannot filter transfers")
            return False
        client = FulfillmentAPIClient(profile)
        try:
            response = client.transfer.list(fulfillment_id=fulfillment_id, page=page, limit=limit)
        except Exception as e:
            _logger.error("[Fulfillment] Error fetching transfers: %s", e)
            return False
        data = response.get("data")
        if not isinstance(data, list):
            _logger.warning("[Fulfillment] Invalid response format: %s", response)
            return False
        for transfer in response.get("data", []):
            try:
                with self.env.cr.savepoint():
                    self._import_transfer(transfer)
            except Exception as e:
                _logger.error("[Fulfillment][IMPORT] Failed transfer %s: %s", transfer.get("id"), e, exc_info=True)
        # Advance picking-type sequences past any names that were explicitly assigned
        # during import to prevent future sequence collisions.
        self._sync_picking_sequences()
        return True

    @api.model
    def _sync_picking_sequences(self):
        """Advance ir.sequence counters past max picking names to avoid conflicts."""
        import re as _re
        picking_types = self.env['stock.picking.type'].search([])
        for pt in picking_types:
            if not pt.sequence_id:
                continue
            prefix = pt.sequence_id.prefix or ''
            padding = pt.sequence_id.padding or 5
            pattern = f'^{_re.escape(prefix)}\\d{{{padding}}}$'
            self.env.cr.execute(
                "SELECT MAX(CAST(SUBSTRING(name, %s, %s) AS INTEGER)) "
                "FROM stock_picking WHERE name ~ %s AND company_id = %s",
                (len(prefix) + 1, padding, pattern, self.env.company.id),
            )
            row = self.env.cr.fetchone()
            if row and row[0]:
                max_num = row[0]
                cur_seq = pt.sequence_id.number_next
                if cur_seq <= max_num:
                    pt.sequence_id.sudo().write({'number_next': max_num + 1})
                    _logger.info("[_sync_picking_sequences] %sXXXXX: seq %s → %s", prefix, cur_seq, max_num + 1)

    def _import_transfer(self, transfer):
        _logger.info("[_import_transfer]")
        remote_id = str(transfer.get("id"))
        if not remote_id:
            _logger.warning("[Fulfillment] Transfer without ID skipped")
            return False

        # Skip transfers where THIS instance is not involved (neither sender nor receiver).
        # The catch-up cron iterates over all partners, so we must guard against importing
        # transfers that belong entirely to another fulfillment account.
        profile = self.env['fulfillment.profile'].search([], limit=1)
        my_fid = profile.fulfillment_profile_id if profile else None
        if my_fid:
            f_in = transfer.get("fulfillment_in")
            f_out = transfer.get("fulfillment_out")
            if f_in and f_out and f_in != my_fid and f_out != my_fid:
                # Check if the transfer is already locally tracked (keep it updated)
                already_local = self.search([
                    ("fulfillment_transfer_id", "=", remote_id),
                    ("company_id", "=", self.env.company.id),
                ], limit=1)
                if not already_local:
                    _logger.info(
                        "[_import_transfer] Skipping transfer %s (%s) — this instance (%s) is not involved"
                        " (f_in=%s, f_out=%s)",
                        remote_id, transfer.get("reference"), my_fid, f_in, f_out,
                    )
                    return False
        wh_in_ext = transfer.get("warehouse_in")
        wh_out_ext = transfer.get("warehouse_out")
        status = transfer.get("status")
        contacts = transfer.get("contacts") or []
        warehouse_in = self.env["stock.warehouse"].search([("fulfillment_warehouse_id", "=", wh_in_ext)], limit=1)
        warehouse_out = self.env["stock.warehouse"].search([("fulfillment_warehouse_id", "=", wh_out_ext)], limit=1)
        location_id = warehouse_out.lot_stock_id.id if warehouse_out else False
        location_dest_id = warehouse_in.lot_stock_id.id if warehouse_in else False

        # For cross-instance transfers one side's warehouse may not exist locally.
        # Use standard virtual locations as fallback so location_id/location_dest_id are never NULL.
        if not location_id:
            supplier_loc = (
                self.env.ref('stock.stock_location_suppliers', raise_if_not_found=False)
                or self.env['stock.location'].search([('usage', '=', 'supplier')], limit=1)
            )
            location_id = supplier_loc.id if supplier_loc else False
            if location_id:
                _logger.info("[_import_transfer] location_id fallback to supplier virtual: %s", location_id)
        if not location_dest_id:
            customer_loc = (
                self.env.ref('stock.stock_location_customers', raise_if_not_found=False)
                or self.env['stock.location'].search([('usage', '=', 'customer')], limit=1)
            )
            location_dest_id = customer_loc.id if customer_loc else False
            if location_dest_id:
                _logger.info("[_import_transfer] location_dest_id fallback to customer virtual: %s", location_dest_id)

        partner_id = self._find_or_create_partner(contacts, wh_in_ext)
        picking = self._create_or_update_picking(
            remote_id, transfer, location_id, location_dest_id, partner_id, warehouse_out, warehouse_in
        )
        self._create_transfer_items(transfer, picking, location_id, location_dest_id)
        try:
            picking.with_context(skip_fulfillment_push=True)._apply_status(status)
            _logger.info("[Fulfillment] Status applied: %s -> %s", picking.name, status)
        except Exception as e:
            _logger.warning("[Fulfillment] Error applying status %s: %s", status, e)
        if picking.state not in ('done', 'cancel') and status not in ('done',):
            try:
                if picking.state == 'draft':
                    picking.with_context(skip_fulfillment_push=True).action_confirm()
                    _logger.info("[Fulfillment] Auto-confirmed imported transfer %s", picking.name)
                if picking.state == 'confirmed':
                    picking.with_context(skip_fulfillment_push=True).action_assign()
                    if picking.state == 'assigned':
                        _logger.info("[Fulfillment] Auto-assigned imported transfer %s — stock available", picking.name)
                    else:
                        _logger.info("[Fulfillment] Imported transfer %s — insufficient stock (state=%s)", picking.name, picking.state)
            except Exception as e:
                _logger.warning("[Fulfillment] Auto-advance failed for %s: %s", picking.name, e)
        return picking

    def _find_or_create_partner(self, contacts, wh_in_ext):
        _logger.info("[_find_or_create_partner]")
        partner_id = False
        contact_data = next(
            (c for c in contacts if (c.get("role") or "").upper() in ("DELIVERY", "CUSTOMER")),
            (contacts[0] if contacts else None),
        )
        if contact_data:
            cid = contact_data.get("contact_id")
            contact_info = contact_data.get("contact") or {}
            cname = contact_info.get("name") or ""
            cphone = contact_info.get("phone") or ""
            cemail = contact_info.get("email") or ""
            partner = self.env["res.partner"].search([("fulfillment_contact_id", "=", cid)], limit=1)
            if not partner:
                domain = [("name", "=", cname)]
                if cphone:
                    domain.append(("phone", "=", cphone))
                partner = self.env["res.partner"].search(domain, limit=1)
            if not partner:
                partner = self.env["res.partner"].create({
                    "name": cname, "phone": cphone, "email": cemail, "fulfillment_contact_id": cid,
                })
                _logger.info("[Fulfillment] Created partner: %s (%s)", cname, cid)
            else:
                update_vals = {}
                if not partner.fulfillment_contact_id and cid:
                    update_vals["fulfillment_contact_id"] = cid
                if cname and partner.name in {"NoName", "Unknown", "", False, None}:
                    update_vals["name"] = cname
                if cphone and not partner.phone:
                    update_vals["phone"] = cphone
                if cemail and not partner.email:
                    update_vals["email"] = cemail
                if update_vals:
                    partner.write(update_vals)
                    _logger.info("[Fulfillment] Updated partner %s: %s", partner.id, update_vals)
            partner_id = partner.id
        if not partner_id:
            partner_fallback = self.env["res.partner"].search([("fulfillment_warehouse_id", "=", wh_in_ext)], limit=1)
            partner_id = partner_fallback.id if partner_fallback else False
        return partner_id

    def _create_or_update_picking(self, remote_id, transfer, location_id, location_dest_id, partner_id, warehouse_out, warehouse_in):
        _logger.info("[_create_or_update_picking]")
        picking = self.search([("fulfillment_transfer_id", "=", remote_id), ("company_id", "=", self.env.company.id)], limit=1)
        picking_type_id = self._map_type(transfer)
        picking_type = self.env["stock.picking.type"].browse(picking_type_id) if picking_type_id else self.env["stock.picking.type"]
        type_code = picking_type.code if picking_type else "unknown"
        wh_code = (warehouse_out.code if warehouse_out else None) or (warehouse_in.code if warehouse_in else None) or "WH"
        type_short = {"incoming": "IN", "outgoing": "OUT", "internal": "INT"}.get(type_code, "UNK")
        # Use 12 hex chars from the UUID (without dashes) to reduce collision chance.
        hash_part = str(remote_id).replace("-", "")[:12]
        transfer_reference = (transfer.get("reference") or "").strip()
        fallback_name = f"[F] {wh_code}/{type_short}/{hash_part}"
        name = transfer_reference or fallback_name

        # Use picking type defaults as last-resort fallback for locations
        if not location_id and picking_type and picking_type.default_location_src_id:
            location_id = picking_type.default_location_src_id.id
            _logger.info("[_create_or_update_picking] location_id fallback to picking type default: %s", location_id)
        if not location_dest_id and picking_type and picking_type.default_location_dest_id:
            location_dest_id = picking_type.default_location_dest_id.id
            _logger.info("[_create_or_update_picking] location_dest_id fallback to picking type default: %s", location_dest_id)
        # Emergency fallback: search by location usage
        if not location_id:
            loc = self.env['stock.location'].search([('usage', '=', 'supplier')], limit=1)
            if loc:
                location_id = loc.id
                _logger.info("[_create_or_update_picking] location_id emergency fallback (usage=supplier): %s", location_id)
        if not location_dest_id:
            loc = self.env['stock.location'].search([('usage', '=', 'customer')], limit=1)
            if loc:
                location_dest_id = loc.id
                _logger.info("[_create_or_update_picking] location_dest_id emergency fallback (usage=customer): %s", location_dest_id)

        if not location_id or not location_dest_id:
            _logger.error(
                "[_create_or_update_picking] Cannot resolve locations for transfer %s "
                "(type=%s location_id=%s location_dest_id=%s) — skipping",
                remote_id, type_code, location_id, location_dest_id,
            )
            return self.env["stock.picking"]

        vals = {
            "fulfillment_transfer_id": remote_id,
            "fulfillment_transfer_owner_id": transfer.get("fulfillment_in") or "Empty",
            "fulfillment_transfer_out_id": transfer.get("fulfillment_out") or "Empty",
            "picking_type_id": picking_type_id,
            "partner_id": partner_id,
            "location_id": location_id,
            "location_dest_id": location_dest_id,
        }
        if transfer_reference:
            vals["origin"] = transfer_reference
        if picking:
            vals_write = dict(vals)
            vals_write.pop("picking_type_id", None)
            my_profile = self.env['fulfillment.profile'].sudo().search([], limit=1)
            my_fid = my_profile.fulfillment_profile_id if my_profile else None
            is_imported = (
                transfer.get("fulfillment_out") == my_fid
                or (picking.name or "").startswith("[F]")
            )
            if transfer_reference and is_imported and picking.name != transfer_reference:
                vals_write["name"] = name
            picking.write(vals_write)
            _logger.info("[Fulfillment] Updated picking %s", picking.name)
        else:
            # Avoid name collision: if the API reference already exists as a local picking
            # (e.g. same sequential number generated on both instances), use the fallback name.
            # Use raw SQL to detect existing names (sees uncommitted rows in the same tx).
            chosen_name = name
            if transfer_reference:
                self.env.cr.execute(
                    "SELECT 1 FROM stock_picking WHERE name = %s AND company_id = %s LIMIT 1",
                    (transfer_reference, self.env.company.id),
                )
                if self.env.cr.fetchone():
                    chosen_name = fallback_name
                    _logger.info(
                        "[_create_or_update_picking] Name '%s' already taken — trying fallback '%s'",
                        transfer_reference, fallback_name,
                    )
            # Also ensure the fallback itself is not taken.
            if chosen_name == fallback_name:
                self.env.cr.execute(
                    "SELECT 1 FROM stock_picking WHERE name = %s AND company_id = %s LIMIT 1",
                    (fallback_name, self.env.company.id),
                )
                if self.env.cr.fetchone():
                    # Last resort: use full UUID as name suffix.
                    chosen_name = f"[F] {remote_id}"
                    _logger.info(
                        "[_create_or_update_picking] Fallback '%s' also taken — using UUID name '%s'",
                        fallback_name, chosen_name,
                    )
            vals["name"] = chosen_name
            picking = self.with_context(skip_fulfillment_push=True).create(vals)
            _logger.info("[Fulfillment] Created picking %s", picking.name)
        return picking

    def _create_transfer_items(self, transfer, picking, location_id, location_dest_id):
        _logger.info("[_create_transfer_items]")
        items = transfer.get("items", [])
        if not items:
            return
        # Skip move updates for already-completed/cancelled pickings to avoid
        # "cannot change UoM for a Done move" errors.
        if picking.state in ('done', 'cancel'):
            _logger.info("[_create_transfer_items] Picking %s is %s — skipping move sync", picking.name, picking.state)
            return
        Move = self.env["stock.move"]
        for item in items:
            product_data = item.get("product") or {}
            fulfillment_product_id = item.get("product_id") or product_data.get("product_id")
            if fulfillment_product_id and not product_data:
                fetched = self._fetch_product_from_api(fulfillment_product_id)
                if fetched:
                    product_data = fetched
            _prod_name_raw = product_data.get("name") or ""
            prod_name = (
                _prod_name_raw
                or product_data.get("sku")
                or product_data.get("barcode")
                or (f"SKU-{fulfillment_product_id}" if fulfillment_product_id else "Unnamed")
            )
            sku = product_data.get("sku")
            barcode = product_data.get("barcode")
            img_url = product_data.get("img_url")
            product_tmpl = self._find_or_create_product(fulfillment_product_id, sku, prod_name, barcode, img_url=img_url)
            product_id = product_tmpl.product_variant_id.id
            quantity = float(item.get("quantity") or 0.0)
            move_vals = {
                "name": prod_name, "product_id": product_id, "product_uom_qty": quantity,
                "product_uom": product_tmpl.uom_id.id, "picking_id": picking.id,
                "location_id": location_id, "location_dest_id": location_dest_id, "state": "draft",
            }
            existing_move = Move.search([("picking_id", "=", picking.id), ("product_id", "=", product_id)], limit=1)
            if existing_move:
                if existing_move.state in ('done', 'cancel'):
                    _logger.info("[_create_transfer_items] Move %s is %s — skipping update", existing_move.id, existing_move.state)
                    continue
                existing_move.write(move_vals)
            else:
                Move.create(move_vals)

    def _fetch_image_b64(self, img_url):
        if not img_url or not img_url.startswith(('http://', 'https://')):
            return None
        try:
            resp = _requests.get(img_url, timeout=10, verify=False)
            if resp.status_code == 200 and resp.content:
                return base64.b64encode(resp.content)
        except Exception as e:
            _logger.warning('[Fulfillment] Failed to fetch image from %s: %s', img_url, e)
        return None

    def _resolve_img_url(self, img_url, fulfillment_product_id):
        if img_url and img_url.startswith(('http://', 'https://')):
            return img_url
        candidate_relative = img_url if (img_url and img_url.startswith('/')) else None
        if fulfillment_product_id:
            try:
                product_data = self._fetch_product_from_api(fulfillment_product_id)
                if product_data:
                    url = product_data.get('img_url')
                    if url and url.startswith(('http://', 'https://')):
                        return url
                    if url and url.startswith('/'):
                        candidate_relative = url
            except Exception as e:
                _logger.warning('[Fulfillment] Could not resolve img_url for product %s: %s', fulfillment_product_id, e)
        if candidate_relative:
            partners = self.env['fulfillment.partners'].sudo().search([('webhook_domain', '!=', False)])
            for partner in partners:
                domain = (partner.webhook_domain or '').strip().rstrip('/')
                if not domain:
                    continue
                if not domain.startswith(('http://', 'https://')):
                    domain = f'https://{domain}'
                full_url = domain + candidate_relative
                try:
                    resp = _requests.head(full_url, timeout=5, verify=False)
                    if resp.status_code == 200:
                        _logger.info('[Fulfillment] Resolved relative img_url via partner domain: %s', full_url)
                        return full_url
                except Exception:
                    pass
        return None

    def _find_or_create_product(self, fulfillment_product_id, sku, prod_name, barcode, img_url=None):
        _logger.info("[_find_or_create_product]")
        ProductTmpl = self.env["product.template"]
        product_tmpl = False
        if fulfillment_product_id:
            product_tmpl = ProductTmpl.search([("fulfillment_product_id", "=", fulfillment_product_id)], limit=1)
        if not product_tmpl and sku:
            product_tmpl = ProductTmpl.search([("default_code", "=", sku)], limit=1)
        if not product_tmpl:
            create_vals = {
                "name": prod_name, "type": "consu", "is_storable": True,
                "uom_id": self.env.ref("uom.product_uom_unit").id,
                "uom_po_id": self.env.ref("uom.product_uom_unit").id,
                "default_code": sku, "fulfillment_product_id": fulfillment_product_id,
            }
            if barcode:
                create_vals["barcode"] = barcode
            resolved_url = self._resolve_img_url(img_url, fulfillment_product_id)
            image_b64 = self._fetch_image_b64(resolved_url)
            if image_b64:
                create_vals["image_1920"] = image_b64
                _logger.info("[Fulfillment] Image downloaded for new product '%s' from %s", prod_name, resolved_url)
            product_tmpl = ProductTmpl.create(create_vals)
            if not product_tmpl.product_variant_ids:
                product_tmpl._create_variant_ids()
                product_tmpl.flush_recordset()
            _logger.info("[Fulfillment] Created product '%s'", prod_name)
        else:
            _prod_update = {}
            if not product_tmpl.fulfillment_product_id and fulfillment_product_id:
                _prod_update["fulfillment_product_id"] = fulfillment_product_id
            if sku and not product_tmpl.default_code:
                _prod_update["default_code"] = sku
            if barcode and not product_tmpl.barcode:
                _prod_update["barcode"] = barcode
            _placeholder_prefixes = ("SKU-", "Unnamed", "Name not found", "NoName")
            _is_placeholder = (not product_tmpl.name) or any(product_tmpl.name.startswith(p) for p in _placeholder_prefixes)
            _real_name = prod_name and not any(prod_name.startswith(p) for p in _placeholder_prefixes)
            if _is_placeholder and _real_name:
                _prod_update["name"] = prod_name
            if _prod_update:
                product_tmpl.with_context(skip_fulfillment_push=True).write(_prod_update)
                _logger.info("[Fulfillment] Updated product %s: %s", product_tmpl.id, _prod_update)
            if not product_tmpl.image_1920:
                resolved_url = self._resolve_img_url(img_url, fulfillment_product_id)
                image_b64 = self._fetch_image_b64(resolved_url)
                if image_b64:
                    product_tmpl.with_context(skip_fulfillment_push=True).write({"image_1920": image_b64})
                    _logger.info("[Fulfillment] Image set for existing product '%s' from %s", product_tmpl.name, resolved_url)
        return product_tmpl

    def _apply_status(self, target_status):
        _logger.info("[_apply_status]")
        self.ensure_one()
        state = self.state
        sequence = ["draft", "confirmed", "assigned", "done"]
        if target_status not in sequence:
            _logger.warning("[Fulfillment] Unknown status: %s", target_status)
            return
        current_i = sequence.index(state)
        target_i = sequence.index(target_status)
        # When the remote side confirms receipt (done), update our delivery status.
        # This must happen even if local picking is already 'done' (sender scenario:
        # local = done, API becomes done when receiver confirms → mark as delivered).
        if target_status == 'done' and self.fulfillment_delivery_status == 'delivering':
            self.with_context(skip_fulfillment_push=True).write({'fulfillment_delivery_status': 'delivered'})
            _logger.info("[Fulfillment] Set fulfillment_delivery_status=delivered for %s", self.name)
        if current_i >= target_i:
            return
        if current_i < sequence.index("confirmed") and target_i >= sequence.index("confirmed"):
            self.with_context(skip_fulfillment_push=True).action_confirm()
            state = self.state
        if state == "confirmed" and target_i >= sequence.index("assigned"):
            self.with_context(skip_fulfillment_push=True).action_assign()
            state = self.state
        if state in ("assigned", "confirmed") and target_i >= sequence.index("done"):
            for move in self.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                if not move.quantity:
                    move.quantity = move.product_uom_qty
                    _logger.info("[Fulfillment][_apply_status] Set quantity=%.2f for move '%s'", move.product_uom_qty, move.product_id.display_name)
            # Mark as delivered BEFORE button_validate so the cross_instance_sender
            # logic in button_validate does not downgrade the status back to 'delivering'.
            if self._is_managed_by_fulfillment():
                self.with_context(skip_fulfillment_push=True).write({'fulfillment_delivery_status': 'delivered'})
            self.with_context(skip_backorder=True, skip_fulfillment_push=True, from_fulfillment_import=True).button_validate()

    def _map_type(self, transfer, current_picking=None):
        _logger.info("[_map_type]")
        tr_type = transfer.get("transfer_type") or transfer.get("type")

        # Determine the operation type from the perspective of THIS Odoo instance.
        # The API stores transfer_type from the sender's point of view (outgoing).
        # When we are the receiver (fulfillment_in == my fulfillment_id), the same
        # transfer must be imported as "incoming" so stock lands in our warehouse.
        profile = self.env['fulfillment.profile'].search([], limit=1)
        my_id = profile.fulfillment_profile_id if profile else None
        fulfillment_in = transfer.get("fulfillment_in")
        fulfillment_out = transfer.get("fulfillment_out")

        if tr_type == "outgoing" and my_id and fulfillment_in == my_id:
            # Sender pushed an outgoing; we are the destination → incoming for us
            op_code = "incoming"
        elif tr_type == "incoming" and my_id and fulfillment_out == my_id:
            # Sender (Handler) pushed an incoming PO receipt; we physically manage the warehouse
            # and must confirm receipt → incoming for us too (goods arrive at our warehouse).
            op_code = "incoming"
        elif tr_type == "internal":
            if my_id and fulfillment_in and fulfillment_out and fulfillment_in != fulfillment_out:
                # Cross-instance "internal": each side sees it as delivery/receipt
                if fulfillment_in == my_id:
                    op_code = "incoming"   # goods arrive at our warehouse
                elif fulfillment_out == my_id:
                    op_code = "outgoing"   # goods leave our warehouse
                else:
                    op_code = "internal"
            else:
                op_code = "internal"
        elif tr_type in ("incoming", "outgoing"):
            op_code = tr_type
        else:
            if current_picking:
                return current_picking.picking_type_id.id
            return False

        op_type = self.env["stock.picking.type"].search([("code", "=", op_code)], limit=1)
        return op_type.id if op_type else (current_picking.picking_type_id.id if current_picking else False)
