/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";

class SystrayIcon extends Component {
    setup() {
        this.action = useService("action");
        this.state = useState({});
    }

    openProfiles() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "fulfillment.profile",
            views: [[false, "list"], [false, "form"]],
            target: "new", // <<< откроет как диалог (popup)
            name: "Profiles Fulfillment",
        });
    }

    openPartners() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "fulfillment.partners",
            views: [[false, "list"], [false, "form"]],
            target: "new",
            name: "Partners Fulfillment",
        });
    }
    
    runImportAll() {
        this.action.doAction("fulfillment_software.action_run_import_all");
    }
}

SystrayIcon.components = { Dropdown, DropdownItem };
SystrayIcon.template = "systray_icon";

export const systrayItem = {
    Component: SystrayIcon,
};

registry.category("systray").add("SystrayIcon", systrayItem, { sequence: 5 });
