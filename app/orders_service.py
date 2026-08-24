"""Жизненный цикл заказа: смена статусов и все связанные с ней действия.

Заказ может охватывать несколько дат — в базе это отдельные строки с общим
номером (group_key). Менеджер работает с заказом целиком: подтвердил, отметил
оплату — это применяется ко всем дням сразу. Доставка и отмена возможны
и по одному дню: ежедневная выгрузка курьерам смотрит на каждую строку
отдельно.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import notify, payments, repo, statuses
from .channels.base import Btn, get_channel
from .utils import fmt_date, now, parse_date, parse_time, utc_stamp

log = logging.getLogger(__name__)
Row = Any

#: статусы, которые менеджер меняет сразу для всего заказа
GROUP_STATUSES = (statuses.ACCEPTED, statuses.PAID, statuses.REJECTED, statuses.CANCELLED)


async def change_status(
    order_id: int, new_status: str, actor: str = "", note: str = ""
) -> tuple[bool, str]:
    """Сменить статус заказа. Подтверждение, оплата и отказ — на весь заказ."""
    order = await repo.get_order(order_id)
    if order is None:
        return False, "Заказ не найден"

    group = await repo.group_of(order)
    targets = group if new_status in GROUP_STATUSES else [order]
    changed = [row for row in targets
               if row["status"] != new_status and row["status"] != statuses.CANCELLED]
    if not changed:
        return False, f"Заказ уже в статусе «{statuses.label(new_status)}»"

    stamp = utc_stamp()
    for row in changed:
        await repo.set_status(row["id"], new_status, actor=actor, note=note)
        if new_status == statuses.PAID and not row["paid_at"]:
            await repo.update_order(row["id"], paid_at=stamp)

    order = await repo.get_order(order_id)
    group = await repo.group_of(order)

    try:
        await _notify_guest(group, order, new_status, note)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось уведомить гостя по заказу %s", order["number"])
    try:
        await notify.refresh_order_cards(order)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось обновить карточки заказа %s", order["number"])

    number = order["group_key"] or order["number"]
    return True, f"Заказ №{number}: {statuses.label(new_status)}"


async def _notify_guest(group: list[Row], order: Row, status: str, note: str = "") -> None:
    active = [row for row in group if row["status"] != statuses.CANCELLED] or group

    if status == statuses.ACCEPTED:
        await _send_payment_request(active)
        return

    if status == statuses.PAID:
        text = await notify.group_status_text(active, "status_paid")
        await notify.notify_guest(order, text, [[Btn(text="📦 Мои заказы", data="g:my")]])
        return

    if status == statuses.DELIVERED:
        text = await notify.group_status_text([order], "status_delivered")
        kb = [[Btn(text="✅ Я получил заказ", data=f"g:got:{order['id']}", intent="positive")]]
        await notify.notify_guest(order, text, kb)
        return

    if status == statuses.RECEIVED:
        text = await notify.group_status_text([order], "status_received")
        await notify.notify_guest(order, text, [[Btn(text="🥐 Заказать ещё", data="g:order")]])
        return

    if status == statuses.REJECTED:
        reason = f"Причина: {note}" if note else ""
        text = await notify.group_status_text(group, "status_rejected", reason=reason)
        await notify.notify_guest(order, text)
        return

    if status == statuses.CANCELLED:
        text = await notify.group_status_text([order], "status_cancelled")
        await notify.notify_guest(order, text)


# ------------------------------------------------------------------- оплата
async def _send_payment_request(group: list[Row]) -> None:
    """Заказ подтверждён — выставляем счёт.

    Оплата принимается только через подключённую кассу: гость получает
    встроенный счёт Telegram, и оплату подтверждает сам Telegram. Если счёт
    выставить не удалось, зовём менеджера — гостя без ответа не оставляем.
    """
    head = group[0]
    total = notify.group_total(group)
    number = head["group_key"] or head["number"]

    if await payments.invoice_available():
        text = await notify.group_status_text(
            group, "status_accepted", pay_details=await repo.render_text("pay_by_invoice"))
        await notify.notify_guest(head, text)
        if await _send_invoice(group, total):
            return
        log.warning("Счёт по заказу %s не выставлен", number)

    # касса не подключена или счёт не ушёл — гостя не бросаем, зовём менеджера
    await notify.notify_guest(head, await repo.render_text(
        "payment_unavailable", number=number))
    await notify.send_to_admins(
        f"⚠️ Заказ <b>№{number}</b>: счёт на оплату выставить не удалось.\n"
        "Проверьте /admin → 💳 Оплата и свяжитесь с гостем."
    )


async def _send_invoice(group: list[Row], total: int) -> bool:
    """Встроенный счёт Telegram на весь заказ."""
    head = group[0]
    channel = get_channel(head["channel"])
    if channel is None or total < payments.MIN_AMOUNT_KOP:
        return False
    ok, error = await channel.send_invoice(
        chat_id=head["chat_id"] or head["ext_id"],
        title=payments.invoice_title(head),
        description=payments.invoice_description(group),
        payload=payments.invoice_payload(head["id"]),
        amount_kop=total,
        provider_token=await payments.provider_token(),
        label=f"Завтраки × {sum(o['qty'] for o in group)}"[:32],
        provider_data=await payments.provider_data(),
    )
    if ok:
        await repo.add_event(head["id"], head["status"], "бот", "Выставлен счёт на оплату")
    elif error:
        log.warning("send_invoice: %s", error)
    return ok


# -------------------------------------------------------- отмена и получение
async def cancel_deadline_for(day: str | Any) -> Optional[Any]:
    """Момент, до которого гость может отменить доставку на эту дату."""
    from datetime import datetime, timedelta

    from .config import cfg

    parsed = parse_date(day) if isinstance(day, str) else day
    if parsed is None:
        return None
    limit = parse_time(await repo.get_setting("cancel_deadline", "18:30"), "18:30")
    return datetime.combine(parsed - timedelta(days=1), limit, tzinfo=cfg.tz)


async def refund_allowed(order: Row) -> bool:
    """Возвращаются ли деньги при отмене прямо сейчас."""
    deadline = await cancel_deadline_for(order["delivery_date"])
    return deadline is None or now() <= deadline


async def guest_cancel(order_id: int, ext_id: str, channel: str,
                       whole_order: bool = False) -> tuple[bool, str]:
    """Отмена заказа гостем: одной даты или всего заказа."""
    order = await repo.get_order(order_id)
    if order is None:
        return False, "Заказ не найден"
    if order["ext_id"] != str(ext_id) or order["channel"] != channel:
        return False, "Это не ваш заказ"

    group = await repo.group_of(order)
    targets = group if whole_order else [order]
    targets = [row for row in targets if row["status"] in statuses.GUEST_CANCELLABLE]
    if not targets:
        return False, f"Заказ в статусе «{statuses.label(order['status'])}» отменить нельзя"

    no_refund = [row for row in targets if not await refund_allowed(row)]
    for row in targets:
        await repo.set_status(row["id"], statuses.CANCELLED, actor="гость",
                              note="Отменён гостем")

    order = await repo.get_order(order_id)
    await notify.refresh_order_cards(order)
    number = order["group_key"] or order["number"]
    dates = ", ".join(fmt_date(row["delivery_date"], False) for row in targets)
    warn = ""
    if no_refund:
        late = ", ".join(fmt_date(row["delivery_date"], False) for row in no_refund)
        warn = f"\n⚠️ Отмена после срока — {late}: оплата за эти дни не возвращается."
    await notify.send_to_admins(
        f"❌ Гость отменил заказ <b>№{number}</b> ({dates}).\n"
        f"{order['object_title']}, апарт. {order['apartment']}.{warn}"
    )

    message = f"Отменено: {dates}."
    if no_refund:
        deadline = await repo.get_setting("cancel_deadline", "18:30")
        message += (f"\n\n⚠️ Отмена после {deadline} накануне — за ближайшую доставку "
                    "оплата не возвращается. По остальным дням вернём полностью.")
    return True, message


async def guest_confirm_received(order_id: int, ext_id: str, channel: str) -> tuple[bool, str]:
    order = await repo.get_order(order_id)
    if order is None:
        return False, "Заказ не найден"
    if order["ext_id"] != str(ext_id) or order["channel"] != channel:
        return False, "Это не ваш заказ"
    if order["status"] == statuses.RECEIVED:
        return False, "Заказ уже отмечен как полученный"
    return await change_status(order_id, statuses.RECEIVED, actor="гость")


# ------------------------------------------------------ совместимость с кодом
async def apply_payment_success(order: Row, charge_id: str = "") -> None:
    """Telegram подтвердил оплату счёта — отмечаем весь заказ оплаченным."""
    if order["status"] in (statuses.PAID, statuses.DELIVERED, statuses.RECEIVED):
        return
    if charge_id:
        await repo.update_order(order["id"], payment_id=charge_id)
    await change_status(order["id"], statuses.PAID, actor="Telegram",
                        note="Оплата подтверждена платёжной системой")
