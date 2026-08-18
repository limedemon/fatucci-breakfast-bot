"""Уведомления: карточки заказов для менеджеров и сообщения гостям."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterable, Optional

import aiosqlite

from . import repo, statuses
from .channels.base import MAX, TG, Btn, Out, channel_title, get_channel
from .config import cfg
from .utils import esc, fmt_date, fmt_dt, fmt_money, fmt_phone

log = logging.getLogger(__name__)
Row = aiosqlite.Row


# ------------------------------------------------------------------ карточки
async def order_card(order: Row, for_admin: bool = True) -> str:
    """Текст карточки заказа."""
    lines = [
        f"<b>Заказ №{esc(order['number'])}</b>",
        f"Статус: <b>{statuses.label(order['status'])}</b>",
        "",
        f"📅 Доставка: <b>{fmt_date(order['delivery_date'])}</b>",
        f"🏢 Объект: {esc(order['object_title'])}",
    ]
    if order["object_address"]:
        lines.append(f"📍 Адрес: {esc(order['object_address'])}")
    lines += [
        f"🚪 Апартаменты: <b>{esc(order['apartment'])}</b>",
        f"🥐 Сет: {esc(order['set_title'])}",
        f"🔢 Количество: {order['qty']}",
    ]
    if for_admin:
        lines.append(f"📞 Телефон: {esc(fmt_phone(order['phone']))}")
    if order["comment"]:
        lines.append(f"💬 Комментарий: {esc(order['comment'])}")
    lines.append(
        f"💰 {fmt_money(order['price_kop'])} × {order['qty']} = <b>{fmt_money(order['total_kop'])}</b>"
    )
    if for_admin:
        lines += [
            "",
            f"🕗 Оформлен: {fmt_dt(order['created_at'])} · {channel_title(order['channel'])}",
        ]
        if order["source_code"]:
            lines.append(f"🔗 QR: <code>{esc(order['source_code'])}</code>")
        if order["paid_at"]:
            lines.append(f"✅ Оплачен: {fmt_dt(order['paid_at'])}")
    return "\n".join(lines)


def order_admin_kb(order: Row, username: str = "") -> list[list[Btn]]:
    """Кнопки менеджера под карточкой заказа."""
    rows: list[list[Btn]] = []
    actions = statuses.next_actions(order["status"])
    positive = (statuses.ACCEPTED, statuses.PAID, statuses.DELIVERED, statuses.RECEIVED)
    if actions:
        rows.append([
            Btn(text=title, data=f"a:ord:{order['id']}:{code}",
                intent="positive" if code in positive else "negative")
            for code, title in actions
        ])
    tail = [Btn(text="🔄 Обновить", data=f"a:ord:{order['id']}:refresh")]
    link = guest_link(order["channel"], username)
    if link:
        tail.insert(0, Btn(text="✉️ Написать гостю", url=link))
    rows.append(tail)
    return rows


def guest_link(channel: str, username: str) -> str:
    if not username:
        return ""
    if channel == TG:
        return f"https://t.me/{username}"
    if channel == MAX:
        return f"https://max.ru/{username}"
    return ""


async def guest_username(order: Row) -> str:
    user = await repo.get_user(order["channel"], order["ext_id"])
    return user["username"] if user else ""


# ------------------------------------------------- отправка менеджерам/админам
async def admin_targets() -> list[str]:
    """Куда слать заказы: рабочий чат, а если он не задан — личные чаты админов."""
    chat_id = (await repo.get_setting("orders_chat_id")) or cfg.orders_chat_id
    if chat_id.strip():
        return [chat_id.strip()]
    return [str(admin_id) for admin_id in cfg.admin_ids]


async def send_to_admins(text: str, kb: Optional[list[list[Btn]]] = None) -> list[dict[str, Any]]:
    channel = get_channel(TG)
    if channel is None:
        log.warning("Telegram-канал не запущен — уведомление админам пропущено")
        return []
    sent: list[dict[str, Any]] = []
    targets = await admin_targets()
    for chat_id in targets:
        message_id = await channel.send(chat_id, Out(text=text, kb=kb))
        if message_id:
            sent.append({"chat_id": chat_id, "message_id": message_id})

    if not sent:
        # рабочий чат недоступен (бот не добавлен, чат удалён) — не теряем заказ
        personal = [str(admin_id) for admin_id in cfg.admin_ids if str(admin_id) not in targets]
        if personal:
            log.warning("Рабочий чат %s недоступен — шлю заказ лично админам", targets)
        for chat_id in personal:
            message_id = await channel.send(chat_id, Out(text=text, kb=kb))
            if message_id:
                sent.append({"chat_id": chat_id, "message_id": message_id})
    return sent


async def notify_new_order(order: Row) -> None:
    """Новый заказ -> в рабочий чат с кнопками действий."""
    text = "🆕 <b>НОВЫЙ ЗАКАЗ</b>\n\n" + await order_card(order)
    kb = order_admin_kb(order, await guest_username(order))
    sent = await send_to_admins(text, kb)
    await repo.update_order(order["id"], admin_msgs=json.dumps(sent, ensure_ascii=False))


async def refresh_order_cards(order: Row) -> None:
    """Обновить все карточки заказа у менеджеров после смены статуса."""
    channel = get_channel(TG)
    if channel is None:
        return
    targets = repo.json_loads(order["admin_msgs"], [])
    text = await order_card(order)
    kb = order_admin_kb(order, await guest_username(order))
    for target in targets:
        try:
            await channel.edit(str(target["chat_id"]), str(target["message_id"]),
                               Out(text=text, kb=kb))
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось обновить карточку заказа: %s", exc)


# ------------------------------------------------------------------- гостю
async def notify_guest(order: Row, text: str, kb: Optional[list[list[Btn]]] = None) -> None:
    channel = get_channel(order["channel"])
    if channel is None:
        log.info("Канал %s недоступен — гость не уведомлён по заказу %s",
                 order["channel"], order["number"])
        return
    await channel.send(order["chat_id"] or order["ext_id"], Out(text=text, kb=kb))


async def guest_status_text(order: Row, key: str, **extra: Any) -> str:
    return await repo.render_text(
        key,
        number=order["number"],
        date_h=fmt_date(order["delivery_date"]),
        set_title=order["set_title"],
        qty=order["qty"],
        total=fmt_money(order["total_kop"]),
        price=fmt_money(order["price_kop"]),
        address=order["object_address"] or order["object_title"],
        apartment=order["apartment"],
        object_title=order["object_title"],
        **extra,
    )


# --------------------------------------------------------------- рассылки
async def broadcast(text: str, channels: Iterable[str], photo: str = "") -> tuple[int, int]:
    """Массовая рассылка. Возвращает (доставлено, ошибок)."""
    ok = failed = 0
    for name in channels:
        channel = get_channel(name)
        if channel is None:
            continue
        for row in await repo.broadcast_targets(name):
            target = row["chat_id"] or row["ext_id"]
            try:
                message_id = await channel.send(target, Out(text=text, photo=photo))
                if message_id:
                    ok += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001
                failed += 1
            await asyncio.sleep(0.05 if name == TG else 0.4)
    return ok, failed
