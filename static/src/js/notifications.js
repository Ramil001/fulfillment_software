/** @odoo-module **/
import { registry } from "@web/core/registry";

class FulfillmentNotifier {
    constructor(env, deps) {
        this.env = env;
        this.bus = deps["bus_service"];
        // mail.store gives us access to the Thread model for real-time refresh
        this.mailStore = deps["mail.store"] || null;
        this._onNotification = this._onNotification.bind(this);
    }

    async start() {
        this.bus.subscribe("fulfillment_new_message", this._onNotification);
    }

    _onNotification(payload) {
        if (!payload || !payload.content) return;

        // Refresh the chatter in real time when the user is on the same page.
        // Odoo's own inbox notification already handles the top-bar bell icon,
        // so we don't show an extra popup here to avoid duplicates.
        this._refreshThread(payload);
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
    dependencies: ["bus_service", "mail.store"],
    async start(env, deps) {
        const notifier = new FulfillmentNotifier(env, deps);
        await notifier.start();
        return notifier;
    },
});
