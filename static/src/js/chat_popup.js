/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState, onWillDestroy } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

// ── Single popup card ────────────────────────────────────────────────────────
class FulfillmentChatPopup extends Component {
    static template = "FulfillmentChatPopup";
    static props = ["popup", "onClose", "onNavigate", "onSend"];

    setup() {
        this.state = useState({ text: "" });
    }

    onNavigate() {
        this.props.onNavigate(this.props.popup.partnerId);
    }

    onClose() {
        this.props.onClose(this.props.popup.id);
    }

    onSend() {
        const text = this.state.text.trim();
        if (text) {
            this.props.onSend(this.props.popup, text);
            this.state.text = "";
        }
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.onSend();
        }
    }

    onInput(ev) {
        this.state.text = ev.target.value;
    }
}

// ── Manager: subscribes to bus, owns the popup stack ────────────────────────
class FulfillmentChatPopupManager extends Component {
    static template = "FulfillmentChatPopupManager";
    static components = { FulfillmentChatPopup };

    setup() {
        this.action = useService("action");
        this.orm    = useService("orm");
        this.state  = useState({ popups: [] });

        // timers for auto-dismiss
        this._timers = [];

        const busService = useService("bus_service");

        // Subscribe — same pattern as notifications.js
        busService.subscribe("fulfillment_new_message", (payload) => {
            this._onMessage(payload);
        });

        onWillDestroy(() => {
            this._timers.forEach(clearTimeout);
        });
    }

    _onMessage(payload) {
        if (!payload || !payload.content) return;

        const id = `fcp_${Date.now()}_${Math.random().toString(36).slice(2)}`;
        this.state.popups.push({
            id,
            partnerName: payload.partner_name || "Partner",
            content:     payload.content,
            partnerId:   payload.partner_id,
            externalId:  payload.external_id,
        });

        // Soft notification sound
        try {
            const ctx  = new (window.AudioContext || window.webkitAudioContext)();
            const osc  = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.type = "sine";
            osc.frequency.setValueAtTime(660, ctx.currentTime);
            osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.15);
            gain.gain.setValueAtTime(0.12, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
            osc.start(ctx.currentTime);
            osc.stop(ctx.currentTime + 0.35);
        } catch (_) {}

        // Auto-dismiss after 15 s
        const t = setTimeout(() => this._closePopup(id), 15000);
        this._timers.push(t);
    }

    _closePopup(id) {
        const idx = this.state.popups.findIndex((p) => p.id === id);
        if (idx >= 0) this.state.popups.splice(idx, 1);
    }

    _navigateToPartner(partnerId) {
        this.action.doAction({
            type:      "ir.actions.act_window",
            res_model: "fulfillment.partners",
            res_id:    partnerId,
            views:     [[false, "form"]],
            target:    "current",
        });
    }

    async _sendReply(popup, text) {
        try {
            await this.orm.call(
                "fulfillment.partners",
                "message_post",
                [[popup.partnerId]],
                {
                    body:          `<p>${text}</p>`,
                    message_type:  "comment",
                    subtype_xmlid: "mail.mt_comment",
                },
            );
            this._closePopup(popup.id);
        } catch (e) {
            console.error("[FulfillmentChatPopup] Reply failed:", e);
        }
    }
}

registry.category("main_components").add("FulfillmentChatPopupManager", {
    Component: FulfillmentChatPopupManager,
    props:     {},
});
