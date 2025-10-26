/** @odoo-module **/
import { registry } from "@web/core/registry";

class FulfillmentNotifier {
    constructor(env, { bus_service, notification }) {
        this.bus = bus_service;
        this.notification = notification;
    }

    async start() {
        console.log("🟢 FulfillmentNotifier запущен...");

        if (typeof this.bus.isReady === "function") {
            await this.bus.isReady();
        }

        // добавляем канал до запуска
        this.bus.addChannel("fulfillment_notification");
        console.log("📡 Канал 'fulfillment_notification' добавлен");

        // подписка на уведомления
        this.bus.addEventListener("notification", (ev) => {
            const notifications = ev.detail || [];
            for (const notif of notifications) {
                const msg = notif.payload;
                if (msg?.type === "fulfillment_notification") {
                    console.log("📩 Получено уведомление:", msg);
                    this.notification.add(msg.message, {
                        title: msg.title || "Fulfillment API",
                        type: msg.level || "info",
                        sticky: msg.sticky || false,
                        autocloseDelay: 4000,
                    });
                }
            }
        });

        if (typeof this.bus.start === "function") this.bus.start();
    }
}

registry.category("services").add("fulfillment_notifier", {
    dependencies: ["bus_service", "notification"],
    async start(env, deps) {
        const notifier = new FulfillmentNotifier(env, deps);
        await notifier.start();
        return notifier;
    },
});
