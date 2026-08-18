"""Онлайн-оплата через ЮKassa (REST API).

Вебхуки не используются намеренно: на bothost нет публичного адреса,
поэтому статус платежа проверяется опросом (см. scheduler.payment_watcher).
Одинаково работает и для Telegram, и для MAX — гость просто получает ссылку.
"""
from __future__ import annotations

import base64
import logging
import uuid
from typing import Any, Optional

import aiohttp
import aiosqlite

from . import net, repo
from .config import cfg
from .utils import fmt_date

log = logging.getLogger(__name__)
Row = aiosqlite.Row

API_URL = "https://api.yookassa.ru/v3/payments"

STATUS_SUCCEEDED = "succeeded"
STATUS_CANCELED = "canceled"
STATUS_PENDING = "pending"
STATUS_WAITING = "waiting_for_capture"


async def credentials() -> tuple[str, str]:
    shop_id = (await repo.get_setting("yk_shop_id")) or cfg.yookassa_shop_id
    secret = (await repo.get_setting("yk_secret")) or cfg.yookassa_secret
    return shop_id.strip(), secret.strip()


async def is_configured() -> bool:
    shop_id, secret = await credentials()
    return bool(shop_id and secret)


async def is_enabled() -> bool:
    return await repo.get_bool("pay_enabled", True) and await is_configured()


def _auth_header(shop_id: str, secret: str) -> str:
    token = base64.b64encode(f"{shop_id}:{secret}".encode()).decode()
    return f"Basic {token}"


async def _request(
    method: str, url: str, json_body: Optional[dict[str, Any]] = None, idempotence: bool = False
) -> tuple[bool, dict[str, Any]]:
    shop_id, secret = await credentials()
    if not (shop_id and secret):
        return False, {"error": "Не заданы shop_id / секретный ключ ЮKassa"}
    headers = {"Authorization": _auth_header(shop_id, secret), "Content-Type": "application/json"}
    if idempotence:
        headers["Idempotence-Key"] = str(uuid.uuid4())
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout, connector=net.connector(net.default_ssl())
        ) as session:
            async with session.request(method, url, headers=headers, json=json_body) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    message = (data or {}).get("description") or str(data)[:200]
                    log.warning("ЮKassa %s %s -> %s: %s", method, url, resp.status, message)
                    return False, {"error": message}
                return True, data or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("ЮKassa запрос не удался: %s", exc)
        return False, {"error": str(exc)}


async def _return_url() -> str:
    custom = (await repo.get_setting("yk_return_url")).strip()
    if custom:
        return custom
    if cfg.telegram_username:
        return f"https://t.me/{cfg.telegram_username}"
    return "https://max.ru/"


def _rub(kop: int) -> str:
    return f"{kop // 100}.{kop % 100:02d}"


async def _receipt(order: Row) -> Optional[dict[str, Any]]:
    if not await repo.get_bool("yk_receipt", False):
        return None
    phone = "".join(ch for ch in (order["phone"] or "") if ch.isdigit())
    if not phone:
        return None
    vat_code = await repo.get_int("yk_vat_code", 1)
    return {
        "customer": {"phone": phone},
        "items": [
            {
                "description": (order["set_title"] or "Завтрак")[:128],
                "quantity": f"{order['qty']}.00",
                "amount": {"value": _rub(order["price_kop"]), "currency": "RUB"},
                "vat_code": vat_code,
                "payment_mode": "full_payment",
                "payment_subject": "commodity",
            }
        ],
    }


async def create_payment(order: Row) -> tuple[str, str, str]:
    """Создать платёж. Возвращает (payment_id, confirmation_url, error)."""
    body: dict[str, Any] = {
        "amount": {"value": _rub(order["total_kop"]), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": await _return_url()},
        "description": f"Заказ №{order['number']} · завтрак {fmt_date(order['delivery_date'], False)}",
        "metadata": {"order_id": str(order["id"]), "number": order["number"]},
    }
    receipt = await _receipt(order)
    if receipt:
        body["receipt"] = receipt

    ok, data = await _request("POST", API_URL, json_body=body, idempotence=True)
    if not ok:
        return "", "", str(data.get("error", "неизвестная ошибка"))
    payment_id = str(data.get("id", ""))
    url = str((data.get("confirmation") or {}).get("confirmation_url", ""))
    if not payment_id or not url:
        return "", "", "ЮKassa не вернула ссылку на оплату"
    return payment_id, url, ""


async def payment_status(payment_id: str) -> str:
    ok, data = await _request("GET", f"{API_URL}/{payment_id}")
    if not ok:
        return ""
    return str(data.get("status", ""))


async def test_credentials() -> tuple[bool, str]:
    ok, data = await _request("GET", f"{API_URL}?limit=1")
    if ok:
        return True, "Ключи ЮKassa приняты — оплата будет работать."
    return False, f"Ошибка: {data.get('error', 'неизвестно')}"
