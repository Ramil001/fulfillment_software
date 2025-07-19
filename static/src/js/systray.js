/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState } from "@odoo/owl";

class SystrayIcon extends Component {
    setup() {
        this.action = useService("action");
        this.state = useState({
            menuOpen: false,
        });
    }

    toggleMenu() {
        this.state.menuOpen = !this.state.menuOpen;
    }

    openAction(action) {
        this.action.doAction(action);
        this.state.menuOpen = false;
    }

    openProfiles() {
        this.openAction({
            type: 'ir.actions.act_window',
            res_model: 'fulfillment.profile',
            views: [[false, 'list'], [false, 'form']],
            target: 'current',
            name: 'Profiles Fulfillment',
        });
    }

    openPartners() {
        this.openAction({
            type: 'ir.actions.act_window',
            res_model: 'fulfillment.partners',
            views: [[false, 'list'], [false, 'form']],
            target: 'current',
            name: 'Partners Fulfillment',
        });
    }
    
    async syncFulfillment() {
        try {
            await rpc.query({
                model: 'stock.picking',
                method: 'create_fulfillment_receipt',
                args: [],
            });
            this.notification.add({
                title: "Fulfillment",
                message: "Синхронизация выполнена успешно",
                type: "success",
            });
        } catch (e) {
            this.notification.add({
                title: "Fulfillment",
                message: `Ошибка при синхронизации: ${e.message || e}`,
                type: "danger",
            });
        }
        this.state.menuOpen = false;
    }

}




SystrayIcon.template = "systray_icon";

export const systrayItem = {
    Component: SystrayIcon,
};

registry.category("systray").add("SystrayIcon", systrayItem, { sequence: 5 });
