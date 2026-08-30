"""Маршрутизация событий: админка или сценарий гостя."""
from __future__ import annotations

import logging

from . import admin, admins, flow, repo
from .channels.base import TG, Btn, Channel, Event, Out
from .channels.telegram import ADMIN_BUTTON, MENU_BUTTON, SUPPORT_BUTTON

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

    if text == MENU_BUTTON:
        # «Главное меню» под полем ввода = /menu, откуда бы её ни нажали
        ev.text = text = "/menu"

    # кнопка «Поддержка» работает в любой момент и не сбивает оформление заказа
    if text == SUPPORT_BUTTON or text == "/support":
        await flow.show_support(ev, ch)
        return

    # служебная команда: узнать ID чата (нужно, чтобы задать рабочий чат заказов)
    if text.startswith("/id"):
        await ch.send(ev.chat_id, Out(
            text=f"ID этого чата: <code>{ev.chat_id}</code>\n"
                 f"Ваш ID: <code>{ev.user_id}</code>"))
        return

    is_group = ch.name == TG and str(ev.chat_id) != str(ev.user_id)

    # привязка рабочего чата прямо из группы — чтобы не искать ID руками
    if text.startswith("/clip"):
        await _clip_chat(ev, ch, is_group)
        return

    # самый первый написавший боту в личку становится владельцем
    if ch.name == TG and not is_group:
        await _claim_owner(ev, ch)

    if ev.kind == "callback" and (ev.payload or "").startswith("a:"):
        # кнопки менеджеров: не админу отвечаем понятно, а не молчим
        if not (ch.name == TG and await admins.is_admin(ev.user_id)):
            await ch.answer_callback(
                ev.callback_id,
                "Эти кнопки доступны только администраторам бота. "
                "Если вы админ — нажмите от своего имени, а не от имени группы.")
            return
        await admin.handle_callback(ev, ch)
        return

    if ch.name == TG and await admins.is_admin(ev.user_id):
        # ввод для админки принимаем только в личном чате с ботом,
        # чтобы обычная переписка в рабочем чате не попала в форму
        if ev.kind == "text" and not is_group and await admin.handle_text(ev, ch):
            return

    if is_group:
        # в рабочем чате бот отвечает только на кнопки заказов
        return

    await flow.handle(ev, ch)


async def _clip_chat(ev: Event, ch: Channel, is_group: bool) -> None:
    """Сделать эту группу рабочим чатом заказов.

    Команду отправляют прямо в группе — так не нужно узнавать ID и вписывать
    его руками, а заодно сразу видно, что бот в этой группе умеет писать.
    """
    if not is_group:
        await ch.send(ev.chat_id, Out(
            text="ℹ️ <b>Команда для группы</b>\n\n"
                 "Добавьте бота в чат менеджеров и отправьте <code>/clip</code> там — "
                 "этот чат станет рабочим, и туда пойдут заказы."))
        return

    if not await admins.is_admin(ev.user_id):
        await ch.send(ev.chat_id, Out(
            text="⛔ Привязать чат может только администратор бота."))
        return

    chat_id = str(ev.chat_id)
    if (await repo.get_setting("orders_chat_id")) == chat_id:
        await ch.send(ev.chat_id, Out(
            text="✅ Этот чат уже рабочий — заказы приходят сюда."))
        return

    await repo.set_setting("orders_chat_id", chat_id)
    await ch.send(ev.chat_id, Out(
        text="✅ <b>Чат привязан</b>\n\n"
             "Сюда будут приходить новые заказы, оплаты и запросы адресов — "
             "с кнопками, чтобы отвечать прямо отсюда.\n\n"
             f"ID чата: <code>{chat_id}</code>"))
    log.info("Рабочий чат заказов привязан: %s (админ %s)", chat_id, ev.user_id)


async def _claim_owner(ev: Event, ch: Channel) -> None:
    """Первый пользователь бота получает права владельца."""
    if not await admins.claim_owner(ev.user_id, ev.username, ev.full_name):
        return
    await ch.show_admin_button(
        str(ev.chat_id),
        "🛠 Внизу закреплены <b>Админ-панель</b> и <b>Поддержка</b> — "
        "они всегда под рукой.",
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
