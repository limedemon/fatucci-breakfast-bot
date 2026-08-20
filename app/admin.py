"""Админ-панель внутри Telegram-бота.

Отдельный сайт не нужен: всё управление — кнопками в чате с ботом.
Команда /admin. Права — по списку ADMIN_IDS в .env.
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import timedelta
from typing import Any, Callable, Optional

import aiosqlite

from . import (admins, courier, guide, notify, orders_service, payments, pricing,
               qrgen, repo, statuses)
from .channels.base import MAX, TG, Btn, Channel, Event, Out
from .config import cfg
from .utils import (
    WEEKDAYS_FULL,
    WEEKDAYS_SHORT,
    chunk,
    esc,
    fmt_date,
    fmt_date_iso,
    fmt_days,
    fmt_dt,
    fmt_money,
    fmt_phone,
    parse_date,
    parse_money,
    slug_code,
    today,
)

log = logging.getLogger(__name__)
Row = aiosqlite.Row

PAGE = 6


def mask_secret(value: str) -> str:
    """Показать, что ключ задан, не раскрывая его целиком."""
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 14 else "•" * len(value)


# =========================================================== описания полей
FieldSpec = tuple[str, str, str]  # (ключ, подпись, тип)

OBJECT_FIELDS: list[FieldSpec] = [
    ("title", "Название", "text"),
    ("group_title", "Сеть / группа", "text"),
    ("address", "Адрес доставки", "text"),
    ("price_kop", "Цена завтрака", "money"),
    ("delivery_days", "Дни доставки", "days"),
    ("cutoff_time", "Приём заказов до", "time"),
    ("lead_days", "Минимум дней до доставки", "int"),
    ("max_days_ahead", "На сколько дней вперёд", "int"),
    ("min_qty", "Мин. наборов", "int"),
    ("max_qty", "Макс. наборов", "int"),
    ("code", "Код для QR", "code"),
    ("note", "Заметка", "text"),
]

SET_FIELDS: list[FieldSpec] = [
    ("title", "Название", "text"),
    ("description", "Состав / описание", "text"),
    ("price_kop", "Своя цена (пусто — цена объекта)", "money_opt"),
    ("photo_path", "Фотография", "photo"),
    ("sort_order", "Порядок в списке", "int"),
]

OFFER_FIELDS: list[FieldSpec] = [
    ("title", "Название", "text"),
    ("description", "Описание", "text"),
    ("url", "Ссылка", "text"),
    ("button_text", "Подпись кнопки", "text"),
    ("photo_path", "Фотография", "photo"),
    ("sort_order", "Порядок в списке", "int"),
]

SETTING_SECTIONS: dict[str, tuple[str, list[FieldSpec]]] = {
    "gen": ("⚙️ Общие", [
        ("orders_chat_id", "Чат для заказов (ID)", "text"),
        ("manager_contact", "Контакт менеджера", "text"),
        ("manager_phone", "Телефон менеджера", "text"),
        ("order_prefix", "Префикс номера заказа", "text"),
        ("comment_enabled", "Спрашивать комментарий", "bool"),
        ("orders_paused", "Пауза приёма заказов", "bool"),
    ]),
    "ch": ("📱 Каналы", [
        ("max_enabled", "Бот в MAX включён", "bool"),
        ("max_token", "Токен бота MAX", "secret"),
        ("max_username", "Username бота MAX (без @)", "text"),
    ]),
    "rem": ("🔔 Напоминания", [
        ("reminder_enabled", "Напоминать о брошенном заказе", "bool"),
        ("reminder_after_min", "Через сколько минут", "int"),
        ("reminder_max_hours", "Не старше скольких часов", "int"),
    ]),
    "price": ("💰 Скидки за количество", [
        ("discount_tiers", "Пороги скидок", "text"),
    ]),
    "cur": ("🚚 Выгрузка курьерам", [
        ("courier_enabled", "Автовыгрузка включена", "bool"),
        ("courier_time", "Время формирования", "time"),
        ("courier_day_offset", "На какой день (1 = завтра)", "int"),
        ("courier_statuses", "Статусы в выгрузке", "text"),
        ("courier_from", "Откуда забирать заказ", "text"),
        ("courier_pickup", "Во сколько забирать", "time"),
        ("courier_note", "Комментарий курьеру", "text"),
    ]),
    "tpl": ("📋 Шаблон выгрузки", [
        ("courier_header", "Шапка", "text"),
        ("courier_object", "Блок объекта", "text"),
        ("courier_line", "Строка заказа", "text"),
        ("courier_footer", "Подвал", "text"),
        ("courier_empty", "Если заказов нет", "text"),
    ]),
    "pay": ("💳 Оплата", [
        ("pay_enabled", "Онлайн-оплата включена", "bool"),
        ("pm_token", "Токен PayMaster от @BotFather", "secret"),
        ("pm_provider_data", "Доп. данные провайдера (JSON)", "text"),
    ]),
}

TEXT_TITLES: dict[str, str] = {
    "welcome": "Приветствие (без объекта)",
    "welcome_object": "Приветствие (объект известен)",
    "menu_intro": "Заголовок раздела «Сеты»",
    "delivery_info": "Доставка",
    "how_to_order": "Как заказать",
    "faq": "Частые вопросы",
    "payment_info": "Об оплате",
    "payment_unavailable": "Оплата недоступна",
    "order_accepted": "Заявка принята",
    "status_accepted": "Статус: принят в работу",
    "status_paid": "Статус: оплачен",
    "status_delivered": "Статус: доставлен",
    "status_received": "Статус: получен",
    "status_rejected": "Статус: отклонён",
    "status_cancelled": "Статус: отменён",
    "reminder": "Напоминание о заказе",
    "upsell_intro": "Блок доп. предложений",
    "blocked": "Гость в чёрном списке",
    "orders_paused": "Приём заказов на паузе",
    "no_dates": "Нет доступных дат",
    "choose_object": "Выбор объекта",
    "ask_apartment": "Запрос номера апартаментов",
    "ask_phone": "Запрос телефона",
    "ask_comment": "Запрос комментария",
}


# ================================================================ вход/выход
async def handle_text(ev: Event, ch: Channel) -> bool:
    """Текст от админа. True — событие обработано админкой."""
    text = (ev.text or "").strip()
    admin_id = int(ev.user_id)

    if text == "/admin":
        await repo.clear_admin_state(admin_id)
        await _home(ev, ch, new_message=True)
        return True

    state, ctx = await repo.get_admin_state(admin_id)
    if not state:
        return False

    if text == "/cancel":
        await repo.clear_admin_state(admin_id)
        await _home(ev, ch, new_message=True)
        return True

    await _consume_input(ev, ch, state, ctx)
    return True


async def handle_callback(ev: Event, ch: Channel) -> None:
    data = ev.payload or ""
    parts = data.split(":")
    section = parts[1] if len(parts) > 1 else "h"
    args = parts[2:]

    # нажали кнопку — значит, предыдущий запрос ввода отменён
    state, _ = await repo.get_admin_state(int(ev.user_id))
    if state:
        await repo.clear_admin_state(int(ev.user_id))

    routes: dict[str, Callable[[], Any]] = {
        "h": lambda: _home(ev, ch),
        "hp": lambda: _guide(ev, ch, args),
        "g": lambda: _group(ev, ch, args),
        "o": lambda: _orders_route(ev, ch, args),
        "ord": lambda: _order_action(ev, ch, args),
        "m": lambda: _sets_route(ev, ch, args),
        "r": lambda: _rotation_route(ev, ch, args),
        "b": lambda: _objects_route(ev, ch, args),
        "q": lambda: _qr_route(ev, ch, args),
        "cur": lambda: _courier_route(ev, ch, args),
        "t": lambda: _texts_route(ev, ch, args),
        "f": lambda: _offers_route(ev, ch, args),
        "u": lambda: _users_route(ev, ch, args),
        "acc": lambda: _access_route(ev, ch, args),
        "bc": lambda: _broadcast_route(ev, ch, args),
        "cfg": lambda: _settings_route(ev, ch, args),
        "st": lambda: _stats_route(ev, ch, args),
        "x": lambda: _export_route(ev, ch, args),
        "nop": lambda: _noop(ev, ch),
    }
    handler = routes.get(section, lambda: _home(ev, ch))
    try:
        await handler()
    except Exception:  # noqa: BLE001
        log.exception("Ошибка админ-панели: %s", data)
        await ch.send(ev.chat_id, Out(text="⚠️ Ошибка в админ-панели, смотрите логи."))
    finally:
        await _answer(ev, ch)


async def _answer(ev: Event, ch: Channel, text: str = "") -> None:
    if ev.kind == "callback" and ev.callback_id and not ev.raw.get("_answered"):
        ev.raw["_answered"] = True
        await ch.answer_callback(ev.callback_id, text)


async def _noop(ev: Event, ch: Channel) -> None:
    await _answer(ev, ch)


async def _show(ev: Event, ch: Channel, out: Out, new_message: bool = False) -> None:
    if not new_message and ev.kind == "callback" and not out.photo and not ev.raw.get("_answered"):
        await ch.reply_to_callback(ev, out)
        return
    await _answer(ev, ch)
    await ch.send(ev.chat_id, out)


def _back(target: str, title: str = "⬅️ Назад") -> list[Btn]:
    if target == "a:h":                     # не дублируем одну и ту же кнопку
        return [Btn(text="🏠 В админку", data="a:h")]
    return [Btn(text=title, data=target), Btn(text="🏠 Админка", data="a:h")]


def _help(topic: str) -> Btn:
    """Кнопка «как это работает» рядом с разделом."""
    return Btn(text="❓ Как это работает", data=f"a:hp:{topic}")


# ==================================================================== справка
async def _guide(ev: Event, ch: Channel, args: list[str]) -> None:
    topic = args[0] if args else ""
    if not topic:
        kb = [[Btn(text=guide.title(key), data=f"a:hp:{key}")] for key in guide.ORDER]
        kb.append(_back("a:h", "⬅️ В админку"))
        await _show(ev, ch, Out(
            text="❓ <b>Справка по админ-панели</b>\n\n"
                 "Короткие пояснения к каждому разделу: что он делает, "
                 "как им пользоваться и на что обратить внимание.\n\n"
                 "Начните с «🚀 С чего начать» — там пошаговая настройка бота.",
            kb=kb))
        return
    kb = [[Btn(text="📖 Все разделы справки", data="a:hp")], _back("a:h", "⬅️ В админку")]
    await _show(ev, ch, Out(text=guide.body(topic), kb=kb))


# ============================================================ группы разделов
GROUPS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "menu": ("🥐 Меню и цены", "Что и почём предлагаем гостям.", [
        ("🥐 Сеты (завтраки)", "a:m:l"),
        ("🗓 Ротация по дням", "a:r:l"),
        ("💰 Скидки за количество", "a:cfg:s:price"),
        ("🍽 Доп. предложения", "a:f:l"),
    ]),
    "obj": ("🏢 Объекты и QR", "Дома, адреса, цены и коды для тейбл-тентов.", [
        ("🏢 Объекты (дома)", "a:b:l"),
        ("🔗 QR-коды", "a:q:l"),
    ]),
    "rep": ("📊 Отчёты", "Как идут дела и выгрузка для бухгалтерии.", [
        ("📈 Статистика", "a:st:m"),
        ("📤 Выгрузка в CSV", "a:x:m"),
        ("👥 Гости и чёрный список", "a:u:l:0"),
    ]),
}

GROUP_HELP = {"menu": "prices", "obj": "objects", "rep": "reports"}


async def _group(ev: Event, ch: Channel, args: list[str]) -> None:
    code = args[0] if args else ""
    group = GROUPS.get(code)
    if group is None:
        await _home(ev, ch)
        return
    title, subtitle, items = group
    kb = [[Btn(text=label, data=target)] for label, target in items]
    kb.append([_help(GROUP_HELP.get(code, "start"))])
    kb.append(_back("a:h", "⬅️ В админку"))
    await _show(ev, ch, Out(text=f"<b>{title}</b>\n\n{subtitle}", kb=kb))


# ==================================================================== главная
async def _home(ev: Event, ch: Channel, new_message: bool = False) -> None:
    new_count = await repo.count_orders(status=statuses.NEW)
    work_count = await repo.count_orders(status=f"{statuses.ACCEPTED},{statuses.PAID}")
    tomorrow = fmt_date_iso(today() + timedelta(days=1))
    tomorrow_count = await repo.count_orders(date_from=tomorrow, date_to=tomorrow,
                                             status=f"{statuses.ACCEPTED},{statuses.PAID}")

    lines = ["🛠 <b>Админ-панель Fatucci</b>", ""]
    if new_count:
        lines.append(f"🆕 <b>Новых заказов: {new_count}</b> — ждут вашего решения")
    else:
        lines.append("🆕 Новых заказов нет")
    lines.append(f"🔧 В работе: {work_count}")
    lines.append(f"🚚 На завтра: {tomorrow_count}")

    warnings = []
    if await repo.get_bool("orders_paused"):
        warnings.append("⏸ Приём заказов на паузе")
    if not await payments.is_configured():
        warnings.append("💳 Оплата не подключена")
    if not (await repo.get_setting("orders_chat_id") or cfg.orders_chat_id):
        warnings.append("💬 Не задан рабочий чат для заказов")
    if warnings:
        lines += ["", *warnings]

    kb = [
        [Btn(text=f"📦 Заказы{f' · {new_count} новых' if new_count else ''}",
             data="a:o:l:new:0", intent="positive" if new_count else "")],
        [Btn(text="🥐 Меню и цены", data="a:g:menu"),
         Btn(text="🏢 Объекты и QR", data="a:g:obj")],
        [Btn(text="🚚 Курьерам", data="a:cur:m"),
         Btn(text="📊 Отчёты", data="a:g:rep")],
        [Btn(text="⚙️ Настройки", data="a:cfg:m"),
         Btn(text="📢 Рассылка", data="a:bc:m")],
        [Btn(text="❓ Справка — что где находится", data="a:hp")],
    ]
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb), new_message=new_message)


# ==================================================================== заказы
ORDER_FILTERS: dict[str, tuple[str, dict[str, Any]]] = {
    "new": ("🆕 Новые", {"status": statuses.NEW}),
    "work": ("🔧 В работе", {"status": f"{statuses.ACCEPTED},{statuses.PAID}"}),
    "today": ("📅 Доставка сегодня", {}),
    "tmrw": ("📅 Доставка завтра", {}),
    "all": ("📋 Все заказы", {}),
}


def _filter_params(code: str) -> dict[str, Any]:
    if code == "today":
        iso = fmt_date_iso(today())
        return {"date_from": iso, "date_to": iso}
    if code == "tmrw":
        iso = fmt_date_iso(today() + timedelta(days=1))
        return {"date_from": iso, "date_to": iso}
    if code.startswith("ob_"):
        return {"object_id": int(code[3:] or 0)}
    if code.startswith("st_"):
        return {"status": code[3:]}
    if code.startswith("dt_"):
        return {"date_from": code[3:], "date_to": code[3:]}
    return ORDER_FILTERS.get(code, ("", {}))[1]


async def _orders_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        await _orders_list(ev, ch, args[1] if len(args) > 1 else "new",
                           int(args[2]) if len(args) > 2 else 0)
    elif action == "c":
        await _order_card(ev, ch, int(args[1]))
    elif action == "f":
        await _orders_filters(ev, ch)
    elif action == "fo":
        await _orders_by_object(ev, ch)
    elif action == "find":
        await _ask(ev, ch, "find_order", {},
                   "🔎 Пришлите номер заказа (например <code>F-00012</code>) "
                   "или номер апартаментов.")
    elif action == "fd":
        await _ask(ev, ch, "find_date", {},
                   "📅 Пришлите дату доставки в формате <code>ДД.ММ.ГГГГ</code>.")
    else:
        await _orders_list(ev, ch, "new", 0)


async def _orders_list(ev: Event, ch: Channel, code: str, page: int) -> None:
    params = _filter_params(code)
    total = await repo.count_orders(**params)
    orders = await repo.list_orders(**params, limit=PAGE, offset=page * PAGE)
    title = ORDER_FILTERS.get(code, ("Заказы", {}))[0]
    if code.startswith("ob_"):
        obj = await repo.get_object(int(code[3:] or 0))
        title = f"🏢 {obj['title']}" if obj else "Заказы"
    elif code.startswith("st_"):
        title = statuses.label(code[3:])
    elif code.startswith("dt_"):
        title = f"📅 {code[3:]}"

    lines = [f"<b>{esc(title)}</b> — найдено: {total}", ""]
    kb: list[list[Btn]] = []
    if not orders:
        lines.append("Пусто.")
    for order in orders:
        lines.append(
            f"№{esc(order['number'])} · {fmt_date(order['delivery_date'], False)} · "
            f"{esc(order['object_title'])}, кв. {esc(order['apartment'])}\n"
            f"   {statuses.label(order['status'])} · {order['qty']} шт · "
            f"{fmt_money(order['total_kop'])}"
        )
        kb.append([Btn(text=f"№{order['number']} · {statuses.label(order['status'])}",
                       data=f"a:o:c:{order['id']}")])

    nav: list[Btn] = []
    if page > 0:
        nav.append(Btn(text="⬅️", data=f"a:o:l:{code}:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(Btn(text="➡️", data=f"a:o:l:{code}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([Btn(text="🔎 Фильтры", data="a:o:f"), Btn(text="🔢 Найти", data="a:o:find")])
    kb.append([_help("orders")])
    kb.append(_back("a:h", "⬅️ В админку"))
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


async def _orders_filters(ev: Event, ch: Channel) -> None:
    kb = [[Btn(text=title, data=f"a:o:l:{code}:0")] for code, (title, _) in ORDER_FILTERS.items()]
    status_buttons = [Btn(text=statuses.label(code), data=f"a:o:l:st_{code}:0")
                      for code in statuses.ORDER]
    kb += chunk(status_buttons, 2)
    kb.append([Btn(text="🏢 По объекту", data="a:o:fo"), Btn(text="📅 По дате", data="a:o:fd")])
    kb.append(_back("a:o:l:new:0"))
    await _show(ev, ch, Out(text="🔎 <b>Фильтры заказов</b>", kb=kb))


async def _orders_by_object(ev: Event, ch: Channel) -> None:
    objects = await repo.list_objects()
    kb = [[Btn(text=obj["title"], data=f"a:o:l:ob_{obj['id']}:0")] for obj in objects]
    kb.append(_back("a:o:f"))
    await _show(ev, ch, Out(text="🏢 <b>Выберите объект</b>", kb=kb))


async def _order_card(ev: Event, ch: Channel, order_id: int) -> None:
    order = await repo.get_order(order_id)
    if order is None:
        await _orders_list(ev, ch, "new", 0)
        return
    text = await notify.order_card(order)
    events = await repo.order_events(order_id)
    if events:
        text += "\n\n<b>История:</b>\n" + "\n".join(
            f"• {fmt_dt(e['at'])} — {statuses.label(e['status'])}"
            + (f" ({esc(e['actor'])})" if e["actor"] else "")
            for e in events[-6:]
        )
    kb: list[list[Btn]] = []
    actions = statuses.next_actions(order["status"])
    if actions:
        kb.append([Btn(text=title, data=f"a:ord:{order_id}:{code}") for code, title in actions])
    if order["payment_url"]:
        kb.append([Btn(text="🔗 Ссылка на оплату", url=order["payment_url"])])
    username = await notify.guest_username(order)
    link = notify.guest_link(order["channel"], username)
    if link:
        kb.append([Btn(text="✉️ Написать гостю", url=link)])
    kb.append([Btn(text="🔄 Обновить", data=f"a:o:c:{order_id}")])
    kb.append(_back("a:o:l:new:0", "⬅️ К заказам"))
    await _show(ev, ch, Out(text=text, kb=kb))


async def _order_action(ev: Event, ch: Channel, args: list[str]) -> None:
    """Кнопки под карточкой заказа в рабочем чате: a:ord:<id>:<action>."""
    if len(args) < 2:
        return
    order_id, action = int(args[0]), args[1]

    if action == "refresh":
        order = await repo.get_order(order_id)
        if order:
            await notify.refresh_order_cards(order)
        await _answer(ev, ch, "Обновлено")
        return

    if action == statuses.REJECTED:
        await _ask(ev, ch, "reject", {"order_id": order_id},
                   f"⛔ Причина отказа по заказу №{order_id}? "
                   "Напишите текст (его увидит гость) или отправьте <code>-</code>.")
        return

    ok, message = await orders_service.change_status(order_id, action, actor=_actor(ev))
    await _answer(ev, ch, message[:180])
    if not ok:
        return
    if ev.chat_id and not _is_orders_chat(ev):
        await _order_card(ev, ch, order_id)


def _actor(ev: Event) -> str:
    return f"@{ev.username}" if ev.username else f"admin:{ev.user_id}"


def _is_orders_chat(ev: Event) -> bool:
    return str(ev.chat_id) != str(ev.user_id)


# ================================================================ меню/сеты
async def _sets_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        await _sets_list(ev, ch)
    elif action == "c":
        await _set_card(ev, ch, int(args[1]))
    elif action == "e":
        await _edit_field(ev, ch, "set", int(args[1]), args[2])
    elif action == "t":
        item = await repo.get_set(int(args[1]))
        if item:
            await repo.update_set(item["id"], is_active=0 if item["is_active"] else 1)
        await _set_card(ev, ch, int(args[1]))
    elif action == "n":
        await _ask(ev, ch, "set_new", {}, "🥐 Название нового сета?")
    elif action == "d":
        await _confirm_delete(ev, ch, "set", int(args[1]),
                              "Удалить сет? Он исчезнет из меню и ротации.")
    elif action == "dd":
        await repo.delete_set(int(args[1]))
        await _sets_list(ev, ch)


async def _sets_list(ev: Event, ch: Channel) -> None:
    items = await repo.list_sets()
    lines = ["🥐 <b>Меню завтраков</b>", ""]
    kb: list[list[Btn]] = []
    for item in items:
        mark = "✅" if item["is_active"] else "🚫"
        price = fmt_money(item["price_kop"]) if item["price_kop"] else "цена объекта"
        photo = "🖼" if item["photo_path"] else "—"
        lines.append(f"{mark} <b>{esc(item['title'])}</b> · {price} · фото: {photo}")
        kb.append([Btn(text=f"{mark} {item['title']}", data=f"a:m:c:{item['id']}")])
    if not items:
        lines.append("Пока пусто.")
    kb.append([Btn(text="➕ Добавить сет", data="a:m:n")])
    kb.append([_help("sets")])
    kb.append(_back("a:h", "⬅️ В админку"))
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


async def _set_card(ev: Event, ch: Channel, set_id: int) -> None:
    item = await repo.get_set(set_id)
    if item is None:
        await _sets_list(ev, ch)
        return
    price = fmt_money(item["price_kop"]) if item["price_kop"] else "по цене объекта"
    text = (
        f"🥐 <b>{esc(item['title'])}</b>\n\n"
        f"{esc(item['description']) or '<i>описание не задано</i>'}\n\n"
        f"💰 Цена: {price}\n"
        f"🖼 Фото: {'загружено' if item['photo_path'] else 'нет'}\n"
        f"Статус: {'✅ показывается гостям' if item['is_active'] else '🚫 скрыт'}"
    )
    kb = [[Btn(text=f"✏️ {label}", data=f"a:m:e:{set_id}:{key}")]
          for key, label, _ in SET_FIELDS]
    kb.append([Btn(text="🚫 Скрыть" if item["is_active"] else "✅ Показать",
                   data=f"a:m:t:{set_id}"),
               Btn(text="🗑 Удалить", data=f"a:m:d:{set_id}", intent="negative")])
    kb.append(_back("a:m:l", "⬅️ К меню"))
    await _show(ev, ch, Out(text=text, kb=kb, photo=item["photo_path"]),
                new_message=bool(item["photo_path"]))


# =================================================================== ротация
async def _rotation_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        await _rotation_week(ev, ch)
    elif action == "w":
        await _rotation_pick(ev, ch, int(args[1]))
    elif action == "ws":
        weekday, set_id = int(args[1]), int(args[2])
        await repo.set_rotation_week(weekday, set_id or None)
        await _rotation_week(ev, ch)
    elif action == "d":
        await _rotation_dates(ev, ch)
    elif action == "dn":
        await _ask(ev, ch, "rot_date", {},
                   "📅 На какую дату задать особый сет?\nФормат: <code>ДД.ММ.ГГГГ</code>")
    elif action == "ds":
        await repo.set_rotation_date(args[1], int(args[2]) or None)
        await _rotation_dates(ev, ch)
    elif action == "dx":
        await repo.del_rotation_date(args[1])
        await _rotation_dates(ev, ch)


async def _rotation_week(ev: Event, ch: Channel) -> None:
    week = await repo.rotation_week()
    items = {item["id"]: item for item in await repo.list_sets()}
    lines = ["🗓 <b>Ротация по дням недели</b>", "",
             "Бот сам подставит нужный сет, когда гость выберет дату.", ""]
    kb: list[list[Btn]] = []
    for weekday in range(1, 8):
        item = items.get(week.get(weekday) or 0)
        title = item["title"] if item else "— не задан —"
        lines.append(f"<b>{WEEKDAYS_FULL[weekday - 1].capitalize()}</b> — {esc(title)}")
        kb.append([Btn(text=f"{WEEKDAYS_SHORT[weekday - 1]}: {title}", data=f"a:r:w:{weekday}")])
    kb.append([Btn(text="📅 Особые даты", data="a:r:d")])
    kb.append([_help("rotation")])
    kb.append(_back("a:h", "⬅️ В админку"))
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


async def _rotation_pick(ev: Event, ch: Channel, weekday: int) -> None:
    items = await repo.list_sets(active_only=True)
    kb = [[Btn(text=item["title"], data=f"a:r:ws:{weekday}:{item['id']}")] for item in items]
    kb.append([Btn(text="🚫 Без завтрака", data=f"a:r:ws:{weekday}:0")])
    kb.append(_back("a:r:l"))
    await _show(ev, ch, Out(
        text=f"🗓 Какой сет в <b>{WEEKDAYS_FULL[weekday - 1]}</b>?", kb=kb))


async def _rotation_dates(ev: Event, ch: Channel) -> None:
    rows = await repo.rotation_dates(today())
    items = {item["id"]: item["title"] for item in await repo.list_sets()}
    lines = ["📅 <b>Особые даты</b>", "",
             "Переопределяют недельную ротацию на конкретный день.", ""]
    kb: list[list[Btn]] = []
    for row in rows:
        title = items.get(row["set_id"] or 0, "— без завтрака —")
        lines.append(f"{row['d']} — {esc(title)}")
        kb.append([Btn(text=f"🗑 {row['d']} · {title}", data=f"a:r:dx:{row['d']}")])
    if not rows:
        lines.append("Пока не задано.")
    kb.append([Btn(text="➕ Добавить дату", data="a:r:dn")])
    kb.append(_back("a:r:l"))
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


# =================================================================== объекты
async def _objects_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        await _objects_list(ev, ch)
    elif action == "c":
        await _object_card(ev, ch, int(args[1]))
    elif action == "e":
        await _edit_field(ev, ch, "obj", int(args[1]), args[2])
    elif action == "t":
        obj = await repo.get_object(int(args[1]))
        if obj:
            await repo.update_object(obj["id"], is_active=0 if obj["is_active"] else 1)
        await _object_card(ev, ch, int(args[1]))
    elif action == "n":
        await _ask(ev, ch, "obj_new", {},
                   "🏢 Название нового объекта?\nНапример: <code>Альфа-Апартаменты, Северная 12</code>")
    elif action == "d":
        await _confirm_delete(ev, ch, "obj", int(args[1]),
                              "Удалить объект? QR-код перестанет работать. "
                              "Заказы останутся в базе.")
    elif action == "dd":
        await repo.delete_object(int(args[1]))
        await _objects_list(ev, ch)


async def _objects_list(ev: Event, ch: Channel) -> None:
    objects = await repo.list_objects()
    lines = ["🏢 <b>Объекты</b>", ""]
    kb: list[list[Btn]] = []
    for obj in objects:
        mark = "✅" if obj["is_active"] else "🚫"
        kind = " · общий QR" if obj["is_general"] else ""
        lines.append(
            f"{mark} <b>{esc(obj['title'])}</b>{kind}\n"
            f"   {esc(obj['address']) or '—'} · {fmt_money(obj['price_kop'])} · "
            f"код <code>{esc(obj['code'])}</code>"
        )
        kb.append([Btn(text=f"{mark} {obj['title']}", data=f"a:b:c:{obj['id']}")])
    if not objects:
        lines.append("Пока пусто.")
    kb.append([Btn(text="➕ Добавить объект", data="a:b:n")])
    kb.append([_help("objects")])
    kb.append(_back("a:h", "⬅️ В админку"))
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


async def _object_card(ev: Event, ch: Channel, object_id: int) -> None:
    obj = await repo.get_object(object_id)
    if obj is None:
        await _objects_list(ev, ch)
        return
    orders_count = await repo.count_orders(object_id=object_id)
    text = (
        f"🏢 <b>{esc(obj['title'])}</b>\n"
        f"{'🌐 Общий QR — гость выбирает объект сам' if obj['is_general'] else ''}\n\n"
        f"Сеть: {esc(obj['group_title']) or '—'}\n"
        f"📍 Адрес: {esc(obj['address']) or '—'}\n"
        f"💰 Цена завтрака: <b>{fmt_money(obj['price_kop'])}</b>\n"
        f"🚚 Дни доставки: {fmt_days(obj['delivery_days'])}\n"
        f"⏰ Приём заказов до: {esc(obj['cutoff_time'])} (за {obj['lead_days']} дн.)\n"
        f"📅 Горизонт заказа: {obj['max_days_ahead']} дн.\n"
        f"🔢 Наборов: от {obj['min_qty']} до {obj['max_qty']}\n"
        f"🔗 Код QR: <code>{esc(obj['code'])}</code>\n"
        f"📦 Заказов всего: {orders_count}\n"
        f"Статус: {'✅ активен' if obj['is_active'] else '🚫 отключён'}"
    )
    if obj["note"]:
        text += f"\n📝 {esc(obj['note'])}"

    kb = chunk([Btn(text=f"✏️ {label}", data=f"a:b:e:{object_id}:{key}")
                for key, label, _ in OBJECT_FIELDS], 2)
    kb.append([Btn(text="🔗 QR-код", data=f"a:q:o:{object_id}"),
               Btn(text="📦 Заказы", data=f"a:o:l:ob_{object_id}:0")])
    kb.append([Btn(text="🚫 Отключить" if obj["is_active"] else "✅ Включить",
                   data=f"a:b:t:{object_id}"),
               Btn(text="🗑 Удалить", data=f"a:b:d:{object_id}", intent="negative")])
    kb.append(_back("a:b:l", "⬅️ К объектам"))
    await _show(ev, ch, Out(text=text, kb=kb))


# ==================================================================== QR-коды
async def _qr_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        objects = await repo.list_objects()
        kb = [[Btn(text=f"{'✅' if obj['is_active'] else '🚫'} {obj['title']}",
                   data=f"a:q:o:{obj['id']}")] for obj in objects]
        kb.append(_back("a:h", "⬅️ В админку"))
        await _show(ev, ch, Out(
            text="🔗 <b>QR-коды</b>\n\nВыберите объект — пришлю готовые PNG "
                 "для тейбл-тента и ссылки.", kb=kb))
    elif action == "o":
        await _qr_send(ev, ch, int(args[1]))


async def _qr_send(ev: Event, ch: Channel, object_id: int) -> None:
    obj = await repo.get_object(object_id)
    if obj is None:
        await _qr_route(ev, ch, ["l"])
        return
    await _answer(ev, ch, "Готовлю QR…")
    links = qrgen.object_links(obj)
    if not links:
        await ch.send(ev.chat_id, Out(
            text="⚠️ Не заданы username ботов. Укажите TELEGRAM_BOT_USERNAME "
                 "и MAX_BOT_USERNAME в .env — без них ссылки для QR не собрать."))
        return

    orders_count = await repo.count_orders(object_id=object_id)
    caption_lines = [
        f"🔗 <b>QR для «{esc(obj['title'])}»</b>",
        f"Код: <code>{esc(obj['code'])}</code>",
        f"Заказов с этого QR: {orders_count}",
        "",
    ]
    for name, link in links.items():
        caption_lines.append(f"{'Telegram' if name == TG else 'MAX'}: {esc(link)}")
    caption_lines += ["", "Гость сканирует код — бот сам подставит объект, адрес и цену."]
    await ch.send(ev.chat_id, Out(text="\n".join(caption_lines),
                                  kb=[_back(f"a:b:c:{object_id}", "⬅️ К объекту")]))

    for name, link in links.items():
        png = qrgen.make_qr(link)
        title = "Telegram" if name == TG else "MAX"
        await ch.send_bytes(ev.chat_id, png, f"qr_{obj['code']}_{name}.png",
                            caption=f"QR · {title} · {obj['title']}")


# =============================================================== курьеры
async def _courier_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "m"
    if action == "m":
        await _courier_menu(ev, ch)
    elif action == "make":
        day = parse_date(args[1]) if len(args) > 1 else await courier.target_day()
        await _answer(ev, ch, "Формирую…")
        await courier.send_digest(day or await courier.target_day(), auto=False)
    elif action == "copy":
        day = parse_date(args[1]) if len(args) > 1 else await courier.target_day()
        await _answer(ev, ch, "Готовлю текст…")
        await ch.send(ev.chat_id, Out(text=await courier.copy_version(day or today())))
    elif action == "day":
        await _ask(ev, ch, "digest_date", {},
                   "📅 На какую дату собрать выгрузку?\nФормат: <code>ДД.ММ.ГГГГ</code>")


async def _courier_menu(ev: Event, ch: Channel) -> None:
    day = await courier.target_day()
    enabled = await repo.get_bool("courier_enabled", True)
    time_str = await repo.get_setting("courier_time", "20:00")
    statuses_str = await repo.get_setting("courier_statuses")
    text = (
        "🚚 <b>Выгрузка для курьеров</b>\n\n"
        f"Автоматически: {'включена' if enabled else 'выключена'}\n"
        f"Время формирования: <b>{esc(time_str)}</b>\n"
        f"Собирается на: <b>{fmt_date(day)}</b>\n"
        f"Статусы в выгрузке: <code>{esc(statuses_str)}</code>\n\n"
        "Бот формирует одно готовое сообщение — его можно целиком скопировать "
        "и отправить курьерской службе."
    )
    kb = [
        [Btn(text=f"📋 Собрать на {day.strftime('%d.%m')}", data=f"a:cur:make:{fmt_date_iso(day)}")],
        [Btn(text="📅 Собрать на другую дату", data="a:cur:day")],
        [Btn(text="⚙️ Настройки выгрузки", data="a:cfg:s:cur"),
         Btn(text="📝 Шаблон", data="a:cfg:s:tpl")],
        _back("a:h", "⬅️ В админку"),
    ]
    await _show(ev, ch, Out(text=text, kb=kb))


# ===================================================================== тексты
async def _texts_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        rows = await repo.all_texts()
        kb = [[Btn(text=TEXT_TITLES.get(row["key"], row["key"]), data=f"a:t:e:{row['key']}")]
              for row in rows]
        kb.append([Btn(text="♻️ Вернуть стандартные тексты", data="a:t:reset")])
        kb.append(_back("a:h", "⬅️ В админку"))
        await _show(ev, ch, Out(
            text="✍️ <b>Тексты бота</b>\n\nВыберите текст, чтобы изменить. "
                 "Поддерживается HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
                 "<code>&lt;i&gt;курсив&lt;/i&gt;</code>.", kb=kb))
    elif action == "reset":
        await _show(ev, ch, Out(
            text="⚠️ <b>Вернуть стандартные тексты?</b>\n\n"
                 "Все ваши правки текстов будут заменены на исходные. "
                 "Меню, объекты, цены и заказы это не затронет.",
            kb=[[Btn(text="♻️ Да, вернуть", data="a:t:resetok", intent="negative")],
                [Btn(text="✖️ Отмена", data="a:t:l")]]))
    elif action == "resetok":
        from .defaults import DEFAULT_TEXTS

        for key, value in DEFAULT_TEXTS.items():
            await repo.set_text(key, value)
        await _answer(ev, ch, "Тексты возвращены")
        await _texts_route(ev, ch, ["l"])
    elif action == "e":
        key = args[1]
        value = await repo.get_text(key)
        await _ask(ev, ch, "text", {"key": key},
                   f"✍️ <b>{esc(TEXT_TITLES.get(key, key))}</b>\n\nСейчас:\n"
                   f"<code>{esc(value)}</code>\n\nПришлите новый текст.")


# ========================================================== доп. предложения
async def _offers_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        offers = await repo.list_offers()
        lines = ["🍽 <b>Дополнительные предложения</b>", "",
                 "Показываются гостю после оформления заказа.", ""]
        kb: list[list[Btn]] = []
        for offer in offers:
            mark = "✅" if offer["is_active"] else "🚫"
            lines.append(f"{mark} <b>{esc(offer['title'])}</b> — {esc(offer['url']) or 'без ссылки'}")
            kb.append([Btn(text=f"{mark} {offer['title']}", data=f"a:f:c:{offer['id']}")])
        if not offers:
            lines.append("Пока пусто.")
        kb.append([Btn(text="➕ Добавить", data="a:f:n")])
        kb.append(_back("a:h", "⬅️ В админку"))
        await _show(ev, ch, Out(text="\n".join(lines), kb=kb))
    elif action == "c":
        await _offer_card(ev, ch, int(args[1]))
    elif action == "e":
        await _edit_field(ev, ch, "offer", int(args[1]), args[2])
    elif action == "t":
        offer = await repo.get_offer(int(args[1]))
        if offer:
            await repo.update_offer(offer["id"], is_active=0 if offer["is_active"] else 1)
        await _offer_card(ev, ch, int(args[1]))
    elif action == "n":
        await _ask(ev, ch, "offer_new", {}, "🍽 Название нового предложения?")
    elif action == "d":
        await _confirm_delete(ev, ch, "offer", int(args[1]), "Удалить это предложение?")
    elif action == "dd":
        await repo.delete_offer(int(args[1]))
        await _offers_route(ev, ch, ["l"])


async def _offer_card(ev: Event, ch: Channel, offer_id: int) -> None:
    offer = await repo.get_offer(offer_id)
    if offer is None:
        await _offers_route(ev, ch, ["l"])
        return
    text = (
        f"🍽 <b>{esc(offer['title'])}</b>\n\n"
        f"{esc(offer['description']) or '<i>описание не задано</i>'}\n\n"
        f"🔗 {esc(offer['url']) or '—'}\n"
        f"🔘 Кнопка: {esc(offer['button_text'])}\n"
        f"🖼 Фото: {'загружено' if offer['photo_path'] else 'нет'}\n"
        f"Статус: {'✅ показывается' if offer['is_active'] else '🚫 скрыто'}"
    )
    kb = chunk([Btn(text=f"✏️ {label}", data=f"a:f:e:{offer_id}:{key}")
                for key, label, _ in OFFER_FIELDS], 2)
    kb.append([Btn(text="🚫 Скрыть" if offer["is_active"] else "✅ Показать",
                   data=f"a:f:t:{offer_id}"),
               Btn(text="🗑 Удалить", data=f"a:f:d:{offer_id}", intent="negative")])
    kb.append(_back("a:f:l"))
    await _show(ev, ch, Out(text=text, kb=kb, photo=offer["photo_path"]),
                new_message=bool(offer["photo_path"]))


# ====================================================================== гости
async def _users_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        page = int(args[1]) if len(args) > 1 else 0
        total = await repo.count_users()
        blocked = await repo.count_users(blocked=True)
        users = await repo.list_users(limit=PAGE, offset=page * PAGE)
        lines = [f"👥 <b>Гости</b> — всего {total}, в чёрном списке {blocked}", ""]
        kb: list[list[Btn]] = []
        for user in users:
            mark = "🚫" if user["is_blocked"] else "👤"
            name = user["full_name"] or user["username"] or user["ext_id"]
            lines.append(
                f"{mark} <b>{esc(name)}</b> · {user['channel']} · "
                f"{esc(fmt_phone(user['phone'])) or 'без телефона'}"
            )
            kb.append([Btn(text=f"{mark} {name}", data=f"a:u:c:{user['id']}")])
        nav: list[Btn] = []
        if page > 0:
            nav.append(Btn(text="⬅️", data=f"a:u:l:{page - 1}"))
        if (page + 1) * PAGE < total:
            nav.append(Btn(text="➡️", data=f"a:u:l:{page + 1}"))
        if nav:
            kb.append(nav)
        kb.append(_back("a:h", "⬅️ В админку"))
        await _show(ev, ch, Out(text="\n".join(lines), kb=kb))
    elif action == "c":
        await _user_card(ev, ch, int(args[1]))
    elif action == "b":
        user = await repo.get_user_pk(int(args[1]))
        if user:
            await repo.set_blocked(user["id"], not user["is_blocked"])
        await _user_card(ev, ch, int(args[1]))


async def _user_card(ev: Event, ch: Channel, user_pk: int) -> None:
    user = await repo.get_user_pk(user_pk)
    if user is None:
        await _users_route(ev, ch, ["l", "0"])
        return
    orders_count = await repo.count_orders(user_key=(user["channel"], user["ext_id"]))
    obj = await repo.get_object(user["object_id"])
    text = (
        f"👤 <b>{esc(user['full_name'] or user['username'] or user['ext_id'])}</b>\n\n"
        f"Канал: {user['channel']}\n"
        f"ID: <code>{esc(user['ext_id'])}</code>\n"
        f"Телефон: {esc(fmt_phone(user['phone'])) or '—'}\n"
        f"Апартаменты: {esc(user['apartment']) or '—'}\n"
        f"Объект: {esc(obj['title']) if obj else '—'}\n"
        f"QR: <code>{esc(user['source_code']) or '—'}</code>\n"
        f"Заказов: {orders_count}\n"
        f"Первый визит: {fmt_dt(user['created_at'])}\n"
        f"Статус: {'🚫 в чёрном списке' if user['is_blocked'] else '✅ обычный гость'}"
    )
    kb = [[Btn(text="✅ Разблокировать" if user["is_blocked"] else "🚫 В чёрный список",
               data=f"a:u:b:{user_pk}", intent="negative" if not user["is_blocked"] else "positive")]]
    link = notify.guest_link(user["channel"], user["username"])
    if link:
        kb.append([Btn(text="✉️ Написать", url=link)])
    kb.append(_back("a:u:l:0", "⬅️ К гостям"))
    await _show(ev, ch, Out(text=text, kb=kb))


# ====================================================================== доступ
async def _access_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "l"
    if action == "l":
        await _access_list(ev, ch)
    elif action == "add":
        await _ask(ev, ch, "admin_add", {},
                   "👑 Пришлите <b>ID пользователя</b> в Telegram — числом.\n\n"
                   "Где взять: пусть человек напишет этому боту команду <code>/id</code> "
                   "и пришлёт вам число.\n\n"
                   "Либо выберите его из тех, кто уже писал боту, — кнопка ниже.")
    elif action == "g":
        await _access_guests(ev, ch, int(args[1]) if len(args) > 1 else 0)
    elif action == "p":
        await _access_add_guest(ev, ch, int(args[1]))
    elif action == "d":
        await _access_confirm_remove(ev, ch, int(args[1]))
    elif action == "dd":
        ok, message = await admins.remove(int(args[1]))
        await _answer(ev, ch, message)
        await _access_list(ev, ch)


async def _access_list(ev: Event, ch: Channel) -> None:
    rows = await admins.listing()
    lines = ["👑 <b>Доступ к админ-панели</b>", ""]
    kb: list[list[Btn]] = []
    for row in rows:
        mark = "👑" if row["is_owner"] else "🛠"
        name = row["full_name"] or (f"@{row['username']}" if row["username"] else row["user_id"])
        role = "владелец" if row["is_owner"] else "менеджер"
        lines.append(f"{mark} <b>{esc(name)}</b> — {role}\n"
                     f"   ID <code>{row['user_id']}</code> · добавлен {fmt_dt(row['created_at'])}")
        if not row["is_owner"]:
            kb.append([Btn(text=f"🗑 Убрать {name}", data=f"a:acc:d:{row['user_id']}",
                           intent="negative")])
    if not rows:
        lines.append("Пока никого — доступ выдан через переменную окружения ADMIN_IDS.")
    if cfg.admin_ids:
        lines += ["", f"🔑 Аварийный доступ из переменной ADMIN_IDS: "
                      f"<code>{', '.join(str(i) for i in cfg.admin_ids)}</code>"]
    lines += ["", "<i>Владельца убрать нельзя — это защита от потери доступа к боту.</i>"]

    kb.append([Btn(text="➕ Добавить по ID", data="a:acc:add"),
               Btn(text="👥 Выбрать из гостей", data="a:acc:g:0")])
    kb.append(_back("a:h", "⬅️ В админку"))
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


async def _access_guests(ev: Event, ch: Channel, page: int) -> None:
    total = await repo.count_users(channel=TG)
    users = await repo.list_users(limit=PAGE, offset=page * PAGE, channel=TG)
    current = await admins.ids()
    kb: list[list[Btn]] = []
    for user in users:
        if int(user["ext_id"]) in current:
            continue
        name = user["full_name"] or user["username"] or user["ext_id"]
        kb.append([Btn(text=f"👤 {name}", data=f"a:acc:p:{user['id']}")])
    nav: list[Btn] = []
    if page > 0:
        nav.append(Btn(text="⬅️", data=f"a:acc:g:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(Btn(text="➡️", data=f"a:acc:g:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append(_back("a:acc:l", "⬅️ К доступу"))
    await _show(ev, ch, Out(
        text="👥 <b>Кому выдать доступ?</b>\n\nПоказаны те, кто писал боту в Telegram.",
        kb=kb))


async def _access_add_guest(ev: Event, ch: Channel, user_pk: int) -> None:
    user = await repo.get_user_pk(user_pk)
    if user is None or user["channel"] != TG:
        await _access_list(ev, ch)
        return
    added = await admins.add(int(user["ext_id"]), user["username"], user["full_name"],
                             added_by=_actor(ev))
    await _answer(ev, ch, "Доступ выдан" if added else "У него уже есть доступ")
    if added:
        await _notify_new_admin(ch, int(user["ext_id"]))
    await _access_list(ev, ch)


async def _access_confirm_remove(ev: Event, ch: Channel, user_id: int) -> None:
    row = await admins.get(user_id)
    name = (row["full_name"] or row["username"] or user_id) if row else user_id
    kb = [[Btn(text="🗑 Да, убрать", data=f"a:acc:dd:{user_id}", intent="negative")],
          [Btn(text="✖️ Отмена", data="a:acc:l")]]
    await _show(ev, ch, Out(
        text=f"⚠️ Убрать доступ у <b>{esc(name)}</b>?\n\n"
             "Он перестанет видеть админ-панель и кнопки под заказами.", kb=kb))


async def _notify_new_admin(ch: Channel, user_id: int) -> None:
    """Сообщить человеку, что ему выдали доступ."""
    try:
        await ch.send(str(user_id), Out(
            text="🛠 <b>Вам выдали доступ к админ-панели Fatucci</b>\n\n"
                 "Откройте её командой /admin.",
            kb=[[Btn(text="🛠 Открыть админ-панель", data="a:h", intent="positive")]]))
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось уведомить нового админа %s: %s", user_id, exc)


# =================================================================== рассылка
async def _broadcast_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "m"
    if action == "m":
        tg_count = await repo.count_users(channel=TG, blocked=False)
        max_count = await repo.count_users(channel=MAX, blocked=False)
        kb = [
            [Btn(text=f"📨 Всем ({tg_count + max_count})", data="a:bc:go:all")],
            [Btn(text=f"Telegram ({tg_count})", data="a:bc:go:tg"),
             Btn(text=f"MAX ({max_count})", data="a:bc:go:max")],
            _back("a:h", "⬅️ В админку"),
        ]
        await _show(ev, ch, Out(
            text="📢 <b>Рассылка</b>\n\nСообщение уйдёт всем гостям, кроме чёрного списка.\n"
                 "Кому отправляем?", kb=kb))
    elif action == "go":
        await _ask(ev, ch, "broadcast", {"target": args[1]},
                   "📢 Пришлите текст рассылки (можно с фото одним сообщением).\n"
                   "Отмена — /cancel")


# ================================================================== настройки
async def _settings_route(ev: Event, ch: Channel, args: list[str]) -> None:
    action = args[0] if args else "m"
    if action == "m":
        hidden = {"price"}          # живёт в разделе «Меню и цены»
        kb = [[Btn(text=title, data=f"a:cfg:s:{code}")]
              for code, (title, _) in SETTING_SECTIONS.items() if code not in hidden]
        kb.append([Btn(text="✍️ Тексты бота", data="a:t:l"),
                   Btn(text="👑 Доступ", data="a:acc:l")])
        kb.append([Btn(text="🧪 Проверить оплату", data="a:cfg:yk")])
        kb.append([_help("settings")])
        kb.append(_back("a:h", "⬅️ В админку"))
        await _show(ev, ch, Out(
            text="⚙️ <b>Настройки</b>\n\nВыберите раздел. Если непонятно, "
                 "что делает пункт — загляните в справку внизу.", kb=kb))
    elif action == "s":
        await _settings_section(ev, ch, args[1])
    elif action == "e":
        await _settings_edit(ev, ch, args[1], args[2])
    elif action == "t":
        key = args[2]
        current = await repo.get_bool(key)
        await repo.set_setting(key, "0" if current else "1")
        await _settings_section(ev, ch, args[1])
    elif action == "yk":
        await _answer(ev, ch, "Проверяю…")
        ok, message = await payments.check_setup()
        await ch.send(ev.chat_id, Out(text=message if ok else "⚠️ " + message,
                                      kb=[_back("a:cfg:s:pay")]))


async def _settings_section(ev: Event, ch: Channel, code: str) -> None:
    section = SETTING_SECTIONS.get(code)
    if section is None:
        await _settings_route(ev, ch, ["m"])
        return
    title, fields = section
    lines = [f"<b>{title}</b>", ""]
    kb: list[list[Btn]] = []
    for key, label, kind in fields:
        value = await repo.get_setting(key)
        if kind == "bool":
            state = "✅ вкл" if value in ("1", "true", "yes") else "🚫 выкл"
            lines.append(f"{esc(label)}: {state}")
            kb.append([Btn(text=f"{state} · {label}", data=f"a:cfg:t:{code}:{key}")])
        else:
            if kind == "secret":
                shown = mask_secret(value)
            else:
                shown = value if len(value) <= 60 else value[:57] + "…"
            lines.append(f"{esc(label)}: <code>{esc(shown) or '—'}</code>")
            kb.append([Btn(text=f"✏️ {label}", data=f"a:cfg:e:{code}:{key}")])
    if code == "ch":
        lines += ["", _max_hint(await repo.get_setting("max_token"),
                                await repo.get_bool("max_enabled", True))]
    if code == "price":
        lines += ["", await _discount_preview()]
    topic = SECTION_HELP.get(code)
    if topic:
        kb.append([_help(topic)])
    kb.append(_back("a:cfg:m", "⬅️ К настройкам"))
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


SECTION_HELP = {
    "gen": "settings", "ch": "channels", "rem": "settings",
    "cur": "courier", "tpl": "courier", "pay": "payment", "price": "prices",
}


async def _discount_preview() -> str:
    """Показать скидки в рублях на примере реального объекта — так понятнее."""
    tiers = await pricing.tiers()
    if not tiers:
        return ("ℹ️ Скидок сейчас нет — цена одинаковая при любом количестве.\n\n"
                "Чтобы включить, впишите пороги в формате <code>3=5, 5=10</code>:\n"
                "от 3 наборов −5%, от 5 наборов −10%.")
    objects = await repo.list_objects(active_only=True, selectable=True)
    if not objects:
        return f"Пороги: {pricing.format_tiers(tiers)}"
    obj = objects[0]
    return (f"<b>Пример для «{esc(obj['title'])}»</b>\n"
            f"{pricing.table(obj['price_kop'], tiers, int(obj['max_qty'] or 10))}")


def _max_hint(token: str, enabled: bool) -> str:
    if not token:
        return ("ℹ️ Чтобы подключить MAX: создайте бота в MasterBot внутри MAX, "
                "вставьте сюда его токен и username. Перезапускать бота не нужно — "
                "он поднимет MAX сам в течение минуты.")
    if not enabled:
        return "⏸ Токен есть, но канал выключен переключателем выше."
    return "✅ Токен задан. Если MAX не отвечает — проверьте логи при старте."


async def _settings_edit(ev: Event, ch: Channel, code: str, key: str) -> None:
    section = SETTING_SECTIONS.get(code)
    if section is None:
        return
    label = next((lbl for k, lbl, _ in section[1] if k == key), key)
    kind = next((t for k, _, t in section[1] if k == key), "text")
    value = await repo.get_setting(key)
    if kind == "secret":
        value = mask_secret(value)
    if key == "discount_tiers":
        await _ask(ev, ch, "setting", {"key": key, "kind": kind, "section": code},
                   "💰 <b>Скидки за количество</b>\n\n"
                   f"Сейчас: <code>{esc(value) or 'скидок нет'}</code>\n\n"
                   "Пришлите пороги в формате <code>3=5, 5=10, 10=15</code> — это значит:\n"
                   "• от 3 наборов — минус 5%\n"
                   "• от 5 наборов — минус 10%\n"
                   "• от 10 наборов — минус 15%\n\n"
                   "Скидка считается только внутри одного заказа на один день.\n"
                   "Чтобы убрать скидки — отправьте <code>-</code>.")
        return
    if key == "pm_token":
        hint = (
            "Токен выдаёт <b>@BotFather</b>: /mybots → ваш бот → "
            "<b>Payments</b> → PayMaster.\n\n"
            "Выглядит так: <code>123456789:TEST:abcdef…</code>\n"
            "<b>TEST</b> — тестовый режим, деньги не списываются.\n"
            "<b>LIVE</b> — настоящие платежи.\n\n"
            "После сохранения нажмите «Проверить оплату»."
        )
        await _ask(ev, ch, "setting", {"key": key, "kind": kind, "section": code},
                   f"💳 <b>{esc(label)}</b>\n\n"
                   f"Сейчас: <code>{esc(value) or 'не задан'}</code>\n\n{hint}")
        return
    hint = {
        "time": "Формат: <code>ЧЧ:ММ</code>, например <code>20:00</code>",
        "int": "Пришлите целое число",
        "money": "Сумма в рублях, например <code>900</code>",
        "secret": "Значение будет показываться скрытым. Сообщение с ключом "
                  "лучше удалить из чата после отправки.",
    }.get(kind, "")
    if key.startswith("courier_"):
        hint = ("Доступные подстановки: {date_h}, {date}, {orders_count}, {sets_count}, "
                "{total}, {now}, {address}, {object_title}, {apartment}, {set_title}, "
                "{qty}, {phone}, {comment}, {number}")
    await _ask(ev, ch, "setting", {"key": key, "kind": kind, "section": code},
               f"⚙️ <b>{esc(label)}</b>\n\nСейчас:\n<code>{esc(value) or '—'}</code>\n\n"
               f"{hint}\n\nПришлите новое значение (или <code>-</code>, чтобы очистить).")


# ================================================================= статистика
async def _stats_route(ev: Event, ch: Channel, args: list[str]) -> None:
    period = args[0] if args else "m"
    if period == "m":
        kb = [
            [Btn(text="Сегодня", data="a:st:d0"), Btn(text="7 дней", data="a:st:d7")],
            [Btn(text="30 дней", data="a:st:d30"), Btn(text="90 дней", data="a:st:d90")],
            _back("a:h", "⬅️ В админку"),
        ]
        await _show(ev, ch, Out(text="📊 <b>Статистика</b>\n\nВыберите период:", kb=kb))
        return

    days = int(period[1:] or 0)
    date_to = today() + timedelta(days=1) if days else today()
    date_from = today() - timedelta(days=days)
    iso_from, iso_to = fmt_date_iso(date_from), fmt_date_iso(date_to)

    totals = await repo.stats_totals(iso_from, iso_to)
    rows = await repo.stats_by_object(iso_from, iso_to)
    sources = await repo.stats_by_source(iso_from, iso_to)

    lines = [
        f"📊 <b>Статистика за {days or 1} дн.</b>",
        f"<i>{date_from.strftime('%d.%m')} — {date_to.strftime('%d.%m')}</i>",
        "",
        f"Заказов: <b>{totals['cnt']}</b>",
        f"Сетов: <b>{totals['sets']}</b>",
        f"Выручка (оплачено): <b>{fmt_money(totals['revenue'])}</b>",
        "",
        "<b>По объектам:</b>",
    ]
    for row in rows:
        lines.append(
            f"• {esc(row['title'] or '—')} — {row['orders_count']} зак., "
            f"{row['sets_count']} сет., {fmt_money(row['revenue_kop'] or 0)}"
        )
    if not rows:
        lines.append("— нет данных")
    if sources:
        lines += ["", "<b>По QR-кодам:</b>"]
        for row in sources:
            lines.append(f"• <code>{esc(row['source_code'] or '—')}</code> — "
                         f"{row['orders_count']} зак.")
    kb = [[Btn(text="📤 Выгрузить в CSV", data=f"a:x:p{days}")], _back("a:st:m")]
    await _show(ev, ch, Out(text="\n".join(lines), kb=kb))


# ==================================================================== экспорт
async def _export_route(ev: Event, ch: Channel, args: list[str]) -> None:
    code = args[0] if args else "m"
    if code == "m":
        kb = [
            [Btn(text="За 7 дней", data="a:x:p7"), Btn(text="За 30 дней", data="a:x:p30")],
            [Btn(text="За 90 дней", data="a:x:p90"), Btn(text="Все заказы", data="a:x:pall")],
            _back("a:h", "⬅️ В админку"),
        ]
        await _show(ev, ch, Out(
            text="📤 <b>Выгрузка заказов</b>\n\nПришлю CSV-файл — открывается в Excel "
                 "и Google Таблицах.", kb=kb))
        return

    await _answer(ev, ch, "Готовлю файл…")
    raw = code[1:]
    if raw == "all":
        date_from, date_to = "2000-01-01", "2100-01-01"
        label = "все"
    else:
        days = int(raw or 30)
        date_from = fmt_date_iso(today() - timedelta(days=days))
        date_to = fmt_date_iso(today() + timedelta(days=60))
        label = f"{days} дн."

    orders = await repo.list_orders(date_from=date_from, date_to=date_to, limit=100000)
    path = cfg.export_dir / f"orders_{today().strftime('%Y%m%d')}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow([
            "Номер", "Создан", "Дата доставки", "Статус", "Канал", "Объект", "Адрес",
            "Апартаменты", "Сет", "Кол-во", "Цена", "Сумма", "Телефон", "Комментарий",
            "QR", "Оплачен",
        ])
        for order in orders:
            writer.writerow([
                order["number"], fmt_dt(order["created_at"]),
                order["delivery_date"], statuses.label(order["status"]), order["channel"],
                order["object_title"], order["object_address"], order["apartment"],
                order["set_title"], order["qty"],
                f"{order['price_kop'] / 100:.2f}", f"{order['total_kop'] / 100:.2f}",
                order["phone"], order["comment"], order["source_code"],
                fmt_dt(order["paid_at"]) if order["paid_at"] else "",
            ])
    sent = await ch.send_file(ev.chat_id, str(path),
                             caption=f"📤 Заказы ({label}) — {len(orders)} шт.")
    if not sent:
        await ch.send(ev.chat_id, Out(text="⚠️ Не удалось отправить файл."))


# ============================================================ ввод значений
async def _ask(ev: Event, ch: Channel, kind: str, ctx: dict[str, Any], prompt: str) -> None:
    ctx = dict(ctx)
    await repo.set_admin_state(int(ev.user_id), kind, ctx)
    await _answer(ev, ch)
    await ch.send(ev.chat_id, Out(
        text=prompt + "\n\n<i>Отмена — /cancel</i>",
        kb=[[Btn(text="✖️ Отмена", data="a:h")]]))


async def _consume_input(ev: Event, ch: Channel, kind: str, ctx: dict[str, Any]) -> None:
    admin_id = int(ev.user_id)
    text = (ev.text or "").strip()
    photo_id = ev.raw.get("photo_file_id", "")

    handlers = {
        "obj_new": _in_obj_new,
        "set_new": _in_set_new,
        "offer_new": _in_offer_new,
        "field": _in_field,
        "text": _in_text,
        "setting": _in_setting,
        "reject": _in_reject,
        "broadcast": _in_broadcast,
        "admin_add": _in_admin_add,
        "find_order": _in_find_order,
        "find_date": _in_find_date,
        "rot_date": _in_rot_date,
        "digest_date": _in_digest_date,
    }
    handler = handlers.get(kind)
    if handler is None:
        await repo.clear_admin_state(admin_id)
        await _home(ev, ch, new_message=True)
        return
    try:
        await handler(ev, ch, ctx, text, photo_id)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка ввода админки (%s)", kind)
        await repo.clear_admin_state(admin_id)
        await ch.send(ev.chat_id, Out(text="⚠️ Не удалось применить значение. Попробуйте снова.",
                                      kb=[[Btn(text="🏠 Админка", data="a:h")]]))


async def _in_obj_new(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    step = ctx.get("step", "title")
    data = ctx.get("data", {})
    if step == "title":
        data["title"] = text[:120]
        await repo.set_admin_state(int(ev.user_id), "obj_new", {"step": "address", "data": data})
        await ch.send(ev.chat_id, Out(text="📍 Адрес доставки?\nНапример: "
                                           "<code>г. Сочи, ул. Северная, д. 12</code>"))
        return
    if step == "address":
        data["address"] = text[:200]
        await repo.set_admin_state(int(ev.user_id), "obj_new", {"step": "price", "data": data})
        await ch.send(ev.chat_id, Out(text="💰 Цена завтрака в рублях? Например: <code>900</code>"))
        return

    price = parse_money(text)
    if price is None:
        await ch.send(ev.chat_id, Out(text="⚠️ Нужна сумма в рублях, например <code>900</code>."))
        return
    code = slug_code(data["title"])
    suffix = 1
    base_code = code
    while await repo.code_taken(code):
        suffix += 1
        code = f"{base_code}{suffix}"
    object_id = await repo.create_object(
        code=code, title=data["title"], address=data["address"], price_kop=price,
        group_title="", min_qty=await repo.get_int("default_min_qty", 1),
        max_qty=await repo.get_int("default_max_qty", 10),
    )
    await repo.clear_admin_state(int(ev.user_id))
    await ch.send(ev.chat_id, Out(
        text=f"✅ Объект <b>{esc(data['title'])}</b> создан.\nКод QR: <code>{esc(code)}</code>",
        kb=[[Btn(text="🔗 Получить QR", data=f"a:q:o:{object_id}")],
            [Btn(text="⚙️ Настроить объект", data=f"a:b:c:{object_id}")]]))


async def _in_set_new(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    step = ctx.get("step", "title")
    data = ctx.get("data", {})
    if step == "title":
        data["title"] = text[:120]
        await repo.set_admin_state(int(ev.user_id), "set_new", {"step": "desc", "data": data})
        await ch.send(ev.chat_id, Out(text="📝 Состав сета? Пришлите текст одним сообщением."))
        return
    set_id = await repo.create_set(title=data["title"], description=text[:2000],
                                   sort_order=100)
    await repo.clear_admin_state(int(ev.user_id))
    await ch.send(ev.chat_id, Out(
        text=f"✅ Сет <b>{esc(data['title'])}</b> добавлен.\n"
             "Не забудьте загрузить фото и поставить его в ротацию.",
        kb=[[Btn(text="🖼 Загрузить фото", data=f"a:m:e:{set_id}:photo_path")],
            [Btn(text="🗓 Ротация", data="a:r:l"), Btn(text="🥐 К сету", data=f"a:m:c:{set_id}")]]))


async def _in_offer_new(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    step = ctx.get("step", "title")
    data = ctx.get("data", {})
    if step == "title":
        data["title"] = text[:120]
        await repo.set_admin_state(int(ev.user_id), "offer_new", {"step": "desc", "data": data})
        await ch.send(ev.chat_id, Out(text="📝 Короткое описание предложения?"))
        return
    if step == "desc":
        data["description"] = text[:1000]
        await repo.set_admin_state(int(ev.user_id), "offer_new", {"step": "url", "data": data})
        await ch.send(ev.chat_id, Out(text="🔗 Ссылка (или <code>-</code>, если её нет)?"))
        return
    url = "" if text == "-" else text[:400]
    offer_id = await repo.create_offer(title=data["title"], description=data["description"],
                                       url=url, button_text="Подробнее", sort_order=100)
    await repo.clear_admin_state(int(ev.user_id))
    await ch.send(ev.chat_id, Out(text=f"✅ Предложение <b>{esc(data['title'])}</b> добавлено.",
                                  kb=[[Btn(text="🍽 Открыть", data=f"a:f:c:{offer_id}")]]))


async def _in_field(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    entity, entity_id, key = ctx["entity"], int(ctx["id"]), ctx["key"]
    specs = {"obj": OBJECT_FIELDS, "set": SET_FIELDS, "offer": OFFER_FIELDS}[entity]
    kind = next((t for k, _, t in specs if k == key), "text")

    if kind == "photo":
        if not photo:
            await ch.send(ev.chat_id, Out(text="⚠️ Пришлите именно фотографию."))
            return
        path = cfg.photos_dir / f"{entity}_{entity_id}.jpg"
        ok = await ch.download_photo(photo, path)
        if not ok:
            await ch.send(ev.chat_id, Out(text="⚠️ Не удалось сохранить фото."))
            return
        await repo.drop_media(str(path))
        value: Any = str(path)
    else:
        value = _parse_value(kind, text)
        if value is None:
            await ch.send(ev.chat_id, Out(text=f"⚠️ Неверный формат ({kind}). Попробуйте ещё раз."))
            return
        if key == "code":
            if await repo.code_taken(str(value), entity_id):
                await ch.send(ev.chat_id, Out(text="⚠️ Такой код уже занят другим объектом."))
                return

    updater = {"obj": repo.update_object, "set": repo.update_set, "offer": repo.update_offer}[entity]
    await updater(entity_id, **{key: value})
    await repo.clear_admin_state(int(ev.user_id))
    back = {"obj": f"a:b:c:{entity_id}", "set": f"a:m:c:{entity_id}", "offer": f"a:f:c:{entity_id}"}
    await ch.send(ev.chat_id, Out(text="✅ Сохранено.",
                                  kb=[[Btn(text="⬅️ Назад", data=back[entity]),
                                       Btn(text="🏠 Админка", data="a:h")]]))


async def _in_text(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    await repo.set_text(ctx["key"], text)
    await repo.clear_admin_state(int(ev.user_id))
    await ch.send(ev.chat_id, Out(text="✅ Текст обновлён.",
                                  kb=[[Btn(text="✍️ К текстам", data="a:t:l"),
                                       Btn(text="🏠 Админка", data="a:h")]]))


async def _in_setting(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    key, kind = ctx["key"], ctx.get("kind", "text")
    raw = "" if text == "-" else text
    if kind in ("int", "time", "money") and raw:
        parsed = _parse_value(kind, raw)
        if parsed is None:
            await ch.send(ev.chat_id, Out(text="⚠️ Неверный формат, попробуйте ещё раз."))
            return
        raw = str(parsed)
    await repo.set_setting(key, raw)
    await repo.clear_admin_state(int(ev.user_id))
    await ch.send(ev.chat_id, Out(
        text="✅ Настройка сохранена.",
        kb=[[Btn(text="⬅️ Назад", data=f"a:cfg:s:{ctx.get('section', 'gen')}"),
             Btn(text="🏠 Админка", data="a:h")]]))


async def _in_reject(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    reason = "" if text == "-" else text[:300]
    await repo.clear_admin_state(int(ev.user_id))
    ok, message = await orders_service.change_status(
        int(ctx["order_id"]), statuses.REJECTED, actor=_actor(ev), note=reason
    )
    await ch.send(ev.chat_id, Out(text=("✅ " if ok else "⚠️ ") + esc(message),
                                  kb=[[Btn(text="🏠 Админка", data="a:h")]]))


async def _in_broadcast(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    await repo.clear_admin_state(int(ev.user_id))
    target = ctx.get("target", "all")
    channels = [TG, MAX] if target == "all" else [target]
    photo_path = ""
    if photo:
        photo_path = str(cfg.photos_dir / "broadcast.jpg")
        await ch.download_photo(photo, cfg.photos_dir / "broadcast.jpg")
        await repo.drop_media(photo_path)
    await ch.send(ev.chat_id, Out(text="📤 Рассылка запущена…"))
    ok, failed = await notify.broadcast(text, channels, photo_path)
    await ch.send(ev.chat_id, Out(
        text=f"✅ Рассылка завершена.\nДоставлено: <b>{ok}</b>\nНе доставлено: {failed}",
        kb=[[Btn(text="🏠 Админка", data="a:h")]]))


async def _in_admin_add(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    raw = text.strip().lstrip("@")
    if not raw.isdigit():
        await ch.send(ev.chat_id, Out(
            text="⚠️ Нужен именно числовой ID, например <code>123456789</code>.\n"
                 "По @username выдать доступ нельзя — Telegram не даёт ботам "
                 "разрешать имена.\n\n"
                 "Пусть человек отправит боту <code>/id</code> и пришлёт вам число.",
            kb=[[Btn(text="👥 Выбрать из гостей", data="a:acc:g:0")],
                [Btn(text="✖️ Отмена", data="a:acc:l")]]))
        return
    await repo.clear_admin_state(int(ev.user_id))
    user_id = int(raw)
    user = await repo.get_user(TG, str(user_id))
    added = await admins.add(user_id, user["username"] if user else "",
                             user["full_name"] if user else "", added_by=_actor(ev))
    if added:
        await _notify_new_admin(ch, user_id)
    await ch.send(ev.chat_id, Out(
        text=(f"✅ Доступ выдан: <code>{user_id}</code>" if added
              else f"ℹ️ У <code>{user_id}</code> доступ уже был"),
        kb=[[Btn(text="👑 К списку", data="a:acc:l"), Btn(text="🏠 Админка", data="a:h")]]))


async def _in_find_order(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    await repo.clear_admin_state(int(ev.user_id))
    order = await repo.get_order_by_number(text)
    if order is None and text.upper().startswith(await repo.get_setting("order_prefix", "F")):
        order = await repo.get_order_by_number(text.upper())
    if order is not None:
        await _order_card(ev, ch, order["id"])
        return
    await ch.send(ev.chat_id, Out(
        text=f"🔎 Заказ <code>{esc(text)}</code> не найден.",
        kb=[[Btn(text="📦 К заказам", data="a:o:l:new:0"), Btn(text="🏠 Админка", data="a:h")]]))


async def _in_find_date(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    await repo.clear_admin_state(int(ev.user_id))
    day = parse_date(text)
    if day is None:
        await ch.send(ev.chat_id, Out(text="⚠️ Не разобрал дату. Формат: <code>ДД.ММ.ГГГГ</code>"))
        return
    await _orders_list(ev, ch, f"dt_{fmt_date_iso(day)}", 0)


async def _in_rot_date(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    day = parse_date(text)
    if day is None:
        await ch.send(ev.chat_id, Out(text="⚠️ Не разобрал дату. Формат: <code>ДД.ММ.ГГГГ</code>"))
        return
    await repo.clear_admin_state(int(ev.user_id))
    items = await repo.list_sets(active_only=True)
    iso = fmt_date_iso(day)
    kb = [[Btn(text=item["title"], data=f"a:r:ds:{iso}:{item['id']}")] for item in items]
    kb.append([Btn(text="🚫 Без завтрака", data=f"a:r:ds:{iso}:0")])
    kb.append([Btn(text="✖️ Отмена", data="a:r:d")])
    await ch.send(ev.chat_id, Out(text=f"📅 Какой сет на <b>{fmt_date(day)}</b>?", kb=kb))


async def _in_digest_date(ev: Event, ch: Channel, ctx: dict, text: str, photo: str) -> None:
    day = parse_date(text)
    if day is None:
        await ch.send(ev.chat_id, Out(text="⚠️ Не разобрал дату. Формат: <code>ДД.ММ.ГГГГ</code>"))
        return
    await repo.clear_admin_state(int(ev.user_id))
    await courier.send_digest(day, auto=False)


# ------------------------------------------------------------- утилиты ввода
async def _edit_field(ev: Event, ch: Channel, entity: str, entity_id: int, key: str) -> None:
    specs = {"obj": OBJECT_FIELDS, "set": SET_FIELDS, "offer": OFFER_FIELDS}[entity]
    label = next((lbl for k, lbl, _ in specs if k == key), key)
    kind = next((t for k, _, t in specs if k == key), "text")
    getter = {"obj": repo.get_object, "set": repo.get_set, "offer": repo.get_offer}[entity]
    row = await getter(entity_id)
    current = row[key] if row is not None else ""
    if kind in ("money", "money_opt") and current:
        current = fmt_money(int(current))
    hints = {
        "money": "Сумма в рублях, например <code>900</code>",
        "money_opt": "Сумма в рублях или <code>-</code>, чтобы брать цену объекта",
        "int": "Целое число",
        "time": "Формат <code>ЧЧ:ММ</code>, например <code>20:00</code>",
        "days": "Дни недели цифрами через запятую: 1=Пн … 7=Вс. Например <code>1,2,3,4,5</code>",
        "code": "Только латиница, цифры, <code>_</code> и <code>-</code>",
        "photo": "Пришлите фотографию одним сообщением",
        "text": "Пришлите новый текст",
    }
    await _ask(ev, ch, "field", {"entity": entity, "id": entity_id, "key": key},
               f"✏️ <b>{esc(label)}</b>\n\nСейчас: <code>{esc(current) or '—'}</code>\n\n"
               f"{hints.get(kind, '')}")


def _parse_value(kind: str, text: str) -> Optional[Any]:
    raw = text.strip()
    if kind == "text":
        return raw
    if kind == "int":
        return int(raw) if raw.lstrip("-").isdigit() else None
    if kind == "money":
        return parse_money(raw)
    if kind == "money_opt":
        return None if raw == "-" else parse_money(raw)
    if kind == "time":
        match = re.match(r"^(\d{1,2})[:.\s]?(\d{2})$", raw)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return f"{hour:02d}:{minute:02d}"
    if kind == "days":
        days = sorted({int(p) for p in raw.replace(" ", "").split(",")
                       if p.isdigit() and 1 <= int(p) <= 7})
        return ",".join(str(d) for d in days) if days else None
    if kind == "code":
        code = slug_code(raw, "")
        return code or None
    return raw


async def _confirm_delete(ev: Event, ch: Channel, entity: str, entity_id: int, text: str) -> None:
    prefix = {"obj": "a:b", "set": "a:m", "offer": "a:f"}[entity]
    kb = [[Btn(text="🗑 Да, удалить", data=f"{prefix}:dd:{entity_id}", intent="negative")],
          [Btn(text="✖️ Отмена", data=f"{prefix}:c:{entity_id}")]]
    await _show(ev, ch, Out(text=f"⚠️ {esc(text)}", kb=kb))
