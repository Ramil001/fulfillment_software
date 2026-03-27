/** @odoo-module **/
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

class FulfillmentNotifier {
    constructor(env, deps) {
        this.env = env;
        this.bus = deps["bus_service"];
        this.notification = deps["notification"];
        this.action = deps["action"];
        // mail.store gives us access to the Thread model for real-time refresh
        this.mailStore = deps["mail.store"] || null;
        this._onNotification = this._onNotification.bind(this);
    }

    async start() {
        this.bus.subscribe("fulfillment_new_message", this._onNotification);
    }

    _onNotification(payload) {
        if (!payload || !payload.content) return;

        const partnerName = payload.partner_name || "Fulfillment";
        const content = payload.content.length > 80
            ? payload.content.slice(0, 80) + "…"
            : payload.content;

        // ── Refresh the chatter in real time ─────────────────────────────────
        // When the user is on the same picking/partner page, update the thread
        // without requiring a manual page refresh.
        this._refreshThread(payload);

        // ── Show a popup notification ─────────────────────────────────────────
        let closeNotif;
        const buttons = [];

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

    /**
     * Refresh the thread matching the payload's model/res_id if currently open.
     * Uses the mail.store Thread model to fetch new messages without page reload.
     */
    _refreshThread(payload) {
        if (!payload.model || !payload.res_id || !this.mailStore) return;
        try {
            const Thread = this.mailStore.Thread;
            if (!Thread) return;
            // Thread.get returns the cached thread if already loaded in this tab
            const thread = Thread.get({ model: payload.model, id: payload.res_id });
            if (thread && typeof thread.fetchNewMessages === "function") {
                thread.fetchNewMessages();
            }
        } catch (_e) {
            // Fail silently — the user can still refresh manually
        }
    }
}

registry.category("services").add("fulfillment_notifier", {
    dependencies: ["bus_service", "notification", "action", "mail.store"],
    async start(env, deps) {
        const notifier = new FulfillmentNotifier(env, deps);
        await notifier.start();
        return notifier;
    },
});
