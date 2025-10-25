/** @odoo-module **/

import { registry } from "@web/core/registry";

const myService = {
    dependencies: ["notification"],
    start(env, { notification }) {
        let counter = 1;
        setInterval(() => {
            const close = notification.add(`Счётчик: ${counter++}`, {
                title: "Тестовое уведомление",
                type: "info", // варианты: success | info | warning | danger
                sticky: false,
                autocloseDelay: 4000, // 4 секунды
                buttons: [
                    {
                        name: "ОК", // текст кнопки
                        primary: false, // выделенная кнопка
                        onClick: () => {
                            console.log("Кнопка нажата ✅");
                            close(); // закрывает уведомление вручную
                        },
                    },
                    {
                        name: "Отмена",
                        primary: false,
                        onClick: () => {
                            console.log("Отменено ❌");
                            close();
                        },
                    },
                ],
                onClose: () => {
                    console.log("Уведомление закрыто 📴");
                },
            });
        }, 5000);
    },
};

registry.category("services").add("myService", myService);
