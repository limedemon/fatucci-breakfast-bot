"""Статусы заказа — согласованная с заказчиком цепочка."""
from __future__ import annotations

NEW = "new"              # заказ оформлен, ещё не оплачен
ACCEPTED = "accepted"    # менеджер принял в работу -> гостю уходит ссылка на оплату
PAID = "paid"            # оплачен
DELIVERED = "delivered"  # доставлен
RECEIVED = "received"    # гость подтвердил получение
REJECTED = "rejected"    # менеджер отклонил
CANCELLED = "cancelled"  # отменён гостем

LABELS: dict[str, str] = {
    NEW: "🆕 Не оплачен",
    ACCEPTED: "🔧 Принят в работу",
    PAID: "💰 Оплачен",
    DELIVERED: "🚚 Доставлен",
    RECEIVED: "✅ Получен",
    REJECTED: "⛔ Отклонён",
    CANCELLED: "❌ Отменён",
}

ORDER = [NEW, ACCEPTED, PAID, DELIVERED, RECEIVED, REJECTED, CANCELLED]

#: заказы, которые ещё «живые» — попадают в работу и в выгрузку курьерам
ACTIVE = (NEW, ACCEPTED, PAID, DELIVERED)
#: заказы, которые гость ещё может отменить сам
GUEST_CANCELLABLE = (NEW, ACCEPTED, PAID)
#: финальные состояния
FINAL = (RECEIVED, REJECTED, CANCELLED)


def label(status: str) -> str:
    return LABELS.get(status, status)


def next_actions(status: str) -> list[tuple[str, str]]:
    """Кнопки, которые видит менеджер для заказа в этом статусе: (код, подпись)."""
    if status == NEW:
        return [(ACCEPTED, "🔧 Принять в работу"), (REJECTED, "⛔ Отклонить")]
    if status == ACCEPTED:
        return [(PAID, "💰 Отметить оплаченным"), (REJECTED, "⛔ Отклонить")]
    if status == PAID:
        return [(DELIVERED, "🚚 Доставлен"), (REJECTED, "⛔ Отклонить")]
    if status == DELIVERED:
        return [(RECEIVED, "✅ Получен")]
    return []
