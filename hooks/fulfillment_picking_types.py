# -*- coding: utf-8 -*-
"""
Post-install hook: create dedicated "Send to Fulfillment" and
"Request from Fulfillment" operation types for every rented/leased-out
warehouse pair that already exists in the database.

This is idempotent — if the types already exist (e.g. on a module upgrade)
they are left unchanged.
"""
import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def create_fulfillment_picking_types(env):
    """Odoo 18 post_init_hook receives env directly."""
    try:
        _create_fulfillment_picking_types(env)
    except Exception as e:
        _logger.exception("Failed to create fulfillment picking types: %s", e)


def _create_fulfillment_picking_types(env):
    PickingType = env["stock.picking.type"]
    Warehouse = env["stock.warehouse"]

    # Own warehouses (our local warehouses that will be the source for Send /
    # the destination for Request)
    own_warehouses = Warehouse.search([("warehouse_role", "=", "own")])
    # Partner warehouses (rented from a fulfillment partner)
    partner_warehouses = Warehouse.search([
        ("warehouse_role", "in", ["rented", "leased_out"]),
        ("fulfillment_warehouse_id", "!=", False),
    ])

    for own_wh in own_warehouses:
        for partner_wh in partner_warehouses:
            fp = partner_wh.fulfillment_owner_id
            _ensure_send_type(env, PickingType, own_wh, partner_wh, fp)
            _ensure_request_type(env, PickingType, own_wh, partner_wh, fp)


def _ensure_send_type(env, PickingType, own_wh, partner_wh, fp):
    """Create 'Send to Fulfillment' outgoing operation type if missing."""
    existing = PickingType.search([
        ("fulfillment_operation_type", "=", "send_to_fulfillment"),
        ("warehouse_id", "=", own_wh.id),
        ("fulfillment_partner_id", "=", fp.id if fp else False),
    ], limit=1)
    if existing:
        return

    out_type = PickingType.search([
        ("code", "=", "outgoing"),
        ("warehouse_id", "=", own_wh.id),
    ], limit=1)

    name = f"Send to {partner_wh.name}"
    vals = {
        "name": name,
        "code": "outgoing",
        "warehouse_id": own_wh.id,
        "fulfillment_operation_type": "send_to_fulfillment",
        "fulfillment_partner_id": fp.id if fp else False,
        "default_location_src_id": own_wh.lot_stock_id.id,
        "default_location_dest_id": partner_wh.lot_stock_id.id,
        "sequence_code": "FOUT",
    }
    if out_type:
        vals["return_picking_type_id"] = out_type.id

    new_type = PickingType.create(vals)
    _logger.info(
        "[FulfillmentPickingTypes] Created 'Send to Fulfillment' type %s (id=%s) for %s → %s",
        name, new_type.id, own_wh.name, partner_wh.name,
    )


def _ensure_request_type(env, PickingType, own_wh, partner_wh, fp):
    """Create 'Request from Fulfillment' incoming operation type if missing."""
    existing = PickingType.search([
        ("fulfillment_operation_type", "=", "request_from_fulfillment"),
        ("warehouse_id", "=", own_wh.id),
        ("fulfillment_partner_id", "=", fp.id if fp else False),
    ], limit=1)
    if existing:
        return

    in_type = PickingType.search([
        ("code", "=", "incoming"),
        ("warehouse_id", "=", own_wh.id),
    ], limit=1)

    name = f"Request from {partner_wh.name}"
    vals = {
        "name": name,
        "code": "incoming",
        "warehouse_id": own_wh.id,
        "fulfillment_operation_type": "request_from_fulfillment",
        "fulfillment_partner_id": fp.id if fp else False,
        "default_location_src_id": partner_wh.lot_stock_id.id,
        "default_location_dest_id": own_wh.lot_stock_id.id,
        "sequence_code": "FIN",
    }
    if in_type:
        vals["return_picking_type_id"] = in_type.id

    new_type = PickingType.create(vals)
    _logger.info(
        "[FulfillmentPickingTypes] Created 'Request from Fulfillment' type %s (id=%s) for %s ← %s",
        name, new_type.id, own_wh.name, partner_wh.name,
    )
