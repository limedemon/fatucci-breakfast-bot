"""Сценарий гостя. Один код — оба мессенджера (Telegram и MAX).

Состояния хранятся в БД (таблица sessions), поэтому:
  • перезапуск бота не роняет незавершённые заказы;
  • работает напоминание о брошенной корзине;
  • логика в Telegram и MAX идентична.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import aiosqlite

from . import availability, notify, orders_service, payments, pricing, repo, statuses
from .channels.base import Btn, Channel, Event, Out
from .utils import (
    chunk,
    esc,
    fmt_date,
    fmt_date_btn,
    fmt_date_iso,
    fmt_money,
    fmt_phone,
    norm_phone,
    parse_date,
    plural,
)

log = logging.getLogger(__name__)
Row = aiosqlite.Row

# состояния
S_NONE = ""
S_OBJECT = "object"
S_DATE = "date"
S_QTY = "qty"
S_APARTMENT = "apartment"
S_PHONE = "phone"
S_COMMENT = "comment"
S_CONFIRM = "confirm"

BACK_LABEL = "⬅️ Назад"
SKIP_LABEL = "⏭ Пропустить"
CONTACT_LABEL = "📞 Поделиться контактом"


# ======================================================================= вход
async def handle(ev: Event, ch: Channel) -> None:
    """Единая точка входа для событий гостя."""
    user = await repo.upsert_user(ev.channel, ev.user_id, ev.chat_id, ev.username, ev.full_name)

    if user["is_blocked"]:
        await _answer(ev, ch)
        await ch.send(ev.chat_id, Out(text=await repo.render_text("blocked")))
        return

    if ev.kind == "payment":
        await _on_payment(ev, ch)
        return

    if ev.kind == "start":
        await _cmd_start(ev, ch)
        return

    if ev.kind == "callback":
        await _on_callback(ev, ch, user)
        await _answer(ev, ch)          # страховка: снять «часики» на кнопке
        return

    if ev.kind in ("text", "contact"):
        await _on_text(ev, ch, user)


# =================================================================== /start
async def _cmd_start(ev: Event, ch: Channel) -> None:
    code = _clean_code(ev.payload)
    obj = await repo.get_object_by_code(code) if code else None

    if code and obj is None:
        await ch.send(ev.chat_id, Out(
            text="😕 Такой QR-код не найден или больше не действует.\n"
                 "Выберите объект вручную или обратитесь к менеджеру."))
    elif obj is not None and not obj["is_active"]:
        await ch.send(ev.chat_id, Out(
            text="⏸ Приём заказов по этому объекту временно приостановлен."))
        obj = None

    session_object: Optional[int] = None
    if obj is not None:
        await repo.update_user(ev.channel, ev.user_id, source_code=obj["code"])
        if obj["is_general"]:
            session_object = None
        else:
            session_object = obj["id"]
            await repo.update_user(ev.channel, ev.user_id, object_id=obj["id"])
    else:
        user = await repo.get_user(ev.channel, ev.user_id)
        session_object = user["object_id"] if user else None

    await repo.save_session(ev.channel, ev.user_id, S_NONE, {}, chat_id=ev.chat_id,
                            object_id=session_object)
    await _show_main_menu(ev, ch, new_message=True)


def _clean_code(payload: str) -> str:
    raw = (payload or "").strip()
    for prefix in ("obj_", "obj-", "o_", "start="):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
    return raw.strip()


# ================================================================ главное меню
async def _show_main_menu(ev: Event, ch: Channel, new_message: bool = False) -> None:
    session = await repo.get_session(ev.channel, ev.user_id)
    obj = await repo.get_object(session["object_id"] if session else None)

    if obj is not None:
        text = await repo.render_text(
            "welcome_object",
            object_title=obj["title"],
            address=obj["address"] or obj["title"],
            price=fmt_money(obj["price_kop"]),
            cutoff=obj["cutoff_time"],
        )
    else:
        text = await repo.render_text("welcome")

    kb = [
        [Btn(text="🥐 Заказать завтрак", data="g:order", intent="positive")],
        [Btn(text="📋 Меню и цены", data="g:sets"), Btn(text="🚚 Доставка", data="g:delivery")],
        [Btn(text="❓ Как заказать", data="g:how"), Btn(text="💬 Вопросы", data="g:faq")],
    ]
    if await repo.count_orders(user_key=(ev.channel, ev.user_id)):
        kb.append([Btn(text="📦 Мои заказы", data="g:my")])
    if await repo.list_offers(active_only=True):
        kb.append([Btn(text="🍽 Ещё от Fatucci", data="g:offers")])

    out = Out(text=text, kb=kb, remove_reply_kb=True)
    await _respond(ev, ch, out, new_message=new_message)


# ==================================================================== callback
async def _on_callback(ev: Event, ch: Channel, user: Row) -> None:
    data = ev.payload or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    handlers = {
        "menu": lambda: _to_menu(ev, ch),
        "order": lambda: _start_order(ev, ch),
        "sets": lambda: _show_sets(ev, ch),
        "set": lambda: _show_set(ev, ch, int(arg or 0)),
        "delivery": lambda: _show_info(ev, ch, "delivery_info"),
        "how": lambda: _show_info(ev, ch, "how_to_order"),
        "faq": lambda: _show_info(ev, ch, "faq"),
        "offers": lambda: _show_offers(ev, ch),
        "offer": lambda: _show_offer(ev, ch, int(arg or 0)),
        "my": lambda: _show_my_orders(ev, ch),
        "ord": lambda: _show_my_order(ev, ch, int(arg or 0)),
        "cancel": lambda: _cancel_order(ev, ch, int(arg or 0)),
        "got": lambda: _confirm_received(ev, ch, int(arg or 0)),
        "pay": lambda: _pay_again(ev, ch, int(arg or 0)),
        "obj": lambda: _pick_object(ev, ch, int(arg or 0)),
        "date": lambda: _pick_date(ev, ch, arg),
        "qty": lambda: (_pick_qty(ev, ch, int(arg)) if arg else _ask_qty(ev, ch)),
        "apt": lambda: _ask_apartment(ev, ch),
        "reapt": lambda: _reuse_apartment(ev, ch),
        "phone": lambda: _ask_phone(ev, ch),
        "rephone": lambda: _reuse_phone(ev, ch),
        "cmt": lambda: _ask_comment(ev, ch),
        "skip": lambda: _skip_comment(ev, ch),
        "edit": lambda: _show_edit(ev, ch),
        "back": lambda: _show_confirm(ev, ch),
        "confirm": lambda: _confirm_order(ev, ch),
        "resume": lambda: _resume_draft(ev, ch),
        "drop": lambda: _drop_draft(ev, ch),
        "noop": lambda: _answer(ev, ch),
    }
    handler = handlers.get(action)
    if handler is None:
        await _to_menu(ev, ch)
        return
    try:
        await handler()
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки кнопки %s", data)
        await ch.send(ev.chat_id, Out(text="⚠️ Что-то пошло не так. Попробуйте ещё раз: /start"))


async def _answer(ev: Event, ch: Channel, text: str = "") -> None:
    """Ответить на нажатие кнопки — ровно один раз за событие."""
    if ev.kind != "callback" or not ev.callback_id or ev.raw.get("_answered"):
        return
    ev.raw["_answered"] = True
    await ch.answer_callback(ev.callback_id, text)


async def _respond(ev: Event, ch: Channel, out: Out, new_message: bool = False) -> str:
    """Обновить сообщение с кнопкой или отправить новое."""
    if (
        not new_message
        and ev.kind == "callback"
        and not ev.raw.get("_answered")
        and not out.photo
        and not out.reply_contact
    ):
        return await ch.reply_to_callback(ev, out)
    await _answer(ev, ch)
    return await ch.send(ev.chat_id, out)


async def _to_menu(ev: Event, ch: Channel) -> None:
    await repo.clear_session(ev.channel, ev.user_id)
    await _show_main_menu(ev, ch)


# ================================================================ информация
async def _show_info(ev: Event, ch: Channel, key: str) -> None:
    session = await repo.get_session(ev.channel, ev.user_id)
    obj = await repo.get_object(session["object_id"] if session else None)
    cutoff = obj["cutoff_time"] if obj is not None else await repo.default_cutoff()
    text = await repo.render_text(key, cutoff=cutoff)
    await _respond(ev, ch, Out(text=text, kb=[[Btn(text="🥐 Заказать", data="g:order"),
                                               Btn(text="⬅️ В меню", data="g:menu")]]))


async def _show_sets(ev: Event, ch: Channel) -> None:
    items = await repo.list_sets(active_only=True)
    if not items:
        await _respond(ev, ch, Out(text="Меню пока пустое. Загляните чуть позже 🙏",
                                   kb=[[Btn(text="⬅️ В меню", data="g:menu")]]))
        return
    text = await repo.render_text("menu_intro")
    rotation = await _rotation_hint(items)
    if rotation:
        text += "\n\n" + rotation
    kb = [[Btn(text=item["title"], data=f"g:set:{item['id']}")] for item in items]
    kb.append([Btn(text="🥐 Заказать", data="g:order"), Btn(text="⬅️ В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb))


async def _rotation_hint(items: list[Row]) -> str:
    from .utils import WEEKDAYS_SHORT

    week = await repo.rotation_week()
    titles = {item["id"]: item["title"] for item in items}
    lines = []
    for weekday in range(1, 8):
        set_id = week.get(weekday)
        if set_id and set_id in titles:
            lines.append(f"{WEEKDAYS_SHORT[weekday - 1]} — {esc(titles[set_id])}")
    if not lines:
        return ""
    return "🗓 <b>Расписание недели</b>\n" + "\n".join(lines)


async def _show_set(ev: Event, ch: Channel, set_id: int) -> None:
    item = await repo.get_set(set_id)
    if item is None:
        await _show_sets(ev, ch)
        return
    session = await repo.get_session(ev.channel, ev.user_id)
    obj = await repo.get_object(session["object_id"] if session else None)
    price = availability.price_for(obj, item) if obj is not None else (item["price_kop"] or 0)
    text = f"🥐 <b>{esc(item['title'])}</b>"
    if item["description"]:
        text += "\n\n" + esc(item["description"])
    if price:
        text += f"\n\n💰 <b>{fmt_money(price)}</b> за сет"
    kb = [[Btn(text="🥐 Заказать", data="g:order")],
          [Btn(text="⬅️ К сетам", data="g:sets"), Btn(text="🏠 В меню", data="g:menu")]]
    await _respond(ev, ch, Out(text=text, kb=kb, photo=item["photo_path"]),
                   new_message=bool(item["photo_path"]))


async def _show_offers(ev: Event, ch: Channel) -> None:
    offers = await repo.list_offers(active_only=True)
    if not offers:
        await _to_menu(ev, ch)
        return
    text = await repo.render_text("upsell_intro")
    kb = [[Btn(text=offer["title"], data=f"g:offer:{offer['id']}")] for offer in offers]
    kb.append([Btn(text="⬅️ В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb))


async def _show_offer(ev: Event, ch: Channel, offer_id: int) -> None:
    offer = await repo.get_offer(offer_id)
    if offer is None:
        await _show_offers(ev, ch)
        return
    text = f"<b>{esc(offer['title'])}</b>\n\n{esc(offer['description'])}"
    kb: list[list[Btn]] = []
    if offer["url"]:
        kb.append([Btn(text=offer["button_text"] or "Открыть", url=offer["url"])])
    kb.append([Btn(text="⬅️ Назад", data="g:offers"), Btn(text="🏠 В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb, photo=offer["photo_path"]),
                   new_message=bool(offer["photo_path"]))


# ============================================================== мои заказы
async def _show_my_orders(ev: Event, ch: Channel) -> None:
    orders = await repo.list_orders(user_key=(ev.channel, ev.user_id), limit=8)
    if not orders:
        await _respond(ev, ch, Out(text="У вас пока нет заказов.",
                                   kb=[[Btn(text="🥐 Заказать завтрак", data="g:order")],
                                       [Btn(text="⬅️ В меню", data="g:menu")]]))
        return
    lines = ["📦 <b>Ваши заказы</b>", ""]
    kb: list[list[Btn]] = []
    for order in orders:
        lines.append(
            f"<b>№{esc(order['number'])}</b> · {fmt_date(order['delivery_date'], False)}\n"
            f"{statuses.label(order['status'])} · {order['qty']} × "
            f"{esc(order['set_title'])} · {fmt_money(order['total_kop'])}"
        )
        kb.append([Btn(text=f"№{order['number']} · {fmt_date(order['delivery_date'], False)}",
                       data=f"g:ord:{order['id']}")])
    lines.append("\n<i>Нажмите на заказ, чтобы открыть его.</i>")
    kb.append([Btn(text="🥐 Заказать ещё", data="g:order"),
               Btn(text="⬅️ В меню", data="g:menu")])
    await _respond(ev, ch, Out(text="\n\n".join(lines), kb=kb))


async def _show_my_order(ev: Event, ch: Channel, order_id: int) -> None:
    order = await repo.get_order(order_id)
    if order is None or order["ext_id"] != str(ev.user_id) or order["channel"] != ev.channel:
        await _show_my_orders(ev, ch)
        return
    text = await notify.order_card(order, for_admin=False)
    kb: list[list[Btn]] = []
    if order["status"] == statuses.ACCEPTED:
        kb.append([Btn(text=f"💳 Оплатить {fmt_money(order['total_kop'])}",
                       data=f"g:pay:{order['id']}", intent="positive")])
    if order["status"] == statuses.DELIVERED:
        kb.append([Btn(text="✅ Я получил заказ", data=f"g:got:{order['id']}", intent="positive")])
    if order["status"] in statuses.GUEST_CANCELLABLE:
        kb.append([Btn(text="❌ Отменить заказ", data=f"g:cancel:{order['id']}", intent="negative")])
    kb.append([Btn(text="⬅️ К заказам", data="g:my"), Btn(text="🏠 В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb))


async def _cancel_order(ev: Event, ch: Channel, order_id: int) -> None:
    ok, message = await orders_service.guest_cancel(order_id, ev.user_id, ev.channel)
    await ch.send(ev.chat_id, Out(text=("✅ " if ok else "⚠️ ") + message))
    await _show_my_orders(ev, ch)


async def _confirm_received(ev: Event, ch: Channel, order_id: int) -> None:
    ok, message = await orders_service.guest_confirm_received(order_id, ev.user_id, ev.channel)
    if not ok:
        await ch.send(ev.chat_id, Out(text="⚠️ " + message))


async def _pay_again(ev: Event, ch: Channel, order_id: int) -> None:
    """Гость нажал «Оплатить» в своих заказах — выставляем счёт заново."""
    order = await repo.get_order(order_id)
    if order is None or order["ext_id"] != str(ev.user_id):
        await _show_my_orders(ev, ch)
        return
    ok, error = await orders_service.send_invoice(order)
    if not ok:
        await ch.send(ev.chat_id, Out(
            text=await repo.render_text("payment_unavailable", number=order["number"])))
        if error:
            log.warning("Счёт по заказу %s не выставлен: %s", order["number"], error)


async def _on_payment(ev: Event, ch: Channel) -> None:
    """Telegram сообщил об успешной оплате."""
    order_id = payments.parse_payload(ev.payload)
    if order_id is None:
        log.warning("Оплата с неизвестной меткой: %r", ev.payload)
        return
    order = await repo.get_order(order_id)
    if order is None:
        log.warning("Оплата по несуществующему заказу: %s", order_id)
        return
    log.info("Заказ %s оплачен (%s)", order["number"], ev.raw.get("charge_id", ""))
    await orders_service.apply_payment_success(order, str(ev.raw.get("charge_id", "")))


# ================================================================ оформление
async def _start_order(ev: Event, ch: Channel) -> None:
    if await repo.get_bool("orders_paused", False):
        await _respond(ev, ch, Out(text=await repo.render_text("orders_paused"),
                                   kb=[[Btn(text="⬅️ В меню", data="g:menu")]]))
        return

    session = await repo.get_session(ev.channel, ev.user_id)
    object_id = session["object_id"] if session else None
    obj = await repo.get_object(object_id)
    if obj is None or not obj["is_active"] or obj["is_general"]:
        await _ask_object(ev, ch)
        return
    await _ask_date(ev, ch)


async def _ask_object(ev: Event, ch: Channel) -> None:
    objects = await repo.list_objects(active_only=True, selectable=True)
    if not objects:
        await _respond(ev, ch, Out(
            text="Пока нет доступных объектов для заказа. Свяжитесь с менеджером.",
            kb=[[Btn(text="⬅️ В меню", data="g:menu")]]))
        return
    await _set_state(ev, S_OBJECT, {})
    kb = [[Btn(text=_object_label(obj), data=f"g:obj:{obj['id']}")] for obj in objects]
    kb.append([Btn(text="⬅️ В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=await repo.render_text("choose_object"), kb=kb))


def _object_label(obj: Row) -> str:
    if obj["group_title"] and obj["group_title"] not in obj["title"]:
        return f"{obj['group_title']} · {obj['title']}"
    return obj["title"]


async def _pick_object(ev: Event, ch: Channel, object_id: int) -> None:
    obj = await repo.get_object(object_id)
    if obj is None or not obj["is_active"]:
        await _ask_object(ev, ch)
        return
    session = await repo.get_session(ev.channel, ev.user_id)
    data = repo.json_loads(session["data"], {}) if session else {}
    await repo.save_session(ev.channel, ev.user_id, S_DATE, data, chat_id=ev.chat_id,
                            object_id=obj["id"])
    await repo.update_user(ev.channel, ev.user_id, object_id=obj["id"])
    await _ask_date(ev, ch)


async def _ask_date(ev: Event, ch: Channel) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    dates = await availability.available_dates(obj)
    if not dates:
        await _respond(ev, ch, Out(text=await repo.render_text("no_dates"),
                                   kb=[[Btn(text="⬅️ В меню", data="g:menu")]]))
        return
    await _set_state(ev, S_DATE, data)
    kb = [[Btn(text=f"{fmt_date_btn(day)} · {breakfast['title']}", data=f"g:date:{fmt_date_iso(day)}")]
          for day, breakfast in dates]
    kb.append([Btn(text="⬅️ В меню", data="g:menu")])
    text = (
        "📅 <b>На какой день привезти завтрак?</b>\n\n"
        f"📍 {esc(obj['address'] or obj['title'])}\n"
        f"💰 {fmt_money(obj['price_kop'])} за сет\n\n"
        "Рядом с датой — сет, который подадут в этот день.\n"
        f"<i>Показаны только те дни, на которые заказ ещё принимается "
        f"(до {esc(obj['cutoff_time'])} накануне).</i>"
    )
    await _respond(ev, ch, Out(text=text, kb=kb))


async def _pick_date(ev: Event, ch: Channel, iso: str) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    day = parse_date(iso)
    if day is None:
        await _ask_date(ev, ch)
        return
    ok, reason = await availability.check_date(obj, day)
    if not ok:
        await ch.send(ev.chat_id, Out(text=f"⚠️ {reason}"))
        await _ask_date(ev, ch)
        return
    breakfast = await repo.set_for_date(day)
    data["date"] = fmt_date_iso(day)
    data["set_id"] = breakfast["id"] if breakfast else None
    await _set_state(ev, S_QTY, data)
    await _ask_qty(ev, ch)


async def _ask_qty(ev: Event, ch: Channel) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    if not data.get("date"):
        await _ask_date(ev, ch)
        return
    breakfast = await repo.get_set(data.get("set_id"))
    low, high = availability.qty_limits(obj)
    price = availability.price_for(obj, breakfast)
    await _set_state(ev, S_QTY, data)

    text = (
        f"📅 <b>{fmt_date(data['date'])}</b>\n\n"
        f"🥐 <b>{esc(breakfast['title'] if breakfast else '—')}</b>"
    )
    if breakfast and breakfast["description"]:
        text += "\n" + esc(breakfast["description"])
    text += f"\n\n💰 {fmt_money(price)} за сет"

    tiers = await pricing.tiers()
    if tiers:
        text += "\n\n🎁 <b>Чем больше наборов, тем дешевле каждый:</b>\n"
        text += "\n".join(
            f"от {t.qty} наборов — −{t.percent}%, "
            f"{fmt_money(pricing.calc(price, t.qty, tiers).per_set)} за сет"
            for t in sorted(tiers, key=lambda t: t.qty)
        )
    text += "\n\n🔢 <b>Сколько наборов привезти?</b>"

    numbers = [
        Btn(text=_qty_label(n, price, tiers), data=f"g:qty:{n}")
        for n in range(low, high + 1)
    ]
    kb = chunk(numbers, 5)
    kb.append([Btn(text="⬅️ Другая дата", data="g:order"), Btn(text="🏠 В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb, photo=breakfast["photo_path"] if breakfast else ""),
                   new_message=bool(breakfast and breakfast["photo_path"]))


def _qty_label(qty: int, base_price: int, tiers: list) -> str:
    """Подпись кнопки количества: со скидкой показываем её прямо на кнопке."""
    percent = pricing.percent_for(qty, tiers)
    return f"{qty} · −{percent}%" if percent else str(qty)


async def _pick_qty(ev: Event, ch: Channel, qty: int) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    low, high = availability.qty_limits(obj)
    if not low <= qty <= high:
        await ch.send(ev.chat_id, Out(text=f"⚠️ Доступно от {low} до {high} наборов."))
        await _ask_qty(ev, ch)
        return
    data["qty"] = qty
    await _set_state(ev, S_APARTMENT, data)
    await _ask_apartment(ev, ch)


async def _ask_apartment(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    await _set_state(ev, S_APARTMENT, data)
    user = await repo.get_user(ev.channel, ev.user_id)
    text = await repo.render_text("ask_apartment")
    kb: list[list[Btn]] = []
    if user and user["apartment"]:
        text += f"\n\nВ прошлый раз вы указывали: <b>{esc(user['apartment'])}</b>"
        kb.append([Btn(text=f"🚪 Снова {user['apartment']}", data="g:reapt", intent="positive")])
    kb.append([Btn(text=BACK_LABEL, data="g:qty"), Btn(text="🏠 В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb, remove_reply_kb=True))


async def _reuse_apartment(ev: Event, ch: Channel) -> None:
    user = await repo.get_user(ev.channel, ev.user_id)
    _, data = await _session_object(ev)
    if not (user and user["apartment"]):
        await _ask_apartment(ev, ch)
        return
    data["apartment"] = user["apartment"]
    await _set_state(ev, S_PHONE, data)
    await _ask_phone(ev, ch)


async def _ask_phone(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    await _set_state(ev, S_PHONE, data)
    user = await repo.get_user(ev.channel, ev.user_id)
    text = await repo.render_text("ask_phone")
    kb: list[list[Btn]] = []
    if user and user["phone"]:
        text += f"\n\nСохранённый номер: <b>{esc(fmt_phone(user['phone']))}</b>"
        kb.append([Btn(text=f"📱 {fmt_phone(user['phone'])}", data="g:rephone", intent="positive")])
    kb.append([Btn(text=BACK_LABEL, data="g:apt")])
    await _respond(ev, ch, Out(text=text, kb=kb, reply_contact=CONTACT_LABEL), new_message=True)


async def _reuse_phone(ev: Event, ch: Channel) -> None:
    user = await repo.get_user(ev.channel, ev.user_id)
    _, data = await _session_object(ev)
    if not (user and user["phone"]):
        await _ask_phone(ev, ch)
        return
    await _input_phone(ev, ch, user["phone"], data, user["customer_name"])


async def _ask_comment(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    if not await repo.get_bool("comment_enabled", True):
        data["comment"] = ""
        await _set_state(ev, S_CONFIRM, data)
        await _show_confirm(ev, ch)
        return
    await _set_state(ev, S_COMMENT, data)
    kb = [[Btn(text=SKIP_LABEL, data="g:skip")],
          [Btn(text=BACK_LABEL, data="g:phone"), Btn(text="🏠 В меню", data="g:menu")]]
    await _respond(ev, ch, Out(text=await repo.render_text("ask_comment"), kb=kb,
                               remove_reply_kb=True), new_message=True)


async def _skip_comment(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    data["comment"] = ""
    await _set_state(ev, S_CONFIRM, data)
    await _show_confirm(ev, ch)


async def _show_confirm(ev: Event, ch: Channel) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    if not (data.get("date") and data.get("qty") and data.get("apartment") and data.get("phone")):
        await _resume_incomplete(ev, ch, data)
        return
    breakfast = await repo.get_set(data.get("set_id"))
    base_price = availability.price_for(obj, breakfast)
    qty = int(data.get("qty", 1))
    price = await pricing.price_for_order(base_price, qty)
    await _set_state(ev, S_CONFIRM, data)

    lines = [
        "🧾 <b>Проверьте заказ</b>",
        "",
        f"📅 <b>{fmt_date(data['date'])}</b>",
        f"🥐 {esc(breakfast['title'] if breakfast else '—')}",
        f"🔢 {qty} {plural(qty, 'набор', 'набора', 'наборов')}",
        f"📍 {esc(obj['address'] or obj['title'])}",
        f"🚪 Апартаменты {esc(data.get('apartment', ''))}",
        f"📞 {esc(fmt_phone(data.get('phone', '')))}",
    ]
    if data.get("name"):
        lines.append(f"👤 {esc(data['name'])}")
    if data.get("comment"):
        lines.append(f"💬 {esc(data['comment'])}")
    lines.append("")
    if price.percent:
        lines += [
            f"💰 {fmt_money(price.base_per_set)} × {qty} = {fmt_money(base_price * qty)}",
            f"🎁 Скидка −{price.percent}% = <b>−{fmt_money(price.saved)}</b>",
            f"<b>К оплате: {fmt_money(price.total)}</b>",
        ]
    else:
        lines.append(f"💰 {fmt_money(price.per_set)} × {qty} = <b>{fmt_money(price.total)}</b>")
    lines += ["", "Всё верно? Любой пункт ещё можно поменять."]
    kb = [
        [Btn(text="✅ Подтвердить заказ", data="g:confirm", intent="positive")],
        [Btn(text="✏️ Изменить", data="g:edit"),
         Btn(text="❌ Отменить", data="g:menu", intent="negative")],
    ]
    await _respond(ev, ch, Out(text="\n".join(lines), kb=kb, remove_reply_kb=True))


async def _show_edit(ev: Event, ch: Channel) -> None:
    """Что именно поменять в заказе — отдельным экраном, чтобы не пугать кнопками."""
    kb = [
        [Btn(text="📅 Дату доставки", data="g:order")],
        [Btn(text="🔢 Количество наборов", data="g:qty")],
        [Btn(text="🚪 Номер апартаментов", data="g:apt")],
        [Btn(text="📞 Телефон", data="g:phone")],
        [Btn(text="⬅️ Назад к заказу", data="g:back")],
    ]
    await _respond(ev, ch, Out(text="✏️ <b>Что поправить?</b>", kb=kb))


async def _resume_incomplete(ev: Event, ch: Channel, data: dict[str, Any]) -> None:
    """Данных не хватает — возвращаем гостя на нужный шаг."""
    if not data.get("date"):
        await _ask_date(ev, ch)
    elif not data.get("qty"):
        await _ask_qty(ev, ch)
    elif not data.get("apartment"):
        await _ask_apartment(ev, ch)
    else:
        await _ask_phone(ev, ch)


async def _confirm_order(ev: Event, ch: Channel) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    required = ("date", "qty", "apartment", "phone")
    if any(not data.get(key) for key in required):
        await ch.send(ev.chat_id, Out(text="⚠️ Заказ заполнен не полностью, начнём заново."))
        await _start_order(ev, ch)
        return

    day = parse_date(data["date"])
    ok, reason = await availability.check_date(obj, day) if day else (False, "Некорректная дата")
    if not ok:
        await ch.send(ev.chat_id, Out(text=f"⚠️ {reason}\nВыберите другую дату."))
        await _ask_date(ev, ch)
        return

    breakfast = await repo.set_for_date(day)
    base_price = availability.price_for(obj, breakfast)
    qty = int(data["qty"])
    price = await pricing.price_for_order(base_price, qty)
    user = await repo.get_user(ev.channel, ev.user_id)
    name = str(data.get("name") or (user["customer_name"] if user else "")
               or (user["full_name"] if user else "") or ev.full_name)

    order = await repo.create_order(
        user_pk=user["id"] if user else None,
        channel=ev.channel,
        ext_id=str(ev.user_id),
        chat_id=str(ev.chat_id),
        object_id=obj["id"],
        object_title=obj["title"],
        object_address=obj["address"],
        set_id=breakfast["id"] if breakfast else None,
        set_title=breakfast["title"] if breakfast else "",
        delivery_date=fmt_date_iso(day),
        qty=qty,
        apartment=str(data["apartment"]),
        phone=str(data["phone"]),
        customer_name=name,
        comment=str(data.get("comment", "")),
        base_price_kop=price.base_per_set,
        discount_pct=price.percent,
        price_kop=price.per_set,
        total_kop=price.total,
        status=statuses.NEW,
        source_code=(user["source_code"] if user else "") or obj["code"],
    )

    await repo.update_user(ev.channel, ev.user_id, phone=data["phone"],
                           apartment=str(data["apartment"]), customer_name=name,
                           object_id=obj["id"])
    await repo.clear_session(ev.channel, ev.user_id)

    text = await notify.guest_status_text(order, "order_accepted")
    await ch.send(ev.chat_id, Out(text=text, kb=[[Btn(text="📦 Мои заказы", data="g:my")]],
                                  remove_reply_kb=True))
    await notify.notify_new_order(order)
    await _send_upsell(ev, ch)


async def _send_upsell(ev: Event, ch: Channel) -> None:
    offers = await repo.list_offers(active_only=True)
    if not offers:
        return
    kb = [[Btn(text=offer["title"], data=f"g:offer:{offer['id']}")] for offer in offers]
    kb.append([Btn(text="🏠 В меню", data="g:menu")])
    await ch.send(ev.chat_id, Out(text=await repo.render_text("upsell_intro"), kb=kb))


# ============================================================= текстовый ввод
async def _on_text(ev: Event, ch: Channel, user: Row) -> None:
    text = (ev.text or "").strip()
    session = await repo.get_session(ev.channel, ev.user_id)
    state = session["state"] if session else S_NONE
    data = repo.json_loads(session["data"], {}) if session else {}

    if text in ("/start", "/menu"):
        await _cmd_start(ev, ch)
        return

    if text == BACK_LABEL:
        await _back_from(ev, ch, state)
        return
    if text == SKIP_LABEL and state == S_COMMENT:
        await _skip_comment(ev, ch)
        return

    if state == S_APARTMENT:
        await _input_apartment(ev, ch, text, data)
        return
    if state == S_PHONE:
        phone = ev.phone if ev.kind == "contact" else text
        await _input_phone(ev, ch, phone, data, str(ev.raw.get("contact_name", "")))
        return
    if state == S_COMMENT:
        await _input_comment(ev, ch, text, data)
        return

    # вне сценария — просто показываем меню
    await _show_main_menu(ev, ch, new_message=True)


async def _back_from(ev: Event, ch: Channel, state: str) -> None:
    if state == S_PHONE:
        await _ask_apartment(ev, ch)
    elif state == S_COMMENT:
        await _ask_phone(ev, ch)
    elif state == S_APARTMENT:
        await _ask_qty(ev, ch)
    else:
        await _show_main_menu(ev, ch, new_message=True)


async def _input_apartment(ev: Event, ch: Channel, text: str, data: dict[str, Any]) -> None:
    value = text.strip()
    if not value or len(value) > 12:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Укажите номер апартаментов — до 12 символов, например <b>45</b> или <b>12А</b>."))
        return
    data["apartment"] = value
    await _set_state(ev, S_PHONE, data)
    await _ask_phone(ev, ch)


async def _input_phone(ev: Event, ch: Channel, raw: str, data: dict[str, Any],
                       name: str = "") -> None:
    phone = norm_phone(raw)
    if not phone:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Не похоже на номер телефона. Пришлите его в формате "
                 "<b>+7 999 123-45-67</b> или нажмите кнопку «Поделиться контактом»."))
        return
    data["phone"] = phone
    if name.strip():
        # имя из карточки контакта — его требует форма курьерской службы
        data["name"] = name.strip()
    await _set_state(ev, S_COMMENT, data)
    await _ask_comment(ev, ch)


async def _input_comment(ev: Event, ch: Channel, text: str, data: dict[str, Any]) -> None:
    data["comment"] = text[:300]
    await _set_state(ev, S_CONFIRM, data)
    await _show_confirm(ev, ch)


# ================================================================= черновики
async def _resume_draft(ev: Event, ch: Channel) -> None:
    session = await repo.get_session(ev.channel, ev.user_id)
    state = session["state"] if session else S_NONE
    routes = {
        S_OBJECT: _ask_object,
        S_DATE: _ask_date,
        S_QTY: _ask_qty,
        S_APARTMENT: _ask_apartment,
        S_PHONE: _ask_phone,
        S_COMMENT: _ask_comment,
        S_CONFIRM: _show_confirm,
    }
    handler = routes.get(state)
    if handler is None:
        await _start_order(ev, ch)
        return
    await handler(ev, ch)


async def _drop_draft(ev: Event, ch: Channel) -> None:
    await repo.clear_session(ev.channel, ev.user_id)
    await _show_main_menu(ev, ch)


# =================================================================== хелперы
async def _session_object(ev: Event) -> tuple[Optional[Row], dict[str, Any]]:
    session = await repo.get_session(ev.channel, ev.user_id)
    if session is None:
        user = await repo.get_user(ev.channel, ev.user_id)
        return await repo.get_object(user["object_id"] if user else None), {}
    data = repo.json_loads(session["data"], {})
    return await repo.get_object(session["object_id"]), data


async def _set_state(ev: Event, state: str, data: dict[str, Any]) -> None:
    session = await repo.get_session(ev.channel, ev.user_id)
    object_id = session["object_id"] if session else None
    await repo.save_session(ev.channel, ev.user_id, state, data, chat_id=ev.chat_id,
                            object_id=object_id)
