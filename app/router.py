"""Маршрутизация событий: админка или сценарий гостя."""
from __future__ import annotations

import logging

from . import admin, flow
from .channels.base import TG, Channel, Event, Out

log = logging.getLogger(__name__)


async def route(ev: Event, ch: Channel) -> None:
    try:
        await _route(ev, ch)
    except Exception:  # noqa: BLE001
        log.exception("Необработанная ошибка события %s/%s", ev.channel, ev.kind)


async def _route(ev: Event, ch: Channel) -> None:
    text = (ev.text or "").strip()

    # служебная команда: узнать ID чата (нужно, чтобы задать рабочий чат заказов)
    if text.startswith("/id"):
        await ch.send(ev.chat_id, Out(
            text=f"ID этого чата: <code>{ev.chat_id}</code>\n"
                 f"Ваш ID: <code>{ev.user_id}</code>"))
        return

    is_group = ch.name == TG and str(ev.chat_id) != str(ev.user_id)

    if ch.name == TG and admin.is_admin(ev.user_id):
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
