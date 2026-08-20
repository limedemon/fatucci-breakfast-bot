"""Цены и скидки за количество.

Заказчик: «чем больше сетов человек заказывает, тем ниже итоговая стоимость
за сет — но только в разрезе одного дня, а не на всё время пребывания».
Поэтому скидка считается по количеству наборов внутри одного заказа
(один заказ = одна дата доставки), а не накопительно.

Цена сета у разных апартаментов разная, поэтому пороги задаются процентами —
их достаточно настроить один раз, и они работают для любого объекта.

Формат настройки (админ-панель -> 🥐 Меню и цены -> Скидки):

    3=5, 5=10, 10=15

читается как «от 3 наборов −5%, от 5 наборов −10%, от 10 наборов −15%».
Пустая строка — скидок нет.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

import aiosqlite

from . import repo
from .utils import fmt_money

Row = aiosqlite.Row

MAX_PERCENT = 90


class Tier(NamedTuple):
    """Порог скидки: от `qty` наборов действует скидка `percent`%."""

    qty: int
    percent: int


class Price(NamedTuple):
    """Итог расчёта заказа."""

    base_per_set: int   # цена сета без скидки, копейки
    per_set: int        # цена сета со скидкой, копейки
    total: int          # к оплате, копейки
    percent: int        # применённая скидка, %
    qty: int            # количество наборов

    @property
    def saved(self) -> int:
        """Сколько гость сэкономил на этом заказе, копейки."""
        return (self.base_per_set - self.per_set) * self.qty


def parse_tiers(raw: str) -> list[Tier]:
    """Разобрать строку настройки в список порогов (по убыванию количества)."""
    tiers: list[Tier] = []
    for chunk in re.split(r"[,;\n]", raw or ""):
        match = re.match(r"^\s*(\d+)\s*[=:\-]\s*(\d+)\s*%?\s*$", chunk)
        if not match:
            continue
        qty, percent = int(match.group(1)), int(match.group(2))
        if qty >= 2 and 1 <= percent <= MAX_PERCENT:
            tiers.append(Tier(qty, percent))
    # при совпадении количества берём большую скидку
    best: dict[int, int] = {}
    for tier in tiers:
        best[tier.qty] = max(best.get(tier.qty, 0), tier.percent)
    return sorted((Tier(q, p) for q, p in best.items()), key=lambda t: t.qty, reverse=True)


def format_tiers(tiers: list[Tier]) -> str:
    """Обратно в строку настройки — по возрастанию, как удобнее читать."""
    return ", ".join(f"{t.qty}={t.percent}" for t in sorted(tiers, key=lambda t: t.qty))


def percent_for(qty: int, tiers: list[Tier]) -> int:
    """Какая скидка действует при таком количестве наборов."""
    for tier in tiers:            # список уже отсортирован по убыванию
        if qty >= tier.qty:
            return tier.percent
    return 0


def calc(base_per_set: int, qty: int, tiers: list[Tier]) -> Price:
    """Посчитать цену заказа. Цена за сет округляется до рубля."""
    qty = max(1, int(qty))
    percent = percent_for(qty, tiers)
    if percent:
        discounted = round(base_per_set * (100 - percent) / 100 / 100) * 100
        per_set = max(100, discounted)     # не даём уйти ниже рубля
    else:
        per_set = base_per_set
    return Price(base_per_set=base_per_set, per_set=per_set, total=per_set * qty,
                 percent=percent, qty=qty)


async def tiers() -> list[Tier]:
    return parse_tiers(await repo.get_setting("discount_tiers"))


async def price_for_order(base_per_set: int, qty: int) -> Price:
    return calc(base_per_set, qty, await tiers())


def next_tier(qty: int, tiers_list: list[Tier]) -> Optional[Tier]:
    """Ближайший следующий порог — чтобы подсказать гостю «возьмите на один больше»."""
    ahead = [t for t in sorted(tiers_list, key=lambda t: t.qty) if t.qty > qty]
    return ahead[0] if ahead else None


def hint(base_per_set: int, qty: int, tiers_list: list[Tier]) -> str:
    """Подсказка гостю о скидке на шаге выбора количества."""
    if not tiers_list:
        return ""
    lines = []
    current = percent_for(qty, tiers_list)
    if current:
        price = calc(base_per_set, qty, tiers_list)
        lines.append(f"🎁 Скидка −{current}%: <b>{fmt_money(price.per_set)}</b> за сет")
    upcoming = next_tier(qty, tiers_list)
    if upcoming:
        price = calc(base_per_set, upcoming.qty, tiers_list)
        lines.append(
            f"от {upcoming.qty} наборов — −{upcoming.percent}% "
            f"({fmt_money(price.per_set)} за сет)"
        )
    return "\n".join(lines)


def table(base_per_set: int, tiers_list: list[Tier], max_qty: int = 10) -> str:
    """Табличка «сколько — почём» для админки."""
    if not tiers_list:
        return "Скидок нет — цена одинаковая при любом количестве."
    rows = [f"1 набор — {fmt_money(base_per_set)}"]
    for tier in sorted(tiers_list, key=lambda t: t.qty):
        if tier.qty > max_qty:
            continue
        price = calc(base_per_set, tier.qty, tiers_list)
        rows.append(
            f"от {tier.qty} — {fmt_money(price.per_set)} за сет "
            f"(−{tier.percent}%, итого {fmt_money(price.total)})"
        )
    return "\n".join(rows)
