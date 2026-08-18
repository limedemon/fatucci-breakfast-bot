"""Фоновые задачи: выгрузка курьерам, напоминания и контроль оплат."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from . import courier, orders_service, payments, repo
from .channels.base import Btn, Out, get_channel
from .utils import fmt_date_iso, now, parse_time

log = logging.getLogger(__name__)


async def run_all() -> None:
    await asyncio.gather(
        _loop("courier", courier_tick, 60),
        _loop("reminders", reminder_tick, 300),
        _loop("payments", payment_tick, 20),
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


# ------------------------------------------------------------ контроль оплат
async def payment_tick() -> int:
    delay = max(10, await repo.get_int("pay_check_sec", 20))
    if not await payments.is_enabled():
        return 120
    orders = await repo.pending_payments()
    if not orders:
        return delay

    timeout_min = await repo.get_int("pay_timeout_min", 180)
    for order in orders:
        status = await payments.payment_status(order["payment_id"])
        if status == payments.STATUS_SUCCEEDED:
            log.info("Заказ %s оплачен", order["number"])
            await orders_service.apply_payment_success(order)
        elif status == payments.STATUS_CANCELED:
            await repo.update_order(order["id"], payment_id="", payment_url="")
            await repo.add_event(order["id"], order["status"], "ЮKassa", "Платёж отменён")
        elif _too_old(order["updated_at"], timeout_min):
            # ссылка протухла — очищаем, гость сможет запросить новую
            await repo.update_order(order["id"], payment_id="", payment_url="")
        await asyncio.sleep(0.2)
    return delay


def _too_old(updated_at: str, minutes: int) -> bool:
    try:
        moment = datetime.strptime(str(updated_at)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return datetime.utcnow() - moment > timedelta(minutes=minutes)
