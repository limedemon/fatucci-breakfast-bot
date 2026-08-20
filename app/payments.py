"""Оплата через PayMaster — нативными платежами Telegram.

Как это работает:

1. Токен провайдера выдаёт @BotFather: /mybots → бот → Payments → PayMaster.
   Формат токена — ``<id>:TEST:<hash>`` или ``<id>:LIVE:<hash>``.
2. Когда менеджер принимает заказ в работу, бот отправляет гостю **инвойс** —
   сообщение со встроенной кнопкой оплаты.
3. Телеграм спрашивает бота, всё ли в порядке (pre_checkout_query) — отвечаем «да».
4. После оплаты приходит successful_payment, и заказ автоматически становится
   «оплачен». Опрашивать ничего не нужно, вебхуки и белый IP тоже не нужны.

Важно: нативные платежи есть только в Telegram. В MAX их нет — там гость
получает сообщение, что с оплатой поможет менеджер (см. orders_service).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import repo
from .config import cfg
from .utils import fmt_date

log = logging.getLogger(__name__)
Row = Any

#: минимальная сумма платежа в копейках — Telegram не пропускает совсем мелкие
MIN_AMOUNT_KOP = 6000


async def provider_token() -> str:
    """Токен провайдера: сначала админ-панель, потом переменная окружения."""
    token = (await repo.get_setting("pm_token")) or cfg.provider_token
    return token.strip()


async def is_configured() -> bool:
    return bool(await provider_token())


async def is_enabled() -> bool:
    return await repo.get_bool("pay_enabled", True) and await is_configured()


def is_test(token: str) -> bool:
    return ":TEST:" in token.upper()


def token_looks_valid(token: str) -> bool:
    parts = token.split(":")
    return len(parts) == 3 and parts[0].isdigit() and parts[1].upper() in ("TEST", "LIVE")


async def mode_label() -> str:
    token = await provider_token()
    if not token:
        return "не настроена"
    return "тестовый режим" if is_test(token) else "боевой режим"


def invoice_payload(order_id: int) -> str:
    return f"order:{order_id}"


def parse_payload(payload: str) -> Optional[int]:
    if not payload.startswith("order:"):
        return None
    tail = payload.split(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def invoice_title(order: Row) -> str:
    """Заголовок инвойса — Telegram разрешает до 32 символов."""
    return f"Заказ №{order['number']}"[:32]


def invoice_description(order: Row) -> str:
    """Описание — до 255 символов."""
    parts = [
        f"{order['set_title']} × {order['qty']}",
        fmt_date(order["delivery_date"], with_weekday=False),
    ]
    if order["object_address"]:
        parts.append(f"{order['object_address']}, апарт. {order['apartment']}")
    else:
        parts.append(f"апарт. {order['apartment']}")
    return " · ".join(parts)[:255]


async def provider_data() -> str:
    """Необязательный JSON для провайдера (например, чек по 54-ФЗ).

    Формат задаёт PayMaster, поэтому строку не собираем сами, а отдаём как есть —
    её можно вписать в админ-панели, если этого требует ваша схема.
    """
    return (await repo.get_setting("pm_provider_data")).strip()


async def check_setup() -> tuple[bool, str]:
    """Диагностика для кнопки «Проверить оплату» в админ-панели."""
    token = await provider_token()
    if not token:
        return False, (
            "Токен не задан.\n\n"
            "Получить: @BotFather → /mybots → ваш бот → Payments → PayMaster.\n"
            "Затем вставьте выданный токен в это поле."
        )
    if not token_looks_valid(token):
        return False, (
            "Это не похоже на токен провайдера.\n\n"
            "Он выглядит так: <code>123456789:TEST:abcdef…</code> — "
            "число, слово TEST или LIVE и хеш через двоеточия.\n"
            "Выдаёт его @BotFather в разделе Payments, а не личный кабинет PayMaster."
        )
    if is_test(token):
        return True, (
            "✅ Токен принят — <b>тестовый режим</b>.\n\n"
            "Деньги не списываются, платить нужно тестовой картой "
            "<code>4111 1111 1111 1111</code>, срок — любой будущий, CVC любой.\n\n"
            "⚠️ В тестовом режиме Telegram присылает счёт только тем, кто есть "
            "во взаимных контактах у владельца бота. Для проверки добавьте "
            "тестировщика в контакты.\n\n"
            "Для приёма настоящих денег получите у @BotFather токен LIVE."
        )
    return True, (
        "✅ Токен принят — <b>боевой режим</b>.\n\n"
        "Деньги будут списываться по-настоящему. Проверьте на маленькой сумме."
    )
