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
from .utils import fmt_date, fmt_money

log = logging.getLogger(__name__)
Row = Any

#: Telegram не пропускает совсем мелкие суммы
MIN_AMOUNT_KOP = 6000

#: Способы оплаты. «Оба» — гость сам решает: картой по счёту или переводом.
INVOICE, DETAILS, BOTH = "invoice", "details", "both"
MODES = {
    INVOICE: "💳 Только счёт картой",
    DETAILS: "🏦 Только перевод по реквизитам",
    BOTH: "💳🏦 Оба способа — гость выбирает",
}


async def mode() -> str:
    """Какой способ оплаты сейчас включён.

    Пустая настройка означает «как было раньше»: до появления режимов
    способ задавался переключателем «платить переводом».
    """
    value = (await repo.get_setting("pay_mode", "")).strip().lower()
    if value in MODES:
        return value
    return DETAILS if await repo.get_bool("pay_by_details", False) else INVOICE


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

    Режим «только перевод» кассу не отключает: токен остаётся в настройках,
    просто счёт не выставляется, пока режим не переключат обратно.
    """
    if not await is_enabled():
        return False
    if await mode() == DETAILS:
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


async def details_offered() -> bool:
    """Показываем ли гостю реквизиты сами, а не только как запасной путь."""
    return await details_configured() and await mode() in (DETAILS, BOTH)


async def available(channel: str = "tg") -> bool:
    """Можно ли вообще принять оплату — от этого зависит, откроется ли заказ.

    В MAX встроенных счетов нет, поэтому там оплата возможна только
    по реквизитам — касса гостю из MAX не поможет.
    """
    if channel != "tg":
        return await details_configured()
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
    current = await mode()

    if current == BOTH:
        if not token_looks_valid(token) or not await details_configured():
            missing = []
            if not token_looks_valid(token):
                missing.append("токен кассы в поле выше")
            if not await details_configured():
                missing.append("текст «Реквизиты для оплаты» в ✍️ Тексты бота")
            return False, (
                "⚠️ <b>Для режима «Оба способа» не хватает настроек</b>\n\n"
                "Заполните: " + ", ".join(missing) + ".\n\n"
                "Или переключите способ оплаты на тот, что уже настроен."
            )
        return True, (
            "✅ <b>Оба способа — гость выбирает</b>\n\n"
            "После подтверждения заказа гость получает реквизиты для перевода "
            "с кнопкой «Я оплатил» и счёт картой следующим сообщением. "
            "Оплату картой подтверждает Telegram, перевод — вы кнопкой.\n\n"
            "Счёт не выставляется, если сумма меньше "
            f"{fmt_money(MIN_AMOUNT_KOP)} — такой заказ уйдёт только переводом.\n\n"
            "Сейчас гость видит это:\n\n" + await details_text() + await _too_cheap_hint()
        )

    if current == DETAILS:
        if await details_configured():
            saved = ("\n\nТокен кассы сохранён — переключите способ оплаты, "
                     "чтобы вернуться к счетам, вводить заново не придётся."
                     if token else "")
            return True, (
                "✅ <b>Оплата по реквизитам</b>\n\n"
                "Гость платит переводом и нажимает «Я оплатил», а вы "
                "подтверждаете поступление кнопкой." + saved + "\n\n"
                "Сейчас гость видит это:\n\n" + await details_text()
            )
        return False, (
            "⚠️ <b>Реквизиты не заполнены — заказы не принимаются</b>\n\n"
            "Выбран способ «Только перевод по реквизитам», но сами реквизиты "
            "пустые. Заполните текст <b>«Реквизиты для оплаты»</b> в разделе "
            "✍️ Тексты бота — или переключите способ оплаты на счёт."
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

    small = await _too_cheap_hint()
    if is_test(token):
        return True, (
            "✅ <b>Касса подключена — тестовый режим</b>\n\n"
            "Гости могут оформлять заказы: после вашего подтверждения счёт "
            "приходит гостю сам.\n\n"
            "Деньги не списываются. Тестовая карта:\n"
            "<code>4111 1111 1111 1111</code>, срок — любой будущий, CVC любой.\n\n"
            "⚠️ В тестовом режиме Telegram показывает счёт не всем — проверяйте "
            "на своём аккаунте.\n\n"
            "Для настоящих платежей получите у @BotFather токен LIVE." + small
        )

    return True, (
        "✅ <b>Касса подключена — боевой режим</b>\n\n"
        "Деньги списываются по-настоящему, оплата подтверждается автоматически.\n"
        "Проверьте на небольшой сумме — например, оформите заказ на себя." + small
    )


async def _too_cheap_hint() -> str:
    """Предупредить, если цены ниже минимальной суммы счёта.

    Telegram не пропускает совсем мелкие платежи. Такой заказ уходит на оплату
    переводом — и это выглядит как «касса не работает», хотя дело в сумме.
    """
    prices = [int(row["price_kop"] or 0)
              for row in await repo.list_objects(active_only=True)]
    prices += [int(row["price_kop"] or 0) for row in await repo.list_sets(active_only=True)]
    low = [price for price in prices if 0 < price < MIN_AMOUNT_KOP]
    if not low:
        return ""
    return (f"\n\n⚠️ Есть цены ниже {fmt_money(MIN_AMOUNT_KOP)} "
            f"(минимум — {fmt_money(min(low))}). Счёт на такую сумму Telegram "
            "не примет: по таким заказам гость получит реквизиты для перевода.")
