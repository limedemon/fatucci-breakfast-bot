"""Расчёт доступных дат доставки с учётом дней доставки, отсечки и ротации сетов."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import aiosqlite

from . import repo
from .config import cfg
from .utils import now, parse_days, parse_time, today

Row = aiosqlite.Row


def order_deadline(day: date, obj: Row) -> datetime:
    """До какого момента принимается заказ на дату `day`."""
    lead = max(0, int(obj["lead_days"] or 0))
    cutoff = parse_time(obj["cutoff_time"] or "20:00")
    deadline_day = day - timedelta(days=lead)
    return datetime.combine(deadline_day, cutoff, tzinfo=cfg.tz)


def is_delivery_day(day: date, obj: Row) -> bool:
    return day.isoweekday() in parse_days(obj["delivery_days"])


def deadline_passed(day: date, obj: Row) -> bool:
    return now() > order_deadline(day, obj)


async def available_dates(obj: Row, limit: int = 14) -> list[tuple[date, Row]]:
    """Список (дата, сет) — только те даты, на которые заказ реально можно принять."""
    horizon = max(1, int(obj["max_days_ahead"] or 7))
    result: list[tuple[date, Row]] = []
    start = today()
    for offset in range(0, horizon + 1):
        day = start + timedelta(days=offset)
        if not is_delivery_day(day, obj):
            continue
        if deadline_passed(day, obj):
            continue
        breakfast = await repo.set_for_date(day)
        if breakfast is None:
            continue
        result.append((day, breakfast))
        if len(result) >= limit:
            break
    return result


async def check_date(obj: Row, day: date) -> tuple[bool, str]:
    """Можно ли ещё принять заказ на эту дату (проверка перед подтверждением)."""
    if not obj["is_active"]:
        return False, "Приём заказов по этому объекту приостановлен."
    if not is_delivery_day(day, obj):
        return False, "В этот день доставки нет."
    if deadline_passed(day, obj):
        deadline = order_deadline(day, obj)
        return False, f"Приём заказов на эту дату закрылся {deadline.strftime('%d.%m в %H:%M')}."
    if await repo.set_for_date(day) is None:
        return False, "На эту дату завтрак не запланирован."
    return True, ""


def price_for(obj: Row, breakfast: Optional[Row]) -> int:
    """Цена одного сета: индивидуальная цена сета важнее цены объекта."""
    if breakfast is not None and breakfast["price_kop"]:
        return int(breakfast["price_kop"])
    return int(obj["price_kop"] or 0)


def qty_limits(obj: Row) -> tuple[int, int]:
    low = max(1, int(obj["min_qty"] or 1))
    high = max(low, int(obj["max_qty"] or 10))
    return low, high
