"""Оплата картой по ссылке ЮKassa — для MAX (и запасной путь в Telegram).

В MAX нет встроенных счетов, поэтому гость получает кнопку «Оплатить картой»:
она открывает страницу ЮKassa (карта, СБП, SberPay). Оплату подтверждает сама
ЮKassa — менеджеру ничего сверять не нужно.

Вебхуков нет намеренно: на bothost у бота нет публичного адреса. Статус
платежа бот узнаёт опросом (scheduler.card_payments_tick) — раз в несколько
секунд, пока ссылка не оплачена или не истекла.

Где что хранится: у головной строки заказа
    payment_id  = «yk:<id платежа>»
    payment_url = ссылка на оплату, пока платёж ждёт; пусто — платёж завершён.
Ключи магазина — только в базе (⚙️ Настройки → 💳 Оплата), в коде их нет.
"""
from __future__ import annotations

import base64
import logging
import uuid
from typing import Any, Optional

import aiohttp

from . import net, repo, statuses
from .channels.base import get_channel

log = logging.getLogger(__name__)
Row = Any

API_BASE = "https://api.yookassa.ru/v3"
PREFIX = "yk:"

SUCCEEDED, CANCELED, PENDING = "succeeded", "canceled", "pending"


# ------------------------------------------------------------------ ключи
async def credentials() -> tuple[str, str]:
    shop_id = (await repo.get_setting("yk_shop_id")).strip()
    secret = (await repo.get_setting("yk_secret")).strip()
    return shop_id, secret


async def is_configured() -> bool:
    shop_id, secret = await credentials()
    return bool(shop_id and secret)


def is_test_key(secret: str) -> bool:
    return secret.strip().startswith("test_")


def secret_looks_valid(secret: str) -> bool:
    return secret.strip().startswith(("live_", "test_")) and len(secret.strip()) > 10


# ---------------------------------------------------------------- запросы
async def _request(method: str, path: str,
                   body: Optional[dict[str, Any]] = None) -> tuple[bool, dict[str, Any]]:
    """Запрос к API ЮKassa. → (успех, ответ или {"error": текст})"""
    shop_id, secret = await credentials()
    if not (shop_id and secret):
        return False, {"error": "не заданы shopId и секретный ключ"}
    token = base64.b64encode(f"{shop_id}:{secret}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if method == "POST":
        # повтор того же запроса не создаст второй платёж
        headers["Idempotence-Key"] = str(uuid.uuid4())
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            connector=net.connector(net.default_ssl()),
        ) as session:
            async with session.request(method, f"{API_BASE}{path}", headers=headers,
                                       json=body) as resp:
                try:
                    data = await resp.json(content_type=None) or {}
                except (aiohttp.ContentTypeError, ValueError):
                    data = {}
                if resp.status >= 400:
                    message = data.get("description") or f"HTTP {resp.status}"
                    log.warning("ЮKassa %s %s → %s: %s", method, path, resp.status, message)
                    return False, {"error": message, "status": resp.status}
                return True, data
    except Exception as exc:  # noqa: BLE001 — сеть не должна ронять бота
        log.warning("ЮKassa: запрос не удался: %s", exc)
        return False, {"error": str(exc) or exc.__class__.__name__}


def _rub(kop: int) -> str:
    return f"{int(kop) // 100}.{int(kop) % 100:02d}"


