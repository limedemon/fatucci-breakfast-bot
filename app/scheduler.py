"""Фоновые задачи: выгрузка курьерам, напоминания и контроль оплат."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from . import courier, repo
from .channels.base import Btn, Out, get_channel
from .utils import fmt_date_iso, now, parse_time

log = logging.getLogger(__name__)


async def run_all() -> None:
    await asyncio.gather(
        _loop("courier", courier_tick, 60),
        _loop("reminders", reminder_tick, 300),
    )


async def _loop(name: str, tick, default_delay: int) -> None:
    log.info("Фоновая задача «%s» запущена", name)
    while True:
        try:
            delay = await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка фоновой задачи «%s»", name)
            delay = None
        await asyncio.sleep(delay or default_delay)


# ------------------------------------------------------- выгрузка курьерам
async def courier_tick() -> int:
    if not await repo.get_bool("courier_enabled", True):
        return 120
    scheduled = parse_time(await repo.get_setting("courier_time", "20:00"))
    current = now()
    planned = current.replace(hour=scheduled.hour, minute=scheduled.minute,
                              second=0, microsecond=0)
    if not (planned <= current < planned + timedelta(minutes=15)):
        return 60
    day = await courier.target_day()
    if await repo.digest_exists_today(fmt_date_iso(day)):
        return 60
    log.info("Формирую автоматическую выгрузку для курьеров на %s", day)
    await courier.send_digest(day, auto=True)
    return 60


# ------------------------------------------- напоминания о брошенных заказах
async def reminder_tick() -> int:
    if not await repo.get_bool("reminder_enabled", True):
        return 600
    after_min = await repo.get_int("reminder_after_min", 45)
    max_hours = await repo.get_int("reminder_max_hours", 24)
    sessions = await repo.stale_sessions(after_min, max_hours)
    if not sessions:
        return 300

    text = await repo.render_text("reminder")
    for session in sessions:
        channel = get_channel(session["channel"])
        if channel is None:
            continue
        user = await repo.get_user(session["channel"], session["ext_id"])
        if user is None or user["is_blocked"]:
            await repo.mark_reminded(session["channel"], session["ext_id"])
            continue
        kb = [[Btn(text="🥐 Продолжить заказ", data="g:resume", intent="positive")],
              [Btn(text="Не сейчас", data="g:drop")]]
        target = session["chat_id"] or session["ext_id"]
        try:
            await channel.send(target, Out(text=text, kb=kb))
        except Exception:  # noqa: BLE001
            log.debug("Напоминание не доставлено: %s/%s", session["channel"], session["ext_id"])
        await repo.mark_reminded(session["channel"], session["ext_id"])
        await asyncio.sleep(0.3)
    return 300
