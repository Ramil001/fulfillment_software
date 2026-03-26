/** @odoo-module **/
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

class FulfillmentNotifier {
    constructor(env, { bus_service, notification, action }) {
        this.env = env;
        this.bus = bus_service;
        this.notification = notification;
        this.action = action;
        this._onNotification = this._onNotification.bind(this);
    }

    async start() {
        // Subscribe to our custom fulfillment_new_message channel
        this.bus.subscribe("fulfillment_new_message", this._onNotification);
        _logger.log("[FulfillmentNotifier] subscribed to fulfillment_new_message");
    }

    _onNotification(payload) {
        if (!payload || !payload.content) return;

        const partnerName = payload.partner_name || "Fulfillment";
        const content = payload.content.length > 80
            ? payload.content.slice(0, 80) + "…"
            : payload.content;

        let closeNotif;
        const buttons = [];

        // If message is linked to a picking, add "Open" button
        if (payload.picking_id) {
            buttons.push({
                name: _t("Open"),
                primary: true,
                onClick: () => {
                    if (closeNotif) closeNotif();
                    this.action.doAction({
                        type: "ir.actions.act_window",
                        res_model: "stock.picking",
                        res_id: payload.picking_id,
                        views: [[false, "form"]],
                        target: "current",
                    });
                },
            });
        } else if (payload.partner_id) {
            buttons.push({
                name: _t("Open"),
                primary: true,
                onClick: () => {
                    if (closeNotif) closeNotif();
                    this.action.doAction({
                        type: "ir.actions.act_window",
                        res_model: "fulfillment.partners",
                        res_id: payload.partner_id,
                        views: [[false, "form"]],
                        target: "current",
                    });
                },
            });
        }

        buttons.push({
            name: _t("Close"),
            onClick: () => { if (closeNotif) closeNotif(); },
        });

        closeNotif = this.notification.add(content, {
            title: partnerName,
            type: "info",
            sticky: false,
            buttons,
        });
    }
}

// Silence the logger reference above (it's just for debug)
const _logger = { log: () => {} };

registry.category("services").add("fulfillment_notifier", {
    dependencies: ["bus_service", "notification", "action"],
    async start(env, deps) {
        const notifier = new FulfillmentNotifier(env, deps);
        await notifier.start();
        return notifier;
    },
});
