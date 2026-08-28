"""Оплата заказа. Два способа, бот выбирает подходящий сам.

**По реквизитам** — рабочий вариант, пока касса не подключена. Менеджер
подтверждает заказ, гость получает сумму, реквизиты и кнопку «Я оплатил».
Нажал — менеджерам приходит сообщение с кнопками «Подтвердить оплату»
и «Оплата не пришла». Реквизиты правятся в ✍️ Тексты бота.

**Счётом в Telegram** — если в админке задан токен кассы от @BotFather
(/mybots → бот → Payments). Тогда гость платит картой в пару касаний,
а оплату подтверждает сам Telegram, без участия менеджера.

Пока не настроено ни то, ни другое, бот не даёт оформить заказ: гость видит
сообщение, что приём заказов временно недоступен. Так не появляется заказов,
за которые нечем заплатить.
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
    """Можно ли выставить встроенный счёт Telegram.

    Переключатель «Оплата по реквизитам» перебивает кассу: токен остаётся
    в настройках, но счёт не выставляется — гость платит переводом.
    """
    if not await is_enabled():
        return False
    if await repo.get_bool("pay_by_details", False):
        return False
    return token_looks_valid(await provider_token())


# --------------------------------------------------------------- реквизиты
async def details_text() -> str:
    """Реквизиты, которые видит гость: перевод по номеру, ссылка и т. п."""
    details = (await repo.get_text("pay_details", "")).strip()
    if not details:
        details = await repo.render_text("pay_details_default")
    link = (await repo.get_setting("pay_link")).strip()
    if link:
        details += f"\n\nСсылка для оплаты: {link}"
    return details


async def details_configured() -> bool:
    """Заданы ли реквизиты — по ним гость платит, если кассы нет."""
    if not await is_enabled():
        return False
    return bool((await repo.get_text("pay_details", "")).strip()
                or (await repo.get_setting("pay_link")).strip())


async def available() -> bool:
    """Можно ли вообще принять оплату — от этого зависит, откроется ли заказ."""
    return await invoice_available() or await details_configured()


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
    if token and await repo.get_bool("pay_by_details", False):
        if await details_configured():
            return True, (
                "✅ <b>Оплата по реквизитам</b>\n\n"
                "Токен кассы сохранён, но выключен переключателем "
                "<b>«Оплата по реквизитам»</b> — гость платит переводом "
                "и нажимает «Я оплатил», а вы подтверждаете поступление кнопкой.\n\n"
                "Выключите переключатель, когда захотите вернуться к счетам "
                "в Telegram — токен вводить заново не придётся.\n\n"
                "Сейчас гость видит это:\n\n" + await details_text()
            )
        return False, (
            "⚠️ <b>Реквизиты не заполнены — заказы не принимаются</b>\n\n"
            "Включён переключатель «Оплата по реквизитам», но сами реквизиты "
            "пустые. Заполните текст <b>«Реквизиты для оплаты»</b> в разделе "
            "✍️ Тексты бота — или выключите переключатель, тогда заработает "
            "счёт от подключённой кассы."
        )

    if not token:
        if await details_configured():
            return True, (
                "✅ <b>Оплата по реквизитам</b>\n\n"
                "Касса не подключена, поэтому после подтверждения заказа гость "
                "получает реквизиты и кнопку «Я оплатил». Когда он её нажмёт, "
                "вам придёт сообщение с кнопками <b>«Подтвердить оплату»</b> "
                "и <b>«Оплата не пришла»</b>.\n\n"
                "Сейчас гость видит это:\n\n" + await details_text()
            )
        return False, (
            "⚠️ <b>Оплата не настроена — заказы не принимаются</b>\n\n"
            "Гость видит сообщение, что заказ пока оформить нельзя. "
            "Годится любой из двух способов.\n\n"
            "<b>Проще всего — реквизиты.</b> Заполните текст "
            "<b>«Реквизиты для оплаты»</b> в разделе ✍️ Тексты бота: например, "
            "перевод по номеру телефона. Гость получит их и кнопку «Я оплатил», "
            "а вы — подтвердите поступление кнопкой.\n\n"
            "<b>Или касса</b> — тогда счёт приходит гостю в чат и подтверждается "
            "сам: @BotFather → /mybots → бот → Payments → скопировать токен "
            "в поле выше."
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
