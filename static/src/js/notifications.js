/** @odoo-module **/
import { registry } from "@web/core/registry";

class FulfillmentNotifier {
    constructor( env, { bus_service, notification } ) {
        this.env = env;
        this.bus = bus_service;
        this.notification = notification;
        this._onNotification = this._onNotification.bind( this );
    }
    
    async start () {
        if ( this.bus.isReady ) {
            await this.bus.isReady();
        }
        this.bus.subscribe( "fulfillment_notification", this._onNotification );
    }
    
    _onNotification ( notification ) {
        if ( notification && notification.type === "fulfillment_notification" ) {
            const msg = notification.payload;
            this._showNotification( msg );
        } else {
            console.warn( "Получено уведомление неизвестного типа:", notification?.type );
        }
    }
    
    _showNotification(msg) {
        if (!msg || typeof msg !== 'object') {
            return;
        }
        
        if (!msg.message) {
            let closeNotification;
            closeNotification = this.notification.add("Получено уведомление с неправильным форматом", {
                title: "Ошибка формата",
                type: "danger",
                sticky: true,
                buttons: [
                    {
                        name: "Okay",
                        primary: true,
                        onClick: () => {
                            if (closeNotification) {
                                closeNotification(); 
                            }
                        },
                    },
                ],
            });
            return;
        }
        
        let closeNotification;
        closeNotification = this.notification.add(msg.message, {
            title: msg.title || "Fulfillment",
            type: msg.level || "info",
            sticky: msg.sticky || false,
            buttons: [
                {
                    name: "Okay",
                    primary: true,
                    onClick: () => {
                        if (closeNotification) {
                            closeNotification(); 
                        }
                    },
                },
            ],
        });
    }
}

registry.category( "services" ).add( "fulfillment_notifier", {
    dependencies: ["bus_service", "notification"],
    async start ( env, deps ) {
        const notifier = new FulfillmentNotifier( env, deps );
        await notifier.start();
        return notifier;
    },
} );