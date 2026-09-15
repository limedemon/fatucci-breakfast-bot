"""Тестовая оплата из админки: проверить кассу на минимальной сумме.

Администратор платит сам себе — без заказа — и получает в этот же чат
уведомление, что платёж прошёл. Так видно, что касса и подтверждение
оплаты работают, ещё до первого гостя.

Два пути, как и у гостей:
    счёт в Telegram  — минимальная сумма, которую пропускает Telegram;
    ссылка ЮKassa    — 1 ₽, так платят гости из MAX. Её статус бот узнаёт
                       опросом (yookassa.watch_tick), а после оплаты даёт
                       вернуть деньги одной кнопкой.

Ждущие тестовые ссылки лежат в настройке pay_tests (JSON), к заказам
они отношения не имеют.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import payments, repo, yookassa
from .channels.base import TG, Btn, Channel, Event, Out, get_channel
from .utils import esc, fmt_money, utc_stamp

log = logging.getLogger(__name__)

INVOICE_PREFIX = "test:"
LINK_AMOUNT_KOP = 100          # минимальный платёж картой в ЮKassa — 1 ₽
STORE_KEY = "pay_tests"


# ------------------------------------------------------------------ экран
async def screen(ev: Event, ch: Channel) -> Out:
    invoice_ok = ch.name == TG and payments.token_looks_valid(await payments.provider_token())
    link_ok = await yookassa.is_configured()

    lines = ["🧪 <b>Тестовая оплата</b>", "",
             "Оплатите сами минимальную сумму — бот пришлёт сюда уведомление, "
             "как только платёж пройдёт. Так видно, что касса работает.", ""]
    kb: list[list[Btn]] = []
    if invoice_ok:
        token = await payments.provider_token()
        mode = "тестовая касса" if payments.is_test(token) else "⚠️ боевая касса — спишется по-настоящему"
        lines.append(f"💳 <b>Счёт в Telegram</b> — {fmt_money(payments.MIN_AMOUNT_KOP)}, "
                     f"меньше Telegram не пропускает ({mode}).")
        kb.append([Btn(text=f"💳 Счёт в Telegram · {fmt_money(payments.MIN_AMOUNT_KOP)}",
                       data="a:cfg:ptinv", intent="positive")])
    if link_ok:
        _, secret = await yookassa.credentials()
        mode = ("тестовый магазин" if yookassa.is_test_key(secret)
                else "⚠️ боевой магазин — спишется по-настоящему, вернуть можно кнопкой")
        lines.append(f"🔗 <b>Ссылка ЮKassa</b> — {fmt_money(LINK_AMOUNT_KOP)}, "
                     f"так платят гости из MAX ({mode}).")
        kb.append([Btn(text=f"🔗 Ссылка ЮKassa · {fmt_money(LINK_AMOUNT_KOP)}",
                       data="a:cfg:ptlink", intent="positive")])
    if not kb:
        lines.append("⚠️ Проверять нечего: не задан ни токен кассы Telegram, "
                     "ни ключи ЮKassa." if ch.name == TG else
                     "⚠️ Проверять нечего: в MAX картой платят через ЮKassa, "
                     "а её ключи не заданы.")
    elif ch.name != TG and payments.token_looks_valid(await payments.provider_token()):
        lines += ["", "<i>Счёт в Telegram проверяется из админки в Telegram.</i>"]
    kb.append([Btn(text="⬅️ К оплате", data="a:cfg:s:pay"), Btn(text="🏠 Админка", data="a:h")])
    return Out(text="\n".join(lines), kb=kb)


# --------------------------------------------------------- счёт Telegram
async def send_invoice(ev: Event, ch: Channel) -> str:
    """Выставить тестовый счёт администратору. → текст ответа"""
    token = await payments.provider_token()
    if ch.name != TG or not payments.token_looks_valid(token):
        return "⚠️ Счёт в Telegram недоступен: нет токена кассы."
    ok, error = await ch.send_invoice(
        chat_id=ev.chat_id,
        title="Тестовая оплата",
        description="Проверка кассы бота Fatucci — оплата минимальной суммы",
        payload=f"{INVOICE_PREFIX}{ev.user_id}",
        amount_kop=payments.MIN_AMOUNT_KOP,
        provider_token=token,
        label="Проверка кассы",
        provider_data=await payments.provider_data(),
    )
    if not ok:
        return (f"⚠️ <b>Telegram не выставил счёт</b>\n\n<code>{esc(error)}</code>\n\n"
                "Проверьте токен кассы в @BotFather. Если ошибка про сумму — "
                "у кассы минимум выше, напишите разработчику.")
    return ("⬆️ <b>Счёт отправлен</b>\n\nОплатите его — как только Telegram подтвердит "
            "платёж, здесь появится уведомление.")


def is_test_payload(payload: str) -> bool:
    return (payload or "").startswith(INVOICE_PREFIX)


async def telegram_paid(ev: Event, ch: Channel) -> None:
    """Telegram подтвердил тестовый счёт."""
    amount = int(ev.raw.get("amount") or payments.MIN_AMOUNT_KOP)
    charge = str(ev.raw.get("charge_id", ""))
    log.info("Тестовая оплата Telegram прошла: %s коп., %s", amount, charge)
    await ch.send(ev.chat_id, Out(
        text="✅ <b>Тестовая оплата прошла</b>\n\n"
             f"Касса Telegram приняла {fmt_money(amount)}"
             + (f"\nПлатёж: <code>{esc(charge)}</code>" if charge else "") + "\n\n"
             "Счёт картой работает: гостю заказ отметится оплаченным так же, сам.\n"
             "Деньги пришли в кассу — вернуть их можно в её личном кабинете.",
        kb=[[Btn(text="🧪 Ещё проверка", data="a:cfg:ptest"),
             Btn(text="🏠 Админка", data="a:h")]]))


# ----------------------------------------------------------- ссылка ЮKassa
async def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(await repo.get_setting(STORE_KEY) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


async def _save(data: dict[str, dict[str, Any]]) -> None:
    await repo.set_setting(STORE_KEY, json.dumps(data, ensure_ascii=False))


async def _receipt(ev: Event) -> dict[str, Any] | None:
    """Чек нужен, если магазин отправляет чеки через ЮKassa."""
    if not await repo.get_bool("yk_receipt", False):
        return None
    user = await repo.get_user(ev.channel, str(ev.user_id))
    phone = yookassa._phone(user["phone"] if user else "")
    if not phone:
        return None
    return {"customer": {"phone": phone}, "items": [{
        "description": "Проверка оплаты",
        "quantity": "1.00",
        "amount": {"value": yookassa._rub(LINK_AMOUNT_KOP), "currency": "RUB"},
        "vat_code": await repo.get_int("yk_vat_code", 1),
        "payment_mode": "full_payment",
        "payment_subject": "service",
    }]}


async def send_link(ev: Event, ch: Channel) -> Out:
    """Создать тестовую ссылку ЮKassa на 1 ₽."""
    if not await yookassa.is_configured():
        return Out(text="⚠️ Ключи ЮKassa не заданы.", kb=[[Btn(text="⬅️ К оплате",
                                                                 data="a:cfg:s:pay")]])
    body: dict[str, Any] = {
        "amount": {"value": yookassa._rub(LINK_AMOUNT_KOP), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": ch.start_link()},
        "description": "Тестовая оплата — проверка кассы бота",
        "metadata": {"test": "1"},
    }
    receipt = await _receipt(ev)
    if receipt:
        body["receipt"] = receipt
    ok, data = await yookassa._request("POST", "/payments", body)
    url = str((data.get("confirmation") or {}).get("confirmation_url", "")) if ok else ""
    if not ok or not url:
        error = data.get("error", "ЮKassa не вернула ссылку")
        hint = ("\n\nВ магазине включены чеки, а для чека нужен ваш телефон: "
                "поделитесь им с ботом, оформив заказ, или временно выключите чеки."
                if "receipt" in str(error).lower() else "")
        return Out(text=f"⚠️ <b>ЮKassa не создала платёж</b>\n\n<code>{esc(str(error))}</code>{hint}",
                   kb=[[Btn(text="⬅️ К оплате", data="a:cfg:s:pay")]])

    tests = await _load()
    tests[str(data["id"])] = {"channel": ev.channel, "chat_id": str(ev.chat_id),
                              "amount": LINK_AMOUNT_KOP, "created": utc_stamp(),
                              "receipt": bool(receipt)}
    await _save(tests)
    return Out(
        text="🔗 <b>Тестовая ссылка готова</b>\n\n"
             f"Оплатите {fmt_money(LINK_AMOUNT_KOP)} по кнопке. Бот проверяет ЮKassa "
             "каждые 20 секунд — уведомление придёт сюда, обычно в течение минуты.",
        kb=[[Btn(text=f"💳 Оплатить {fmt_money(LINK_AMOUNT_KOP)}", url=url)],
            [Btn(text="⬅️ К оплате", data="a:cfg:s:pay")]])


async def watch() -> int:
    """Проверить ждущие тестовые ссылки. → сколько ещё ждут."""
    tests = await _load()
    if not tests:
        return 0
    changed = False
    for payment_id, info in list(tests.items()):
        status, data = await yookassa.get_payment(payment_id)
        if status not in (yookassa.SUCCEEDED, yookassa.CANCELED):
            continue
        channel = get_channel(info.get("channel", TG))
        tests.pop(payment_id)
        changed = True
        if channel is None:
            continue
        amount = int(info.get("amount") or LINK_AMOUNT_KOP)
        if status == yookassa.SUCCEEDED:
            log.info("Тестовая оплата ЮKassa прошла: %s", payment_id)
            method = (data.get("payment_method") or {}).get("title") or ""
            await channel.send(info["chat_id"], Out(
                text="✅ <b>Тестовая оплата прошла</b>\n\n"
                     f"ЮKassa приняла {fmt_money(amount)}"
                     + (f" · {esc(method)}" if method else "") + "\n"
                     f"Платёж: <code>{esc(payment_id)}</code>\n\n"
                     "Ссылки на оплату работают: заказ гостя отметится оплаченным "
                     "так же, сам.",
                kb=[[Btn(text=f"↩️ Вернуть {fmt_money(amount)}",
                         data=f"a:cfg:ptr:{payment_id}")],
                    [Btn(text="🧪 Ещё проверка", data="a:cfg:ptest"),
                     Btn(text="🏠 Админка", data="a:h")]]))
        else:
            reason = (data.get("cancellation_details") or {}).get("reason", "")
            await channel.send(info["chat_id"], Out(
                text="❌ <b>Тестовая оплата не прошла</b>\n\n"
                     f"ЮKassa отменила платёж: <code>{esc(reason or 'без причины')}</code>.\n"
                     "Чаще всего ссылку просто не оплатили вовремя — попробуйте ещё раз.",
                kb=[[Btn(text="🧪 Ещё проверка", data="a:cfg:ptest")]]))
    if changed:
        await _save(tests)
    return len(tests)


async def refund(payment_id: str, ev: Event) -> str:
    """Вернуть деньги за тестовый платёж ЮKassa."""
    status, data = await yookassa.get_payment(payment_id)
    if status != yookassa.SUCCEEDED or (data.get("metadata") or {}).get("test") != "1":
        return "⚠️ Вернуть можно только оплаченный тестовый платёж."
    amount = data.get("amount") or {"value": yookassa._rub(LINK_AMOUNT_KOP), "currency": "RUB"}
    body: dict[str, Any] = {"payment_id": payment_id, "amount": amount}
    receipt = await _receipt(ev)
    if receipt:
        body["receipt"] = receipt
    ok, answer = await yookassa._request("POST", "/refunds", body)
    if not ok:
        return f"⚠️ <b>Возврат не оформлен</b>\n\n<code>{esc(str(answer.get('error')))}</code>"
    return ("↩️ <b>Возврат оформлен</b>\n\n"
            f"{amount.get('value')} ₽ вернутся на карту — обычно за несколько минут, "
            "иногда банк держит до нескольких дней.")
