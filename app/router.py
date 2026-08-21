"""Маршрутизация событий: админка или сценарий гостя."""
from __future__ import annotations

import logging

from . import admin, admins, flow
from .channels.base import TG, Btn, Channel, Event, Out
from .channels.telegram import ADMIN_BUTTON

log = logging.getLogger(__name__)


async def route(ev: Event, ch: Channel) -> None:
    try:
        await _route(ev, ch)
    except Exception:  # noqa: BLE001
        log.exception("Необработанная ошибка события %s/%s", ev.channel, ev.kind)


async def _route(ev: Event, ch: Channel) -> None:
    text = (ev.text or "").strip()
    if text == ADMIN_BUTTON:
        # нажали постоянную кнопку под полем ввода — это то же самое, что /admin
        ev.text = text = "/admin"

    # служебная команда: узнать ID чата (нужно, чтобы задать рабочий чат заказов)
    if text.startswith("/id"):
        await ch.send(ev.chat_id, Out(
            text=f"ID этого чата: <code>{ev.chat_id}</code>\n"
                 f"Ваш ID: <code>{ev.user_id}</code>"))
        return

    is_group = ch.name == TG and str(ev.chat_id) != str(ev.user_id)

    # самый первый написавший боту в личку становится владельцем
    if ch.name == TG and not is_group:
        await _claim_owner(ev, ch)

    if ch.name == TG and await admins.is_admin(ev.user_id):
        if ev.kind == "callback" and (ev.payload or "").startswith("a:"):
            await admin.handle_callback(ev, ch)
            return
        # ввод для админки принимаем только в личном чате с ботом,
        # чтобы обычная переписка в рабочем чате не попала в форму
        if ev.kind == "text" and not is_group and await admin.handle_text(ev, ch):
            return

    if is_group:
        # в рабочем чате бот отвечает только на кнопки заказов
        return

    await flow.handle(ev, ch)


async def _claim_owner(ev: Event, ch: Channel) -> None:
    """Первый пользователь бота получает права владельца."""
    if not await admins.claim_owner(ev.user_id, ev.username, ev.full_name):
        return
    await ch.show_admin_button(
        str(ev.chat_id),
        "🛠 Кнопка админ-панели закреплена внизу — она всегда под рукой.",
    )
    await ch.send(ev.chat_id, Out(
        text=(
            "👑 <b>Вы владелец этого бота</b>\n\n"
            "Вы первый, кто написал боту, поэтому доступ к управлению выдан вам.\n\n"
            "Откройте админ-панель командой /admin — там настраиваются объекты, "
            "меню, цены, оплата и всё остальное.\n\n"
            "Остальных менеджеров добавьте в разделе <b>👑 Доступ</b>: "
            "права владельца остаются только у вас."
        ),
        kb=[[Btn(text="🛠 Открыть админ-панель", data="a:h", intent="positive")]],
    ))
