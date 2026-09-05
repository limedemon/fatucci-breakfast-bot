"""Уведомления: карточки заказов для менеджеров и сообщения гостям."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterable, Optional

from . import admins, repo, statuses
from .channels.base import MAX, TG, Btn, Out, channel_title, get_channel
from .config import cfg
from .utils import esc, fmt_date, fmt_dt, fmt_money, fmt_phone, plural

log = logging.getLogger(__name__)
Row = Any


# ------------------------------------------------------------------ карточки
def _field(row: Row, name: str, default: Any = "") -> Any:
    """Значение колонки, которой может не быть в старой записи."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def schedule_lines(orders: list[Row]) -> str:
    """Разбивка заказа по датам — то, что просила заказчица:

        25 августа — Сет 1 — 2 шт.
        26 августа — Сет 2 — 2 шт.
    """
    active = [o for o in orders if o["status"] != statuses.CANCELLED] or orders
    rows = [
        f"{fmt_date(o['delivery_date'], with_weekday=False)} — "
        f"{esc(o['set_title'] or 'сет дня')} — {o['qty']} шт."
        for o in sorted(active, key=lambda r: r["delivery_date"])
    ]
    total_sets = sum(o["qty"] for o in active)
    total_sum = sum(o["total_kop"] for o in active)
    if len(rows) > 1:
        rows.append("")
        rows.append(f"Итого: {total_sets} "
                    f"{plural(total_sets, 'завтрак', 'завтрака', 'завтраков')} — "
                    f"<b>{fmt_money(total_sum)}</b>")
    return "\n".join(rows)


def group_total(orders: list[Row]) -> int:
    return sum(o["total_kop"] for o in orders if o["status"] != statuses.CANCELLED)


async def group_status_text(orders: list[Row], key: str, **extra: Any) -> str:
    """Текст статуса для заказа целиком — с разбивкой по датам."""
    head = orders[0]
    return await repo.render_text(
        key,
        number=head["group_key"] or head["number"],
        schedule=schedule_lines(orders),
        date_h=fmt_date(head["delivery_date"]),
        set_title=head["set_title"],
        qty=sum(o["qty"] for o in orders),
        total=fmt_money(group_total(orders)),
        price=fmt_money(head["price_kop"]),
        address=head["object_address"] or head["object_title"],
        apartment=head["apartment"],
        object_title=head["object_title"],
        **extra,
    )


async def order_card(order: Row, for_admin: bool = True, group: list[Row] | None = None) -> str:
    """Карточка заказа: сверху главное, ниже детали, в конце — служебное."""
    group = group or [order]
    multi = len(group) > 1
    lines = [
        f"<b>Заказ №{esc(order['group_key'] or order['number'])}</b>",
        statuses.label(order["status"]),
        "",
    ]
    if multi:
        lines.append(schedule_lines(group))
    else:
        lines.append(f"📅 {fmt_date(order['delivery_date'])}")
        lines.append(f"🥐 {esc(order['set_title'])} × {order['qty']}")
    if order["object_address"]:
        lines.append(f"📍 {esc(order['object_address'])}")
    else:
        lines.append(f"📍 {esc(order['object_title'])}")
    lines.append(f"🚪 Апартаменты {esc(order['apartment'])}")
    if for_admin:
        contact = esc(fmt_phone(order["phone"]))
        name = _field(order, "customer_name")
        lines.append(f"📞 {contact}" + (f", {esc(name)}" if name else ""))
    allergies = _field(order, "allergies")
    if allergies:
        lines.append(f"⚠️ <b>Аллергии: {esc(allergies)}</b>")
    if order["comment"]:
        lines.append(f"💬 {esc(order['comment'])}")
    if for_admin and not int(_field(order, "address_ok", 1) or 0):
        lines.append("❗️ <b>Адрес введён гостем вручную и не найден среди объектов</b>")
    lines.append("")
    discount = int(_field(order, "discount_pct") or 0)
    if discount:
        base = int(_field(order, "base_price_kop") or order["price_kop"])
        lines.append(f"💰 {fmt_money(base)} × {order['qty']} = {fmt_money(base * order['qty'])}")
        lines.append(f"🎁 Скидка −{discount}% → <b>{fmt_money(order['total_kop'])}</b>")
    elif not multi:
        lines.append(
            f"💰 {fmt_money(order['price_kop'])} × {order['qty']} = "
            f"<b>{fmt_money(order['total_kop'])}</b>"
        )
    if multi:
        lines.append(f"💰 К оплате за весь заказ: <b>{fmt_money(group_total(group))}</b>")
    if for_admin:
        lines += ["", f"🏢 {esc(order['object_title'])}",
                  f"🕗 {fmt_dt(order['created_at'])} · {channel_title(order['channel'])}"]
        if order["source_code"]:
            lines.append(f"🔗 <code>{esc(order['source_code'])}</code>")
        if order["paid_at"]:
            lines.append(f"✅ Оплачен {fmt_dt(order['paid_at'])}")
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
    return [str(admin_id) for admin_id in sorted(await admins.ids())]


async def send_to_admins(text: str, kb: Optional[list[list[Btn]]] = None,
                         photo: str = "") -> list[dict[str, Any]]:
    channel = get_channel(TG)
    if channel is None:
        log.warning("Telegram-канал не запущен — уведомление админам пропущено")
        return []
    sent: list[dict[str, Any]] = []
    targets = await admin_targets()
    for chat_id in targets:
        message_id = await channel.send(chat_id, Out(text=text, kb=kb, photo=photo))
        if message_id:
            sent.append({"chat_id": chat_id, "message_id": message_id})

    if not sent:
        # рабочий чат недоступен (бот не добавлен, чат удалён) — не теряем заказ
        personal = [str(a) for a in sorted(await admins.ids()) if str(a) not in targets]
        if personal:
            log.warning("Рабочий чат %s недоступен — шлю заказ лично админам", targets)
        for chat_id in personal:
            message_id = await channel.send(chat_id, Out(text=text, kb=kb, photo=photo))
            if message_id:
                sent.append({"chat_id": chat_id, "message_id": message_id})
    return sent


