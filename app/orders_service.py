"""Жизненный цикл заказа: смена статусов и все связанные с ней действия."""
from __future__ import annotations

import logging

import aiosqlite

from . import notify, payments, repo, statuses
from .channels.base import Btn
from .utils import fmt_money, now

log = logging.getLogger(__name__)
Row = aiosqlite.Row


async def change_status(
    order_id: int, new_status: str, actor: str = "", note: str = ""
) -> tuple[bool, str]:
    """Сменить статус заказа и выполнить всё, что к этому привязано."""
    order = await repo.get_order(order_id)
    if order is None:
        return False, "Заказ не найден"
    if order["status"] == new_status:
        return False, f"Заказ уже в статусе «{statuses.label(new_status)}»"

    await repo.set_status(order_id, new_status, actor=actor, note=note)
    if new_status == statuses.PAID and not order["paid_at"]:
        await repo.update_order(order_id, paid_at=now().strftime("%Y-%m-%d %H:%M:%S"))

    order = await repo.get_order(order_id)
    assert order is not None

    try:
        await _notify_guest(order, note)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось уведомить гостя по заказу %s", order["number"])
    try:
        await notify.refresh_order_cards(order)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось обновить карточки заказа %s", order["number"])

    return True, f"Статус заказа №{order['number']}: {statuses.label(new_status)}"


async def _notify_guest(order: Row, note: str = "") -> None:
    status = order["status"]

    if status == statuses.ACCEPTED:
        await _send_payment_request(order)
        return

    if status == statuses.PAID:
        text = await notify.guest_status_text(order, "status_paid")
        await notify.notify_guest(order, text)
        return

    if status == statuses.DELIVERED:
        text = await notify.guest_status_text(order, "status_delivered")
        kb = [[Btn(text="✅ Я получил заказ", data=f"g:got:{order['id']}", intent="positive")]]
        await notify.notify_guest(order, text, kb)
        return

    if status == statuses.RECEIVED:
        text = await notify.guest_status_text(order, "status_received")
        await notify.notify_guest(order, text, [[Btn(text="🥐 Заказать ещё", data="g:menu")]])
        return

    if status == statuses.REJECTED:
        reason = f"Причина: {note}" if note else ""
        text = await notify.guest_status_text(order, "status_rejected", reason=reason)
        await notify.notify_guest(order, text)
        return

    if status == statuses.CANCELLED:
        text = await notify.guest_status_text(order, "status_cancelled")
        await notify.notify_guest(order, text)


async def _send_payment_request(order: Row) -> None:
    """Заказ принят в работу — просим гостя оплатить."""
    payment_url, error = await ensure_payment_link(order)
    text = await notify.guest_status_text(order, "status_accepted")
    if payment_url:
        kb = [[Btn(text=f"💳 Оплатить {fmt_money(order['total_kop'])}", url=payment_url)]]
        kb.append([Btn(text="❌ Отменить заказ", data=f"g:cancel:{order['id']}", intent="negative")])
        await notify.notify_guest(order, text, kb)
        return

    fallback = await repo.render_text("payment_unavailable", number=order["number"])
    await notify.notify_guest(order, fallback)
    await notify.send_to_admins(
        f"⚠️ Заказ №{order['number']}: не удалось создать ссылку на оплату.\n"
        f"{error}\n\nПроверьте /admin → 💳 Оплата."
    )


async def ensure_payment_link(order: Row) -> tuple[str, str]:
    """Вернуть действующую ссылку на оплату, создав платёж при необходимости."""
    if not await payments.is_enabled():
        return "", "Онлайн-оплата выключена или не настроена."
    if order["payment_url"] and order["payment_id"]:
        status = await payments.payment_status(order["payment_id"])
        if status in ("", payments.STATUS_PENDING, payments.STATUS_WAITING):
            return order["payment_url"], ""
    payment_id, url, error = await payments.create_payment(order)
    if not url:
        return "", error
    await repo.update_order(order["id"], payment_id=payment_id, payment_url=url)
    return url, ""


async def apply_payment_success(order: Row) -> None:
    """Платёж подтверждён ЮKassa."""
    if order["status"] in (statuses.PAID, statuses.DELIVERED, statuses.RECEIVED):
        return
    await change_status(order["id"], statuses.PAID, actor="ЮKassa", note="Оплата подтверждена")


async def guest_cancel(order_id: int, ext_id: str, channel: str) -> tuple[bool, str]:
    """Отмена заказа самим гостем."""
    order = await repo.get_order(order_id)
    if order is None:
        return False, "Заказ не найден"
    if order["ext_id"] != str(ext_id) or order["channel"] != channel:
        return False, "Это не ваш заказ"
    if order["status"] not in statuses.GUEST_CANCELLABLE:
        return False, f"Заказ в статусе «{statuses.label(order['status'])}» отменить нельзя"
    await repo.set_status(order_id, statuses.CANCELLED, actor="гость", note="Отменён гостем")
    order = await repo.get_order(order_id)
    assert order is not None
    await notify.refresh_order_cards(order)
    await notify.send_to_admins(
        f"❌ Гость отменил заказ <b>№{order['number']}</b> "
        f"({order['object_title']}, апарт. {order['apartment']})."
    )
    return True, "Заказ отменён"


async def guest_confirm_received(order_id: int, ext_id: str, channel: str) -> tuple[bool, str]:
    order = await repo.get_order(order_id)
    if order is None:
        return False, "Заказ не найден"
    if order["ext_id"] != str(ext_id) or order["channel"] != channel:
        return False, "Это не ваш заказ"
    if order["status"] == statuses.RECEIVED:
        return False, "Заказ уже отмечен как полученный"
    ok, message = await change_status(order_id, statuses.RECEIVED, actor="гость")
    return ok, message
