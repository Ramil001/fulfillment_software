/** @odoo-module **/

import { registry } from "@web/core/registry";

class FulfillmentNotifier {
    constructor(env, { bus_service, notification }) {
        this.env = env;
        this.bus = bus_service;
        this.notification = notification;
        this._onNotification = this._onNotification.bind(this);
        console.log("✅ Fulfillment Notifier JS загружен");
    }

    async start() {
        console.log("🟢 FulfillmentNotifier запущен...");

        // Ждем готовности bus service
        if (this.bus.isReady) {
            await this.bus.isReady();
        }

        // Подписка на канал fulfillment_notification
        this.bus.subscribe("fulfillment_notification", this._onNotification);
        console.log("📡 Подписан на канал: fulfillment_notification");

      
    }

    _onNotification(notification) {
        console.log("📩 Получено уведомление через bus:", notification);
        
        if (notification && notification.type === "fulfillment_notification") {
            const msg = notification.payload;
            this._showNotification(msg);
        }
    }

    _showNotification(msg) {
        console.log("🎯 Показываем уведомление:", msg);
        
        if (!msg.message) {
            console.error("❌ Нет сообщения для показа:", msg);
            return;
        }

        this.notification.add(msg.message, {
            title: msg.title || "Fulfillment",
            type: msg.level || "info",
            sticky: msg.sticky || false,
        });
    }

    // Метод для ручной отправки уведомления (для тестирования)
    sendTestNotification() {
        this.notification.add("Тестовое уведомление из Fulfillment Notifier", {
            title: "Тест",
            type: "success",
            sticky: true,
        });
    }

    // Метод для очистки подписки
    destroy() {
        if (this.bus) {
            this.bus.unsubscribe("fulfillment_notification", this._onNotification);
        }
    }
}

// Регистрация сервиса
registry.category("services").add("fulfillment_notifier", {
    dependencies: ["bus_service", "notification"],
    async start(env, deps) {
        const notifier = new FulfillmentNotifier(env, deps);
        await notifier.start();
        return notifier;
    },
});

export default FulfillmentNotifier;