async def send_to_admin_dms(text: str, kb: Optional[list[list[Btn]]] = None) -> int:
    """Личное сообщение каждому администратору.

    Отдельно от send_to_admins: то шлёт в рабочий чат, а есть вещи, которые
    должны дойти лично — например, просьба гостя подключить новый адрес.
    """
    channel = get_channel(TG)
    if channel is None:
        log.warning("Telegram-канал не запущен — личное уведомление пропущено")
        return 0
    delivered = 0
    for admin_id in sorted(await admins.ids()):
        if await channel.send(str(admin_id), Out(text=text, kb=kb)):
            delivered += 1
    if not delivered:
        log.warning("Никому из админов не удалось доставить личное уведомление")
    return delivered


async def new_object_request(channel_name: str, user_id: str, username: str,
                             full_name: str, address: str) -> int:
    """Гость просит подключить свой дом — зовём администраторов лично."""
    who = esc(full_name.strip() or "гость")
    link = guest_link(channel_name, username)
    lines = [
        "🏠 <b>Запрос новых апартаментов</b>",
        "",
        f"📍 Адрес: <b>{esc(address)}</b>",
        f"👤 Гость: {who}" + (f" (@{esc(username)})" if username else ""),
        f"🆔 <code>{esc(str(user_id))}</code> · {channel_title(channel_name)}",
        "",
        "Если возим по этому адресу — заведите объект и напишите гостю.",
    ]
    kb = [[Btn(text="🏢 Добавить объект", data="a:b:n")]]
    if link:
        kb.append([Btn(text="✉️ Написать гостю", url=link)])
    return await send_to_admin_dms("\n".join(lines), kb)


def _general_price(general: Optional[Row]) -> int:
    """Цена объекта «общий QR» — по ней считаются адреса вне списка."""
    return int(general["price_kop"] or 0) if general is not None else 0


async def send_review(review: Row, user: Row) -> bool:
    """Отзыв — одним сообщением в чат отзывов (или менеджерам, если он не задан)."""
    channel = get_channel(TG)
    if channel is None:
        return False

    stars = max(0, min(5, int(review["stars"] or 0)))
    lines = ["⭐️ <b>Новый отзыв</b>", "", "★" * stars + "☆" * (5 - stars) + f"  {stars} из 5"]
    if review["comment"]:
        lines += ["", f"«{esc(review['comment'])}»"]
    who = esc((user["full_name"] if user else "") or "гость")
    if user and user["username"]:
        who += f" (@{esc(user['username'])})"
    tail = f"Гость: {who}"
    if review["order_no"]:
        tail += f" · заказ №{esc(review['order_no'])}"
    lines += ["", tail]

    target = (await repo.get_setting("reviews_chat_id")).strip()
    out = Out(text="\n".join(lines), photo=review["photo_key"] or "")
    if target:
        return bool(await channel.send(target, out))
    # чат отзывов не привязан — не теряем отзыв, шлём менеджерам
    return bool(await send_to_admins(out.text, photo=out.photo))


async def new_address(user: Row, address: str) -> None:
    """Гость заказывает по адресу вне списка — сообщаем в рабочий чат.

    Заказывать он может уже сейчас: менеджеры решают спокойно, а не держат
    гостя на паузе. Решение прилетает кнопками из этого же сообщения.
    """
    link = guest_link(user["channel"], user["username"])
    general = await repo.general_object()
    lines = [
        "📍 <b>Новый адрес вне списка</b>",
        "",
        f"Адрес: <b>{esc(address)}</b>",
        f"Гость: {esc(user['full_name'] or 'без имени')}"
        + (f" (@{esc(user['username'])})" if user["username"] else ""),
        f"Цена по такому адресу: <b>{fmt_money(_general_price(general))}</b>",
        "",
        "Гость уже может оформить заказ — он придёт сюда как обычный.",
        "Если по этому адресу не возим, нажмите «Отклонить»: гостю сообщим "
        "и попросим другой адрес.",
    ]
    kb = [[Btn(text="✅ Принять", data=f"a:addr:ok:{user['id']}", intent="positive"),
           Btn(text="⛔ Отклонить", data=f"a:addr:no:{user['id']}", intent="negative")]]
    if link:
        kb.append([Btn(text="✉️ Написать гостю", url=link)])
    await send_to_admins("\n".join(lines), kb)


async def notify_new_order(orders: list[Row] | Row) -> None:
    """Новый заказ -> одна карточка в рабочий чат, даже если дней несколько."""
    group = orders if isinstance(orders, list) else [orders]
    head = group[0]
    text = "🆕 <b>НОВЫЙ ЗАКАЗ</b>\n\n" + await order_card(head, group=group)
    kb = order_admin_kb(head, await guest_username(head))
    sent = await send_to_admins(text, kb)
    await repo.update_order(head["id"], admin_msgs=json.dumps(sent, ensure_ascii=False))


async def refresh_order_cards(order: Row) -> None:
    """Обновить карточку заказа у менеджеров после смены статуса."""
    channel = get_channel(TG)
    if channel is None:
        return
    group = await repo.group_of(order)
    head = group[0]
    targets = repo.json_loads(head["admin_msgs"], [])
    if not targets:                 # карточку отправляли по конкретной строке заказа
        targets = repo.json_loads(order["admin_msgs"], [])
        head, group = order, group
    text = await order_card(head, group=group)
    kb = order_admin_kb(head, await guest_username(head))
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
