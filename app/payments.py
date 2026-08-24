"""Оплата заказа — встроенным счётом Telegram.

Токен платёжного провайдера выдаёт @BotFather: /mybots → бот → Payments →
подключённая касса. Токен вписывается в админке (⚙️ Настройки → 💳 Оплата)
и выглядит так: 123456789:TEST:abcdef… — номер, слово TEST или LIVE и хеш.

Пока токен не задан, бот не даёт оформить заказ: гость видит сообщение, что
приём заказов временно недоступен. Так не появляется заказов, за которые
нечем заплатить.

Когда токен есть, всё происходит само: менеджер подтверждает заказ — гостю
приходит счёт, он платит картой, Telegram сообщает боту об оплате, и заказ
переходит в статус «Оплачен» без участия менеджера.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import repo
from .config import cfg
from .utils import fmt_date

log = logging.getLogger(__name__)
Row = Any

#: Telegram не пропускает совсем мелкие суммы
MIN_AMOUNT_KOP = 6000


# --------------------------------------------------------- токен провайдера
async def provider_token() -> str:
    """Токен из админки, иначе из переменной окружения."""
    token = (await repo.get_setting("pm_token")) or cfg.provider_token
    return token.strip()


def is_test(token: str) -> bool:
    return ":TEST:" in token.upper()


def token_looks_valid(token: str) -> bool:
    """Грубая проверка формы токена — чтобы отловить опечатку сразу."""
    parts = token.split(":")
    return len(parts) == 3 and parts[0].isdigit() and parts[1].upper() in ("TEST", "LIVE")


async def is_enabled() -> bool:
    return await repo.get_bool("pay_enabled", True)


async def invoice_available() -> bool:
    """Можно ли принять оплату — от этого зависит, откроется ли оформление."""
    if not await is_enabled():
        return False
    return token_looks_valid(await provider_token())


# ------------------------------------------------------------------- счёт
def invoice_payload(order_id: int) -> str:
    return f"order:{order_id}"


def parse_payload(payload: str) -> Optional[int]:
    if not payload.startswith("order:"):
        return None
    tail = payload.split(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def invoice_title(order: Row) -> str:
    """Заголовок счёта — Telegram разрешает до 32 символов."""
    return f"Заказ №{order['group_key'] or order['number']}"[:32]


def invoice_description(orders: list[Row]) -> str:
    """Описание счёта — до 255 символов, с разбивкой по датам."""
    parts = [
        f"{fmt_date(o['delivery_date'], with_weekday=False)}: "
        f"{o['set_title'] or 'сет дня'} × {o['qty']}"
        for o in orders
    ]
    head = orders[0]
    tail = f" · апарт. {head['apartment']}"
    return (" · ".join(parts) + tail)[:255]


async def provider_data() -> str:
    """Доп. данные для кассы (например, чек по 54-ФЗ) — обычно не нужны."""
    return (await repo.get_setting("pm_provider_data")).strip()


# -------------------------------------------------------------- диагностика
async def check_setup() -> tuple[bool, str]:
    """Что показывает кнопка «Проверить оплату» в админ-панели."""
    if not await is_enabled():
        return False, (
            "⛔️ <b>Приём оплаты выключен</b>\n\n"
            "Пока переключатель выше выключен, гости не могут оформить заказ.\n"
            "Включите его, когда будете готовы принимать оплату."
        )

    token = await provider_token()
    if not token:
        return False, (
            "⚠️ <b>Касса не подключена — заказы не принимаются</b>\n\n"
            "Гость видит сообщение, что заказ пока оформить нельзя.\n\n"
            "Где взять токен:\n"
            "1. Откройте @BotFather\n"
            "2. /mybots → выберите бота → Payments\n"
            "3. Выберите кассу и подключите её\n"
            "4. Скопируйте выданный токен и впишите его в поле выше\n\n"
            "Для проверки подойдёт токен в режиме TEST — деньги не списываются."
        )

    if not token_looks_valid(token):
        return False, (
            "⚠️ <b>Токен не похож на настоящий</b>\n\n"
            "Правильный вид: <code>123456789:TEST:abcdef…</code> — номер, "
            "слово TEST или LIVE и хеш через двоеточия.\n\n"
            "Скопируйте токен заново: @BotFather → /mybots → бот → Payments."
        )

    if is_test(token):
        return True, (
            "✅ <b>Касса подключена — тестовый режим</b>\n\n"
            "Гости могут оформлять заказы: после вашего подтверждения счёт "
            "приходит гостю сам.\n\n"
            "Деньги не списываются. Тестовая карта:\n"
            "<code>4111 1111 1111 1111</code>, срок — любой будущий, CVC любой.\n\n"
            "⚠️ В тестовом режиме Telegram показывает счёт не всем — проверяйте "
            "на своём аккаунте.\n\n"
            "Для настоящих платежей получите у @BotFather токен LIVE."
        )

    return True, (
        "✅ <b>Касса подключена — боевой режим</b>\n\n"
        "Деньги списываются по-настоящему, оплата подтверждается автоматически.\n"
        "Проверьте на небольшой сумме — например, оформите заказ на себя."
    )