def _phone(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


async def _receipt(group: list[Row]) -> Optional[dict[str, Any]]:
    """Чек по 54-ФЗ — если в настройках включена отправка чеков через ЮKassa."""
    if not await repo.get_bool("yk_receipt", False):
        return None
    phone = _phone(group[0]["phone"])
    if not phone:
        log.warning("Чек ЮKassa без телефона гостя не сформировать — заказ %s",
                    group[0]["number"])
        return None
    vat_code = await repo.get_int("yk_vat_code", 1)
    items = []
    for row in group:
        qty, total = int(row["qty"] or 1), int(row["total_kop"] or 0)
        # цена за штуку должна быть в копейках без остатка — иначе одной позицией
        unit, whole = (total // qty, qty) if qty and total % qty == 0 else (total, 1)
        items.append({
            "description": f"Завтрак «{row['set_title'] or 'сет дня'}» на {row['delivery_date']}"[:128],
            "quantity": f"{whole}.00",
            "amount": {"value": _rub(unit), "currency": "RUB"},
            "vat_code": vat_code,
            "payment_mode": "full_payment",
            "payment_subject": "commodity",
        })
    return {"customer": {"phone": phone}, "items": items}


# ---------------------------------------------------------------- платежи
def _payment_ref(order: Row) -> str:
    raw = order["payment_id"] or ""
    return raw[len(PREFIX):] if raw.startswith(PREFIX) else ""


async def get_payment(payment_id: str) -> tuple[str, dict[str, Any]]:
    """Статус платежа: succeeded / canceled / pending или '' при ошибке."""
    ok, data = await _request("GET", f"/payments/{payment_id}")
    return (str(data.get("status", "")) if ok else ""), data


async def payment_link(group: list[Row]) -> tuple[str, str]:
    """Ссылка на оплату заказа. → (ссылка, ошибка)

    Ссылка на заказ одна: если прежняя ещё ждёт оплаты, отдаём её же —
    так гость не оплатит один заказ дважды по двум разным ссылкам.
    """
    active = [row for row in group if row["status"] != statuses.CANCELLED] or group
    head = await repo.get_order(group[0]["id"])
    if head is None:
        return "", "заказ не найден"

    current = _payment_ref(head)
    if current and head["payment_url"]:
        status, _ = await get_payment(current)
        if status == PENDING:
            return head["payment_url"], ""
        if status == SUCCEEDED:
            return "", "заказ уже оплачен"

    total = sum(int(row["total_kop"] or 0) for row in active)
    if total <= 0:
        return "", "сумма заказа нулевая"
    number = head["group_key"] or head["number"]
    channel = get_channel(head["channel"])
    body: dict[str, Any] = {
        "amount": {"value": _rub(total), "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            # после оплаты гость возвращается в чат с ботом
            "return_url": channel.start_link() if channel else "https://yookassa.ru",
        },
        "description": f"Заказ №{number} · завтраки Fatucci"[:128],
        "metadata": {"order_id": str(head["id"]), "number": number},
    }
    receipt = await _receipt(active)
    if receipt:
        body["receipt"] = receipt

    ok, data = await _request("POST", "/payments", body)
    if not ok:
        return "", str(data.get("error", "неизвестная ошибка"))
    payment_id = str(data.get("id", ""))
    url = str((data.get("confirmation") or {}).get("confirmation_url", ""))
    if not payment_id or not url:
        return "", "ЮKassa не вернула ссылку на оплату"

    await repo.update_order(head["id"], payment_id=PREFIX + payment_id, payment_url=url)
    await repo.add_event(head["id"], head["status"], "бот",
                         f"Ссылка на оплату ЮKassa на {_rub(total)} ₽")
    return url, ""


async def check_setup() -> tuple[bool, str]:
    """Кнопка «Проверить ЮKassa» в админке."""
    shop_id, secret = await credentials()
    if not (shop_id and secret):
        return False, (
            "ℹ️ <b>ЮKassa не подключена</b>\n\n"
            "Нужны два значения из личного кабинета ЮKassa → "
            "<b>Интеграция → Ключи API</b>:\n"
            "• <b>shopId</b> — номер магазина;\n"
            "• <b>секретный ключ</b> — начинается с <code>live_</code> "
            "(или <code>test_</code> у тестового магазина).\n\n"
            "Без них гости из MAX платят только переводом по реквизитам."
        )
    if not shop_id.isdigit():
        return False, "⚠️ <b>shopId должен быть числом</b> — скопируйте его заново."
    if not secret_looks_valid(secret):
        return False, ("⚠️ <b>Секретный ключ не похож на настоящий</b>\n\n"
                       "Он начинается с <code>live_</code> или <code>test_</code>.")

    ok, data = await _request("GET", "/me")
    if not ok:
        if data.get("status") in (401, 403):
            return False, ("⚠️ <b>ЮKassa не приняла ключи</b>\n\n"
                           "Проверьте shopId и секретный ключ: они должны быть "
                           "от одного магазина. Ключ можно перевыпустить в личном кабинете.")
        return False, f"⚠️ <b>Не удалось связаться с ЮKassa</b>\n\n<code>{data.get('error')}</code>"

    test = bool(data.get("test")) or is_test_key(secret)
    mode = ("🧪 <b>Тестовый магазин</b> — деньги не списываются. Тестовая карта: "
            "<code>5555 5555 5555 4477</code>, срок любой будущий, CVC любой."
            if test else "💰 <b>Боевой магазин</b> — платежи настоящие.")
    receipt = ("🧾 Чеки по 54-ФЗ отправляет ЮKassa (по телефону гостя)."
               if await repo.get_bool("yk_receipt", False) else
               "🧾 Чеки через ЮKassa выключены.")
    return True, (
        f"✅ <b>ЮKassa подключена</b> · магазин <code>{shop_id}</code>\n\n{mode}\n{receipt}\n\n"
        "Гость из MAX получает кнопку «Оплатить картой», оплата подтверждается "
        "сама в течение минуты."
    )


# ------------------------------------------------------------ опрос оплат
async def watch_tick() -> int:
    """Проверить ждущие ссылки. Возвращает паузу до следующей проверки."""
    from . import notify, orders_service, paytest
    from .channels.base import Btn

    if not await is_configured():
        return 120
    waiting_tests = await paytest.watch()
    rows = await repo.open_card_payments()
    for order in rows:
        payment_id = _payment_ref(order)
        status, data = await get_payment(payment_id)
        if status == SUCCEEDED:
            await repo.update_order(order["id"], payment_url="")
            fresh = await repo.get_order(order["id"])
            if fresh["status"] in (statuses.NEW, statuses.ACCEPTED):
                await orders_service.apply_payment_success(
                    fresh, PREFIX + payment_id, actor="ЮKassa")
            elif fresh["status"] not in (statuses.PAID, statuses.DELIVERED, statuses.RECEIVED):
                number = fresh["group_key"] or fresh["number"]
                await notify.send_to_admins(
                    f"⚠️ <b>Оплата картой по заказу №{number}</b> пришла, но заказ "
                    f"в статусе «{statuses.label(fresh['status'])}».\n\n"
                    "Свяжитесь с гостем: деньги можно вернуть в личном кабинете ЮKassa.")
        elif status == CANCELED:
            await repo.update_order(order["id"], payment_url="")
            reason = (data.get("cancellation_details") or {}).get("reason", "")
            await repo.add_event(order["id"], order["status"], "ЮKassa",
                                 f"Ссылка на оплату не оплачена ({reason or 'отменена'})")
            if order["status"] == statuses.ACCEPTED:
                number = order["group_key"] or order["number"]
                await notify.notify_guest(
                    order,
                    await repo.render_text("card_link_expired", number=number),
                    [[Btn(text="💳 Новая ссылка на оплату", data=f"g:card:{order['id']}",
                          intent="positive")],
                     [Btn(text="📦 Мои заказы", data="g:my")]])
    return 20 if rows or waiting_tests else 60
