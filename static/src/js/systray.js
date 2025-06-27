/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState } from "@odoo/owl";

class SystrayIcon extends Component {
    setup() {
        this.notification = useService("notification");
        this.state = useState({
            shake: false,
        });
    }

    showNotification() {
        this.notification.add("Hello! This is a notification", {
            title: "Systray Notification",
            type: "info",
            sticky: false,
        });
        this.state.shake = true;

        // Авто-выключаем тряску через 2 секунды
        setTimeout(() => {
            this.state.shake = false;
        }, 12000);
    }
}

SystrayIcon.template = "systray_icon";

export const systrayItem = {
    Component: SystrayIcon,
};

registry.category("systray").add("SystrayIcon", systrayItem, { sequence: 5 });
