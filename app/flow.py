"""Сценарий гостя. Один код — оба мессенджера (Telegram и MAX).

Состояния хранятся в БД (таблица sessions), поэтому:
  • перезапуск бота не роняет незавершённые заказы;
  • работает напоминание о брошенной корзине;
  • логика в Telegram и MAX идентична.

Заказ можно оформить сразу на несколько дат: гость отмечает дни, бот сам
подставляет сет по ротации на каждый из них, а в базе получается один заказ
из нескольких строк — по одной на дату.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import (availability, media, notify, orders_service, payments, pricing, repo,
               statuses)
from .channels.base import Btn, Channel, Event, Out, get_channel
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
    today,
)

log = logging.getLogger(__name__)
Row = Any

# состояния
S_NONE = ""
S_ADDRESS = "address"
S_REQUEST = "request"
S_RECEIPT = "receipt"
S_REVIEW = "review"
S_DATE = "date"
S_QTY = "qty"
S_APARTMENT = "apartment"
S_PHONE = "phone"
S_ALLERGY = "allergy"
S_COMMENT = "comment"
S_CONFIRM = "confirm"

BACK_LABEL = "⬅️ Назад"
SKIP_LABEL = "⏭ Пропустить"
CONTACT_LABEL = "📞 Поделиться контактом"

#: сколько дат максимум можно отметить в одном заказе
OBJECTS_PER_PAGE = 8
MAX_DATES = 14


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
    await _pin_support_button(ev, ch)
    code = _clean_code(ev.payload)
    obj = await repo.get_object_by_code(code) if code else None

    if code:
        await repo.add_qr_visit(code, ev.channel, fmt_date_iso(today()))

    if code and obj is None:
        await ch.send(ev.chat_id, Out(
            text="😕 Такой QR-код не найден или больше не действует.\n"
                 "Оформите заказ обычной кнопкой — бот спросит адрес."))
    elif obj is not None and not obj["is_active"]:
        await ch.send(ev.chat_id, Out(
            text="⏸ Приём заказов по этому объекту временно приостановлен."))
        obj = None

    session_object: Optional[int] = None
    if obj is not None:
        await repo.update_user(ev.channel, ev.user_id, source_code=obj["code"])
        if obj["is_general"]:
            session_object = obj["id"]       # общий QR: адрес спросим при заказе
        else:
            session_object = obj["id"]
            await repo.update_user(ev.channel, ev.user_id, object_id=obj["id"])
    else:
        user = await repo.get_user(ev.channel, ev.user_id)
        session_object = user["object_id"] if user else None

    await repo.save_session(ev.channel, ev.user_id, S_NONE, {}, chat_id=ev.chat_id,
                            object_id=session_object)
    if session_object is None:
        # дом неизвестен: по QR не пришли или код не подошёл — предлагаем выбрать
        await _ask_object(ev, ch, new_message=True)
        return
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

    if obj is not None and not obj["is_general"]:
        text = await repo.render_text(
            "welcome_object",
            object_title=obj["title"],
            address=obj["address"] or obj["title"],
            price=fmt_money(await availability.price_of(obj)),
            cutoff=obj["cutoff_time"],
            delivery_time=obj["delivery_time"],
            delivery_window=repo.delivery_window(obj),
        )
    else:
        text = await repo.render_text("welcome")

    kb = [
        [Btn(text="🥐 Заказать завтрак", data="g:order", intent="positive")],
        [Btn(text="📋 Меню и цены", data="g:sets"), Btn(text="🚚 Доставка", data="g:delivery")],
        [Btn(text="❓ Как заказать", data="g:how"), Btn(text="💬 Вопросы", data="g:faq")],
        [Btn(text="📋 Условия заказа", data="g:rules")],
    ]
    if await repo.count_orders(user_key=(ev.channel, ev.user_id)):
        kb.append([Btn(text="📦 Мои заказы", data="g:my")])
    if await repo.list_offers(active_only=True):
        kb.append([Btn(text="🤍 Ещё от Fatucci", data="g:offers")])
    if obj is not None and len(await repo.list_objects(active_only=True, selectable=True)) > 1:
        # домов несколько — гость мог выбрать не тот или переехать
        kb.append([Btn(text="🏠 Сменить апартаменты", data="g:objs")])
    kb.append([_manager_btn()])

    await _respond(ev, ch, Out(text=text, kb=kb), new_message=new_message)


def _manager_btn() -> Btn:
    return Btn(text="✉️ Связаться с менеджером", data="g:manager")


# ==================================================================== поддержка
async def _pin_support_button(ev: Event, ch: Channel) -> None:
    """Закрепить постоянные кнопки под полем ввода — один раз на чат.

    Поддержка есть у всех. У администратора она стоит рядом с админ-панелью,
    поэтому ему закрепляем сразу обе кнопки одним рядом.
    """
    from . import admins

    try:
        if await admins.is_admin(ev.user_id):
            await ch.show_admin_button(
                str(ev.chat_id),
                "🛠 Внизу закреплены <b>Админ-панель</b> и <b>Поддержка</b> — "
                "они всегда под рукой.")
        else:
            await ch.show_support_button(str(ev.chat_id),
                                         await repo.render_text("support_pinned"))
    except Exception:  # noqa: BLE001
        log.debug("Не удалось закрепить кнопки под полем ввода", exc_info=True)


async def show_support(ev: Event, ch: Channel) -> None:
    """Контакты техподдержки. Вызывается кнопкой под полем ввода и из меню."""
    contact = (await repo.get_setting("support_contact")).strip()
    kb: list[list[Btn]] = []
    link = _contact_link(contact)
    if link:
        kb.append([Btn(text="✍️ Написать в поддержку", url=link)])
    from . import admins

    if await admins.is_admin(ev.user_id):
        kb.append([Btn(text="🛠 Админ-панель", data="a:h")])
    else:
        kb.append([Btn(text="🏠 В начало", data="g:menu")])
    await _respond(ev, ch, Out(text=await repo.render_text("support"), kb=kb),
                   new_message=ev.kind != "callback")


def _contact_link(contact: str) -> str:
    """Ссылка на переписку по @username. Телефон и ссылки оставляем как есть."""
    value = contact.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("@") and len(value) > 1:
        return f"https://t.me/{value[1:]}"
    return ""


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
        "rules": lambda: _show_info(ev, ch, "rules"),
        "manager": lambda: _contact_manager(ev, ch),
        "objs": lambda: _ask_object(ev, ch),
        "objp": lambda: _ask_object(ev, ch, page=int(arg or 0)),
        "obj": lambda: _pick_object(ev, ch, int(arg or 0)),
        "objnew": lambda: _ask_new_object(ev, ch),
        "addr": lambda: _ask_address(ev, ch),
        "offers": lambda: _show_offers(ev, ch),
        "offer": lambda: _show_offer(ev, ch, int(arg or 0)),
        "my": lambda: _show_my_orders(ev, ch),
        "ord": lambda: _show_my_order(ev, ch, int(arg or 0)),
        "cancel": lambda: _cancel_order(ev, ch, int(arg or 0), whole=False),
        "cancelall": lambda: _cancel_order(ev, ch, int(arg or 0), whole=True),
        "got": lambda: _confirm_received(ev, ch, int(arg or 0)),
        "paid": lambda: _mark_paid(ev, ch, int(arg or 0)),
        "paidnp": lambda: _send_paid(ev, ch, int(arg or 0)),
        "star": lambda: _pick_stars(ev, ch, parts[2:]),
        "revskip": lambda: _finish_review(ev, ch, int(arg or 0)),
        "date": lambda: _toggle_date(ev, ch, arg),
        "dates": lambda: _dates_done(ev, ch),
        "date_back": lambda: _ask_date(ev, ch),
        "qty": lambda: (_pick_qty(ev, ch, int(arg)) if arg else _ask_qty(ev, ch)),
        "apt": lambda: _ask_apartment(ev, ch),
        "reapt": lambda: _reuse_apartment(ev, ch),
        "phone": lambda: _ask_phone(ev, ch),
        "rephone": lambda: _reuse_phone(ev, ch),
        "allergy": lambda: _ask_allergy(ev, ch),
        "skipa": lambda: _skip_allergy(ev, ch),
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
    obj = (await _session_object(ev))[0]
    text = await repo.render_text(
        key,
        cutoff=obj["cutoff_time"] if obj is not None else await repo.default_cutoff(),
        delivery_time=obj["delivery_time"] if obj is not None
        else await repo.default_delivery_time(),
        delivery_window=repo.delivery_window(obj) if obj is not None
        else await repo.default_delivery_window(),
    )
    kb = [[Btn(text="🥐 Заказать", data="g:order"), Btn(text="⬅️ В меню", data="g:menu")],
          [_manager_btn()]]
    await _respond(ev, ch, Out(text=text, kb=kb))


async def _contact_manager(ev: Event, ch: Channel) -> None:
    contact = await repo.get_setting("manager_contact")
    phone = await repo.get_setting("manager_phone")
    lines = ["✉️ <b>Связаться с менеджером</b>", ""]
    if contact:
        lines.append(f"Telegram: {esc(contact)}")
    if phone:
        lines.append(f"Телефон: {esc(phone)}")
    lines += ["", "Напишите нам по любому вопросу — о заказе, доставке или оплате."]
    await _respond(ev, ch, Out(text="\n".join(lines),
                               kb=[[Btn(text="⬅️ В меню", data="g:menu")]]))


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
    obj = (await _session_object(ev))[0]
    price = await availability.price_of(obj, item) if obj is not None \
        else (item["price_kop"] or 0)
    text = f"🥐 <b>{esc(item['title'])}</b>"
    if item["description"]:
        text += "\n\n" + esc(item["description"])
    if price:
        text += f"\n\n💰 <b>{fmt_money(price)}</b> за сет"
    kb = [[Btn(text="🥐 Заказать", data="g:order")],
          [Btn(text="⬅️ К меню", data="g:sets"), Btn(text="🏠 В начало", data="g:menu")]]
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
    kb.append([Btn(text="⬅️ Назад", data="g:offers"), Btn(text="🏠 В начало", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb, photo=offer["photo_path"]),
                   new_message=bool(offer["photo_path"]))


# ============================================================== мои заказы
async def _show_my_orders(ev: Event, ch: Channel) -> None:
    groups = await repo.list_order_groups(limit=8, user_key=(ev.channel, ev.user_id))
    if not groups:
        await _respond(ev, ch, Out(text="У вас пока нет заказов.",
                                   kb=[[Btn(text="🥐 Заказать завтрак", data="g:order")],
                                       [Btn(text="⬅️ В меню", data="g:menu")]]))
        return
    lines = ["📦 <b>Ваши заказы</b>", ""]
    kb: list[list[Btn]] = []
    for group in groups:
        head = group[0]
        number = head["group_key"] or head["number"]
        days = len([row for row in group if row["status"] != statuses.CANCELLED]) or len(group)
        lines.append(
            f"<b>№{esc(number)}</b> · {statuses.label(head['status'])}\n"
            f"{days} {plural(days, 'день', 'дня', 'дней')} · "
            f"{fmt_money(notify.group_total(group))}"
        )
        kb.append([Btn(text=f"№{number} · {fmt_date(head['delivery_date'], False)}",
                       data=f"g:ord:{head['id']}")])
    kb.append([Btn(text="🥐 Заказать ещё", data="g:order"),
               Btn(text="⬅️ В меню", data="g:menu")])
    await _respond(ev, ch, Out(text="\n\n".join(lines), kb=kb))


async def _show_my_order(ev: Event, ch: Channel, order_id: int) -> None:
    order = await repo.get_order(order_id)
    if order is None or order["ext_id"] != str(ev.user_id) or order["channel"] != ev.channel:
        await _show_my_orders(ev, ch)
        return
    group = await repo.group_of(order)
    text = await notify.order_card(order, for_admin=False, group=group)
    deadline = await repo.get_setting("cancel_deadline", "18:30")
    text += (f"\n\n<i>Отменить или изменить заказ можно до {esc(deadline)} "
             "предыдущего дня доставки.</i>")

    kb: list[list[Btn]] = []
    if order["status"] == statuses.ACCEPTED and not await payments.invoice_available():
        kb.append([Btn(text="✅ Я оплатил", data=f"g:paid:{order['id']}", intent="positive")])
    if order["status"] == statuses.DELIVERED:
        kb.append([Btn(text="✅ Я получил заказ", data=f"g:got:{order['id']}", intent="positive")])
    if order["status"] in statuses.GUEST_CANCELLABLE:
        label = "❌ Отменить весь заказ" if len(group) > 1 else "❌ Отменить заказ"
        kb.append([Btn(text=label, data=f"g:cancelall:{order['id']}", intent="negative")])
        if len(group) > 1:
            for row in group:
                if row["status"] in statuses.GUEST_CANCELLABLE:
                    kb.append([Btn(text=f"❌ Отменить {fmt_date(row['delivery_date'], False)}",
                                   data=f"g:cancel:{row['id']}")])
    kb.append([Btn(text="📋 Условия", data="g:rules"), _manager_btn()])
    kb.append([Btn(text="⬅️ К заказам", data="g:my"), Btn(text="🏠 В начало", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb))


async def _cancel_order(ev: Event, ch: Channel, order_id: int, whole: bool) -> None:
    ok, message = await orders_service.guest_cancel(order_id, ev.user_id, ev.channel,
                                                    whole_order=whole)
    await ch.send(ev.chat_id, Out(text=("✅ " if ok else "⚠️ ") + message))
    await _show_my_orders(ev, ch)


async def _confirm_received(ev: Event, ch: Channel, order_id: int) -> None:
    ok, message = await orders_service.guest_confirm_received(order_id, ev.user_id, ev.channel)
    if not ok:
        await ch.send(ev.chat_id, Out(text="⚠️ " + message))


async def _mark_paid(ev: Event, ch: Channel, order_id: int) -> None:
    """Гость нажал «Я оплатил» — сначала просим скриншот платежа."""
    order = await repo.get_order(order_id)
    if order is None or order["ext_id"] != str(ev.user_id) or order["channel"] != ev.channel:
        await _answer(ev, ch, "Это не ваш заказ")
        return
    if order["status"] not in (statuses.NEW, statuses.ACCEPTED):
        await _answer(ev, ch, f"Заказ уже в статусе «{statuses.label(order['status'])}»")
        return

    _, data = await _session_object(ev)
    data["paid_order"] = order_id
    await _set_state(ev, S_RECEIPT, data)
    await _answer(ev, ch)
    await _respond(ev, ch, Out(
        text=await repo.render_text("ask_receipt"),
        kb=[[Btn(text="Отправить без скриншота", data=f"g:paidnp:{order_id}")],
            [Btn(text="⬅️ К заказу", data=f"g:ord:{order_id}")]]), new_message=True)


async def _send_paid(ev: Event, ch: Channel, order_id: int, receipt: str = "") -> None:
    """Сообщить менеджерам об оплате — со скриншотом, если он есть."""
    _, data = await _session_object(ev)
    data.pop("paid_order", None)
    await _set_state(ev, S_NONE, data)
    ok, message = await orders_service.guest_marked_paid(
        order_id, ev.user_id, ev.channel, receipt=receipt)
    await _respond(ev, ch, Out(text=message if ok else f"⚠️ {esc(message)}",
                               kb=[[Btn(text="📦 Мои заказы", data="g:my")],
                                   [_manager_btn()]]), new_message=True)


async def _input_receipt(ev: Event, ch: Channel, data: dict[str, Any]) -> None:
    """Скриншот оплаты от гостя."""
    order_id = int(data.get("paid_order") or 0)
    if not order_id:
        await _show_main_menu(ev, ch, new_message=True)
        return

    file_id = str(ev.raw.get("photo_file_id", ""))
    if not file_id:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Нужен именно скриншот — пришлите его картинкой.\n"
                 "Если скриншота нет, нажмите «Отправить без скриншота».",
            kb=[[Btn(text="Отправить без скриншота", data=f"g:paidnp:{order_id}")]]))
        return

    receipt = ""
    raw = await ch.download_bytes(file_id)
    key = f"receipt:{order_id}"
    if raw and await media.save(key, raw):
        receipt = key
    else:
        log.warning("Скриншот оплаты по заказу %s не сохранился", order_id)
    await _send_paid(ev, ch, order_id, receipt)


async def _on_payment(ev: Event, ch: Channel) -> None:
    """Telegram подтвердил оплату встроенного счёта."""
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


async def _start_order(ev: Event, ch: Channel) -> None:
    if await repo.get_bool("orders_paused", False):
        await _respond(ev, ch, Out(text=await repo.render_text("orders_paused"),
                                   kb=[[_manager_btn()],
                                       [Btn(text="⬅️ В меню", data="g:menu")]]))
        return

    if not await payments.available():
        # ни реквизитов, ни кассы — оплатить будет нечем, не начинаем оформление
        await _respond(ev, ch, Out(text=await repo.render_text("no_payment"),
                                   kb=[[_manager_btn()],
                                       [Btn(text="⬅️ В меню", data="g:menu")]]))
        return

    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    if obj["is_general"] and not data.get("address"):
        # общий QR: дом неизвестен — предлагаем выбрать из списка
        await _ask_object(ev, ch)
        return
    await _ask_date(ev, ch)


# ------------------------------------------------------------------ отзывы
async def offer_review(order: Row) -> None:
    """Предложить оценить сервис — после первого и пятого завершённых заказов."""
    channel = get_channel(order["channel"])
    if channel is None:
        return
    kb = [[Btn(text="★" * n, data=f"g:star:{order['id']}:{n}") for n in (1, 2, 3)],
          [Btn(text="★" * n, data=f"g:star:{order['id']}:{n}") for n in (4, 5)]]
    await channel.send(order["chat_id"] or order["ext_id"],
                       Out(text=await repo.render_text("ask_review"), kb=kb))


async def _pick_stars(ev: Event, ch: Channel, args: list[str]) -> None:
    """Гость выбрал оценку — заводим отзыв и просим пару слов."""
    if len(args) < 2:
        return
    order = await repo.get_order(int(args[0] or 0))
    stars = max(1, min(5, int(args[1] or 5)))
    number = (order["group_key"] or order["number"]) if order else ""
    review_id = await repo.create_review(ev.channel, str(ev.user_id), number, stars)

    _, data = await _session_object(ev)
    data["review_id"] = review_id
    await _set_state(ev, S_REVIEW, data)
    await _answer(ev, ch, "Спасибо!")
    await _respond(ev, ch, Out(
        text="★" * stars + "☆" * (5 - stars) + "\n\n"
             + await repo.render_text("ask_review_text"),
        kb=[[Btn(text=SKIP_LABEL, data=f"g:revskip:{review_id}")]]))


async def _input_review(ev: Event, ch: Channel, text: str, data: dict[str, Any]) -> None:
    """Комментарий к отзыву: текст, фото или и то и другое."""
    review_id = int(data.get("review_id") or 0)
    if not review_id:
        await _show_main_menu(ev, ch, new_message=True)
        return

    file_id = str(ev.raw.get("photo_file_id", ""))
    photo_key = ""
    if file_id:
        raw = await ch.download_bytes(file_id)
        key = f"review:{review_id}"
        if raw and await media.save(key, raw):
            photo_key = key
    await repo.update_review(review_id, comment=text.strip()[:600], photo_key=photo_key)
    await _finish_review(ev, ch, review_id)


async def _finish_review(ev: Event, ch: Channel, review_id: int) -> None:
    """Отправить готовый отзыв и поблагодарить гостя."""
    _, data = await _session_object(ev)
    data.pop("review_id", None)
    await _set_state(ev, S_NONE, data)

    review = await repo.get_review(review_id)
    if review is not None and not review["sent"]:
        user = await repo.get_user(ev.channel, ev.user_id)
        if await notify.send_review(review, user):
            await repo.update_review(review_id, sent=1)
    await _answer(ev, ch, "Спасибо!")
    await _respond(ev, ch, Out(text=await repo.render_text("review_thanks"),
                               kb=[[Btn(text="🥐 Заказать ещё", data="g:order")]]),
                   new_message=True)


# ------------------------------------------------------- выбор апартаментов
async def _ask_object(ev: Event, ch: Channel, page: int = 0,
                      new_message: bool = False) -> None:
    """Список домов кнопками. Когда дом неизвестен — с этого начинается заказ."""
    objects = await repo.list_objects(active_only=True, selectable=True)
    if not objects:
        # объектов ещё не завели — гостю остаётся только оставить адрес
        await _ask_new_object(ev, ch, new_message=new_message)
        return

    pages = chunk(objects, OBJECTS_PER_PAGE)
    page = max(0, min(page, len(pages) - 1))
    kb = [[Btn(text=f"🏢 {obj['title']}", data=f"g:obj:{obj['id']}")] for obj in pages[page]]
    if len(pages) > 1:
        nav = []
        if page:
            nav.append(Btn(text="⬅️ Назад", data=f"g:objp:{page - 1}"))
        nav.append(Btn(text=f"{page + 1}/{len(pages)}", data="g:noop"))
        if page + 1 < len(pages):
            nav.append(Btn(text="Ещё ➡️", data=f"g:objp:{page + 1}"))
        kb.append(nav)
    kb.append([Btn(text="✍️ Ввести свой адрес", data="g:addr")])

    kb.append([Btn(text="➕ Сообщить о новом доме", data="g:objnew")])
    kb.append([Btn(text="📋 Меню и цены", data="g:sets"), _manager_btn()])
    await _respond(ev, ch, Out(text=await repo.render_text("choose_object"), kb=kb),
                   new_message=new_message)


async def _pick_object(ev: Event, ch: Channel, object_id: int) -> None:
    """Гость выбрал дом — запоминаем его и открываем меню."""
    obj = await repo.get_object(object_id)
    if obj is None or not obj["is_active"] or obj["is_general"]:
        await _ask_object(ev, ch)
        return
    await repo.update_user(ev.channel, ev.user_id, object_id=obj["id"])
    await repo.save_session(ev.channel, ev.user_id, S_NONE, {}, chat_id=ev.chat_id,
                            object_id=obj["id"])
    await _show_main_menu(ev, ch)


async def _ask_new_object(ev: Event, ch: Channel, new_message: bool = False) -> None:
    _, data = await _session_object(ev)
    await _set_state(ev, S_REQUEST, data)
    kb = [[Btn(text="⬅️ К списку домов", data="g:objs")],
          [Btn(text="🏠 В начало", data="g:menu")]]
    await _respond(ev, ch, Out(text=await repo.render_text("request_object"), kb=kb),
                   new_message=new_message)


async def _input_request(ev: Event, ch: Channel, text: str, data: dict[str, Any]) -> None:
    """Гость прислал адрес, которого нет в списке."""
    address = " ".join(text.split())[:200]
    if len(address) < 5:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Напишите улицу и номер дома — например, <b>Советская 16</b>."))
        return

    await repo.save_session(ev.channel, ev.user_id, S_NONE, {}, chat_id=ev.chat_id)
    user = await repo.get_user(ev.channel, ev.user_id)
    delivered = await notify.new_object_request(
        ev.channel, str(ev.user_id), ev.username or (user["username"] if user else ""),
        ev.full_name or (user["full_name"] if user else ""), address)
    if not delivered:
        log.warning("Запрос апартаментов «%s» никому не доставлен", address)

    await _respond(ev, ch, Out(
        text=await repo.render_text("request_sent", address=esc(address)),
        kb=[[Btn(text="🏠 В начало", data="g:menu")], [_manager_btn()]]), new_message=True)


# ------------------------------------------------------------------- адрес
async def address_rejected(user: Row) -> None:
    """Менеджер отказал по адресу — просим у гостя другой."""
    channel = get_channel(user["channel"])
    if channel is None:
        return
    text = await repo.render_text("address_rejected",
                                  address=esc(user["custom_address"] or "—"))
    kb = [[Btn(text="🏢 Выбрать из списка", data="g:objs")],
          [Btn(text="✍️ Ввести другой адрес", data="g:addr")]]
    await channel.send(user["chat_id"] or user["ext_id"], Out(text=text, kb=kb))


async def address_accepted(user: Row) -> None:
    """Менеджер подтвердил адрес — сообщаем гостю."""
    channel = get_channel(user["channel"])
    if channel is None:
        return
    await channel.send(user["chat_id"] or user["ext_id"], Out(
        text=await repo.render_text("address_accepted",
                                    address=esc(user["custom_address"] or "—")),
        kb=[[Btn(text="🥐 Заказать завтрак", data="g:order")]]))


async def _ask_address(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    await _set_state(ev, S_ADDRESS, data)
    kb: list[list[Btn]] = []
    user = await repo.get_user(ev.channel, ev.user_id)
    if user and user["object_id"]:
        known = await repo.get_object(user["object_id"])
        if known is not None and not known["is_general"] and known["address"]:
            kb.append([Btn(text=f"📍 {known['address']}", data="g:noop")])
    kb.append([Btn(text="⬅️ В меню", data="g:menu")])
    await _respond(ev, ch, Out(text=await repo.render_text("ask_address"), kb=kb,))


async def _input_address(ev: Event, ch: Channel, text: str, data: dict[str, Any]) -> None:
    address = " ".join(text.split())[:200]
    if len(address) < 5:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Напишите улицу и номер дома — например, <b>Северная 12</b>."))
        return

    matched = await repo.find_object_by_address(address)
    data["address"] = address
    data["address_ok"] = bool(matched)

    if matched is not None:
        await repo.update_user(ev.channel, ev.user_id, object_id=matched["id"],
                               custom_address="", address_status="")
        await repo.save_session(ev.channel, ev.user_id, S_DATE, data, chat_id=ev.chat_id,
                                object_id=matched["id"])
        await _ask_date(ev, ch)
        return

    # дома нет в списке: заказ всё равно оформляем — по общей цене,
    # а менеджеры решают отдельно, возим ли мы туда
    general = await repo.general_object()
    if general is None:
        await ch.send(ev.chat_id, Out(text=await repo.render_text("address_unknown_closed")))
        await _ask_object(ev, ch)
        return

    await repo.save_session(ev.channel, ev.user_id, S_DATE, data, chat_id=ev.chat_id,
                            object_id=general["id"])
    user = await repo.get_user(ev.channel, ev.user_id)
    known = user["custom_address"] if user else ""
    await repo.update_user(ev.channel, ev.user_id, object_id=general["id"],
                           custom_address=address, address_status=repo.ADDRESS_PENDING)

    await ch.send(ev.chat_id, Out(text=await repo.render_text(
        "address_unknown", address=esc(address),
        price=fmt_money(await availability.price_of(general)))))
    if user is not None and known.lower() != address.lower():
        # про один и тот же адрес менеджеров дёргаем один раз
        user = await repo.get_user(ev.channel, ev.user_id)
        await notify.new_address(user, address)
    await _ask_date(ev, ch)


# -------------------------------------------------------------------- даты
async def _ask_date(ev: Event, ch: Channel) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return

    dates = await availability.available_dates(obj, limit=MAX_DATES)
    if not dates:
        await _respond(ev, ch, Out(
            text=await repo.render_text("too_late", cutoff=obj["cutoff_time"]),
            kb=[[_manager_btn()], [Btn(text="⬅️ В меню", data="g:menu")]]))
        return

    chosen = set(data.get("dates", []))
    multi = await repo.get_bool("multiday_enabled", True)
    await _set_state(ev, S_DATE, data)

    kb: list[list[Btn]] = []
    for day, breakfast in dates:
        iso = fmt_date_iso(day)
        mark = "✅ " if iso in chosen else ""
        kb.append([Btn(text=f"{mark}{fmt_date_btn(day)} · {breakfast['title']}",
                       data=f"g:date:{iso}")])
    if chosen:
        kb.append([Btn(text=f"➡️ Далее · выбрано {len(chosen)} "
                            f"{plural(len(chosen), 'день', 'дня', 'дней')}",
                       data="g:dates", intent="positive")])
    kb.append([Btn(text="⬅️ В меню", data="g:menu")])

    text = ["📅 <b>На какие дни привезти завтрак?</b>", ""]
    if obj["address"] and not obj["is_general"]:
        text.append(f"📍 {esc(obj['address'])}")
    elif data.get("address"):
        text.append(f"📍 {esc(data['address'])}")
    text.append(f"💰 {fmt_money(await availability.price_of(obj))} за сет · "
                f"доставка {esc(repo.delivery_window(obj))}")
    text.append("")
    if multi:
        text.append("Можно отметить <b>несколько дней сразу</b> — рядом с датой "
                    "показан сет, который подадут в этот день.")
    else:
        text.append("Рядом с датой — сет, который подадут в этот день.")
    text.append(f"<i>Заказы принимаем до {esc(obj['cutoff_time'])} накануне.</i>")
    await _respond(ev, ch, Out(text="\n".join(text), kb=kb))


async def _toggle_date(ev: Event, ch: Channel, iso: str) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_address(ev, ch)
        return
    day = parse_date(iso)
    if day is None:
        await _ask_date(ev, ch)
        return
    ok, reason = await availability.check_date(obj, day)
    if not ok:
        await _answer(ev, ch, reason[:180])
        await _ask_date(ev, ch)
        return

    chosen: list[str] = list(data.get("dates", []))
    key = fmt_date_iso(day)
    multi = await repo.get_bool("multiday_enabled", True)
    if key in chosen:
        chosen.remove(key)
    elif not multi:
        chosen = [key]
    elif len(chosen) >= MAX_DATES:
        await _answer(ev, ch, f"Больше {MAX_DATES} дней за раз не получится")
        return
    else:
        chosen.append(key)
    data["dates"] = sorted(chosen)
    await _set_state(ev, S_DATE, data)

    if not multi and chosen:
        await _ask_qty(ev, ch)
        return
    await _ask_date(ev, ch)


async def _dates_done(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    if not data.get("dates"):
        await _ask_date(ev, ch)
        return
    await _ask_qty(ev, ch)


# ------------------------------------------------------------- количество
async def _ask_qty(ev: Event, ch: Channel) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_address(ev, ch)
        return
    dates = data.get("dates") or []
    if not dates:
        await _ask_date(ev, ch)
        return

    low, high = availability.qty_limits(obj)
    price = await availability.price_of(obj)
    tiers = await pricing.tiers()
    await _set_state(ev, S_QTY, data)

    first = parse_date(dates[0])
    breakfast = await repo.set_for_date(first) if first else None

    lines = ["🔢 <b>Сколько наборов привозить каждый день?</b>", ""]
    lines.append(await _dates_preview(dates))
    lines.append("")
    lines.append(f"💰 {fmt_money(price)} за сет")
    if tiers:
        lines.append("")
        lines.append("🎁 <b>Чем больше наборов в день, тем дешевле каждый:</b>")
        lines += [f"от {t.qty} наборов — −{t.percent}%, "
                  f"{fmt_money(pricing.calc(price, t.qty, tiers).per_set)} за сет"
                  for t in sorted(tiers, key=lambda t: t.qty)]

    numbers = [Btn(text=_qty_label(n, tiers), data=f"g:qty:{n}") for n in range(low, high + 1)]
    kb = chunk(numbers, 5)
    kb.append([Btn(text="⬅️ Изменить даты", data="g:date_back"),
               Btn(text="🏠 В начало", data="g:menu")])
    photo = breakfast["photo_path"] if breakfast else ""
    await _respond(ev, ch, Out(text="\n".join(lines), kb=kb, photo=photo),
                   new_message=bool(photo))


async def _dates_preview(dates: list[str], qty: int = 0) -> str:
    """Строки «дата — сет — количество» для экранов заказа."""
    rows = []
    for iso in dates:
        day = parse_date(iso)
        if day is None:
            continue
        breakfast = await repo.set_for_date(day)
        title = breakfast["title"] if breakfast else "сет дня"
        tail = f" — {qty} шт." if qty else ""
        rows.append(f"{fmt_date(day, with_weekday=False)} — {esc(title)}{tail}")
    return "\n".join(rows)


def _qty_label(qty: int, tiers: list) -> str:
    percent = pricing.percent_for(qty, tiers)
    return f"{qty} · −{percent}%" if percent else str(qty)


async def _pick_qty(ev: Event, ch: Channel, qty: int) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_address(ev, ch)
        return
    low, high = availability.qty_limits(obj)
    if not low <= qty <= high:
        await ch.send(ev.chat_id, Out(text=f"⚠️ Доступно от {low} до {high} наборов."))
        await _ask_qty(ev, ch)
        return
    data["qty"] = qty
    await _set_state(ev, S_APARTMENT, data)
    await _ask_apartment(ev, ch)


# ------------------------------------------------------- апартаменты и связь
async def _ask_apartment(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    await _set_state(ev, S_APARTMENT, data)
    user = await repo.get_user(ev.channel, ev.user_id)
    text = await repo.render_text("ask_apartment")
    kb: list[list[Btn]] = []
    if user and user["apartment"]:
        kb.append([Btn(text=f"🚪 Снова {user['apartment']}", data="g:reapt", intent="positive")])
    kb.append([Btn(text=BACK_LABEL, data="g:qty"), Btn(text="🏠 В начало", data="g:menu")])
    await _respond(ev, ch, Out(text=text, kb=kb))


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
        kb.append([Btn(text=f"📱 {fmt_phone(user['phone'])}", data="g:rephone",
                       intent="positive")])
    kb.append([Btn(text=BACK_LABEL, data="g:apt")])
    await _respond(ev, ch, Out(text=text, kb=kb, reply_contact=CONTACT_LABEL), new_message=True)


async def _reuse_phone(ev: Event, ch: Channel) -> None:
    user = await repo.get_user(ev.channel, ev.user_id)
    _, data = await _session_object(ev)
    if not (user and user["phone"]):
        await _ask_phone(ev, ch)
        return
    await _input_phone(ev, ch, user["phone"], data, user["customer_name"])


# ------------------------------------------------- аллергии и пожелания
async def _ask_allergy(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    if not await repo.get_bool("allergies_enabled", True):
        data["allergies"] = ""
        await _set_state(ev, S_COMMENT, data)
        await _ask_comment(ev, ch)
        return
    await _set_state(ev, S_ALLERGY, data)
    kb = [[Btn(text=SKIP_LABEL, data="g:skipa")],
          [Btn(text=BACK_LABEL, data="g:phone"), Btn(text="🏠 В начало", data="g:menu")]]
    await _respond(ev, ch, Out(text=await repo.render_text("ask_allergies"), kb=kb,), new_message=True)


async def _skip_allergy(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    data["allergies"] = ""
    await _set_state(ev, S_COMMENT, data)
    await _ask_comment(ev, ch)


async def _ask_comment(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    if not await repo.get_bool("comment_enabled", True):
        data["comment"] = ""
        await _set_state(ev, S_CONFIRM, data)
        await _show_confirm(ev, ch)
        return
    await _set_state(ev, S_COMMENT, data)
    kb = [[Btn(text=SKIP_LABEL, data="g:skip")],
          [Btn(text=BACK_LABEL, data="g:allergy"), Btn(text="🏠 В начало", data="g:menu")]]
    await _respond(ev, ch, Out(text=await repo.render_text("ask_comment"), kb=kb,), new_message=True)


async def _skip_comment(ev: Event, ch: Channel) -> None:
    _, data = await _session_object(ev)
    data["comment"] = ""
    await _set_state(ev, S_CONFIRM, data)
    await _show_confirm(ev, ch)


# ---------------------------------------------------------------- проверка
async def _order_lines(obj: Row, data: dict[str, Any]) -> tuple[list[str], int, int]:
    """Разбивка по датам, всего наборов и итоговая сумма."""
    qty = int(data.get("qty", 1))
    tiers = await pricing.tiers()
    custom = await repo.custom_address_price()
    rows: list[str] = []
    total = 0
    sets = 0
    for iso in data.get("dates", []):
        day = parse_date(iso)
        if day is None:
            continue
        breakfast = await repo.set_for_date(day)
        base = availability.price_for(obj, breakfast, custom)
        price = pricing.calc(base, qty, tiers)
        total += price.total
        sets += qty
        line = (f"{fmt_date(day, with_weekday=False)} — "
                f"{esc(breakfast['title'] if breakfast else 'сет дня')} — {qty} шт.")
        if price.percent:
            line += f"  (−{price.percent}%)"
        rows.append(line)
    return rows, sets, total


async def _show_confirm(ev: Event, ch: Channel) -> None:
    obj, data = await _session_object(ev)
    if obj is None:
        await _ask_object(ev, ch)
        return
    if not (data.get("dates") and data.get("qty") and data.get("apartment")
            and data.get("phone")):
        await _resume_incomplete(ev, ch, data)
        return
    await _set_state(ev, S_CONFIRM, data)

    rows, sets, total = await _order_lines(obj, data)
    address = data.get("address") or obj["address"] or obj["title"]
    deadline = await repo.get_setting("cancel_deadline", "18:30")

    lines = ["🧾 <b>Проверьте заказ</b>", ""]
    lines += rows
    lines += [
        "",
        f"Итого: {sets} {plural(sets, 'завтрак', 'завтрака', 'завтраков')} — "
        f"<b>{fmt_money(total)}</b>",
        "",
        f"📍 {esc(address)}",
        f"🚪 Апартаменты {esc(data.get('apartment', ''))}",
        f"📞 {esc(fmt_phone(data.get('phone', '')))}",
    ]
    if data.get("allergies"):
        lines.append(f"⚠️ Аллергии: {esc(data['allergies'])}")
    if data.get("comment"):
        lines.append(f"💬 {esc(data['comment'])}")
    lines += [
        "",
        f"🕘 Привезём {esc(repo.delivery_window(obj))}",
        f"<i>Отменить или изменить заказ можно до {esc(deadline)} "
        "предыдущего дня доставки. Полные условия — по кнопке ниже.</i>",
    ]

    kb = [
        [Btn(text="✅ Подтвердить заказ", data="g:confirm", intent="positive")],
        [Btn(text="✏️ Изменить", data="g:edit"),
         Btn(text="📋 Условия", data="g:rules")],
        [Btn(text="❌ Отменить", data="g:menu", intent="negative")],
    ]
    await _respond(ev, ch, Out(text="\n".join(lines), kb=kb))


async def _show_edit(ev: Event, ch: Channel) -> None:
    kb = [
        [Btn(text="📅 Дни доставки", data="g:date_back")],
        [Btn(text="🔢 Количество наборов", data="g:qty")],
        [Btn(text="🚪 Номер апартаментов", data="g:apt")],
        [Btn(text="📞 Телефон", data="g:phone")],
        [Btn(text="⚠️ Аллергии", data="g:allergy")],
        [Btn(text="⬅️ Назад к заказу", data="g:back")],
    ]
    await _respond(ev, ch, Out(text="✏️ <b>Что поправить?</b>", kb=kb))


async def _resume_incomplete(ev: Event, ch: Channel, data: dict[str, Any]) -> None:
    if not data.get("dates"):
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
        await _ask_address(ev, ch)
        return
    if not (data.get("dates") and data.get("qty") and data.get("apartment")
            and data.get("phone")):
        await ch.send(ev.chat_id, Out(text="⚠️ Заказ заполнен не полностью, начнём заново."))
        await _start_order(ev, ch)
        return

    qty = int(data["qty"])
    tiers = await pricing.tiers()
    custom = await repo.custom_address_price()
    days: list[dict[str, Any]] = []
    skipped: list[str] = []
    for iso in data["dates"]:
        day = parse_date(iso)
        if day is None:
            continue
        ok, reason = await availability.check_date(obj, day)
        if not ok:
            skipped.append(f"{fmt_date(day, False)} — {reason}")
            continue
        breakfast = await repo.set_for_date(day)
        base = availability.price_for(obj, breakfast, custom)
        price = pricing.calc(base, qty, tiers)
        days.append({
            "delivery_date": fmt_date_iso(day),
            "set_id": breakfast["id"] if breakfast else None,
            "set_title": breakfast["title"] if breakfast else "",
            "qty": qty,
            "base_price_kop": price.base_per_set,
            "discount_pct": price.percent,
            "price_kop": price.per_set,
            "total_kop": price.total,
        })

    if not days:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Выбранные даты уже недоступны — выберите другие."))
        await _ask_date(ev, ch)
        return
    if skipped:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Не получилось принять часть дат:\n" + "\n".join(skipped)))

    user = await repo.get_user(ev.channel, ev.user_id)
    name = str(data.get("name") or (user["customer_name"] if user else "")
               or (user["full_name"] if user else "") or ev.full_name)
    address = data.get("address") or obj["address"]

    orders = await repo.create_order_group(
        days,
        user_pk=user["id"] if user else None,
        channel=ev.channel,
        ext_id=str(ev.user_id),
        chat_id=str(ev.chat_id),
        object_id=obj["id"],
        object_title=obj["title"],
        object_address=address,
        apartment=str(data["apartment"]),
        phone=str(data["phone"]),
        customer_name=name,
        allergies=str(data.get("allergies", "")),
        comment=str(data.get("comment", "")),
        address_ok=1 if data.get("address_ok", True) else 0,
        status=statuses.NEW,
        source_code=(user["source_code"] if user else "") or obj["code"],
    )

    await repo.update_user(ev.channel, ev.user_id, phone=data["phone"],
                           apartment=str(data["apartment"]), customer_name=name,
                           object_id=obj["id"])
    await repo.clear_session(ev.channel, ev.user_id)

    text = await notify.group_status_text(orders, "order_accepted")
    await ch.send(ev.chat_id, Out(text=text, kb=[
        [Btn(text="📦 Мои заказы", data="g:my")],
        [Btn(text="📋 Условия заказа", data="g:rules"), _manager_btn()],
    ]))
    await notify.notify_new_order(orders)
    await _send_upsell(ev, ch)


async def _send_upsell(ev: Event, ch: Channel) -> None:
    offers = await repo.list_offers(active_only=True)
    if not offers:
        return
    kb = [[Btn(text=offer["title"], data=f"g:offer:{offer['id']}")] for offer in offers]
    kb.append([Btn(text="🏠 В начало", data="g:menu")])
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
    if text == SKIP_LABEL:
        if state == S_ALLERGY:
            await _skip_allergy(ev, ch)
            return
        if state == S_COMMENT:
            await _skip_comment(ev, ch)
            return

    if state == S_ADDRESS:
        await _input_address(ev, ch, text, data)
        return
    if state == S_REQUEST:
        await _input_request(ev, ch, text, data)
        return
    if state == S_RECEIPT:
        await _input_receipt(ev, ch, data)
        return
    if state == S_REVIEW:
        await _input_review(ev, ch, text, data)
        return
    if state == S_APARTMENT:
        await _input_apartment(ev, ch, text, data)
        return
    if state == S_PHONE:
        phone = ev.phone if ev.kind == "contact" else text
        await _input_phone(ev, ch, phone, data, str(ev.raw.get("contact_name", "")))
        return
    if state == S_ALLERGY:
        await _input_allergy(ev, ch, text, data)
        return
    if state == S_COMMENT:
        await _input_comment(ev, ch, text, data)
        return

    await _show_main_menu(ev, ch, new_message=True)


async def _back_from(ev: Event, ch: Channel, state: str) -> None:
    if state == S_PHONE:
        await _ask_apartment(ev, ch)
    elif state == S_ALLERGY:
        await _ask_phone(ev, ch)
    elif state == S_COMMENT:
        await _ask_allergy(ev, ch)
    elif state == S_APARTMENT:
        await _ask_qty(ev, ch)
    else:
        await _show_main_menu(ev, ch, new_message=True)


async def _input_apartment(ev: Event, ch: Channel, text: str, data: dict[str, Any]) -> None:
    value = text.strip()
    if not value or len(value) > 12:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Укажите номер апартаментов — до 12 символов, "
                 "например <b>45</b> или <b>12А</b>."))
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
        data["name"] = name.strip()
    await _set_state(ev, S_ALLERGY, data)
    # сообщение без кнопок под текстом — заодно возвращает постоянные кнопки
    # под полем ввода вместо клавиатуры «Поделиться контактом»
    await ch.send(ev.chat_id, Out(text=f"📱 Телефон записан: <b>{esc(fmt_phone(phone))}</b>"))
    await _ask_allergy(ev, ch)


async def _input_allergy(ev: Event, ch: Channel, text: str, data: dict[str, Any]) -> None:
    data["allergies"] = text[:200]
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
        S_ADDRESS: _ask_address,
        S_DATE: _ask_date,
        S_QTY: _ask_qty,
        S_APARTMENT: _ask_apartment,
        S_PHONE: _ask_phone,
        S_ALLERGY: _ask_allergy,
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
