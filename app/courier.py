"""Ежедневная сводка заказов для курьеров.

Заказчик просил не «таблицу и не пачку сообщений», а одно готовое текстовое
сообщение, которое можно целиком скопировать и отправить курьерской службе.
Форма сообщения задаётся шаблонами в админке (Курьеры -> Шаблон).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import aiosqlite

from . import notify, repo
from .channels.base import Btn
from .utils import esc, fmt_date, fmt_date_iso, fmt_money, fmt_phone, now, safe_format, strip_html

log = logging.getLogger(__name__)
Row = aiosqlite.Row


async def digest_statuses() -> list[str]:
    raw = await repo.get_setting("courier_statuses", "accepted,paid")
    return [s.strip() for s in raw.split(",") if s.strip()]


async def build_digest(day: date) -> str:
    """Собрать текст сводки на дату доставки."""
    iso = fmt_date_iso(day)
    orders = await repo.orders_for_delivery(iso, await digest_statuses())
    common: dict[str, Any] = {
        "date": day.strftime("%d.%m.%Y"),
        "date_h": fmt_date(day),
        "now": now().strftime("%d.%m.%Y %H:%M"),
    }

    if not orders:
        return safe_format(await repo.get_setting("courier_empty"), **common,
                           orders_count=0, sets_count=0, total=fmt_money(0))

    header_tpl = await repo.get_setting("courier_header")
    object_tpl = await repo.get_setting("courier_object")
    line_tpl = await repo.get_setting("courier_line")
    footer_tpl = await repo.get_setting("courier_footer")

    groups: dict[tuple[str, str], list[Row]] = {}
    for order in orders:
        groups.setdefault((order["object_address"], order["object_title"]), []).append(order)

    total_sets = sum(order["qty"] for order in orders)
    total_sum = sum(order["total_kop"] for order in orders)
    totals = {
        "orders_count": len(orders),
        "sets_count": total_sets,
        "total": fmt_money(total_sum),
    }

    parts = [safe_format(header_tpl, **common, **totals)]
    for (address, title), items in sorted(groups.items()):
        parts.append(safe_format(
            object_tpl,
            address=esc(address or title),
            object_title=esc(title),
            orders_count=len(items),
            sets_count=sum(item["qty"] for item in items),
            **common,
        ))
        for order in items:
            comment = f" · {order['comment']}" if order["comment"] else ""
            parts.append(safe_format(
                line_tpl,
                number=order["number"],
                apartment=esc(order["apartment"]),
                set_title=esc(order["set_title"]),
                qty=order["qty"],
                phone=fmt_phone(order["phone"]),
                comment=esc(comment),
                comment_raw=esc(order["comment"]),
                total=fmt_money(order["total_kop"]),
                address=esc(order["object_address"]),
                object_title=esc(order["object_title"]),
                **common,
            ))
    parts.append(safe_format(footer_tpl, **common, **totals))
    return "\n".join(part for part in parts if part.strip())


async def send_digest(day: date, auto: bool = True) -> str:
    """Собрать сводку и отправить менеджерам."""
    body = await build_digest(day)
    await repo.save_digest(fmt_date_iso(day), body, auto)
    prefix = "🤖 <b>Автоматическая выгрузка для курьеров</b>\n" if auto else ""
    kb = [[
        Btn(text="📋 Текстом для копирования", data=f"a:cur:copy:{fmt_date_iso(day)}"),
        Btn(text="🔄 Пересобрать", data=f"a:cur:make:{fmt_date_iso(day)}"),
    ]]
    await notify.send_to_admins(prefix + body, kb)
    return body


async def copy_version(day: date) -> str:
    """Тот же текст без разметки — одним блоком, который удобно скопировать."""
    body = strip_html(await build_digest(day))
    return "<pre>" + esc(body) + "</pre>"


async def target_day() -> date:
    """На какой день формируется автоматическая выгрузка."""
    offset = await repo.get_int("courier_day_offset", 1)
    return now().date() + timedelta(days=offset)
