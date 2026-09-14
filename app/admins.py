"""Администраторы бота.

Список живёт в базе и правится из админ-панели — заново разворачивать бота
ради нового менеджера не нужно.

Администратор привязан к мессенджеру: ID в Telegram и в MAX — это разные
люди, даже если номера совпали. Поэтому везде, где проверка идёт по событию,
передаётся и канал. Без канала подразумевается Telegram — там админ-панель.

Первый, кто написал боту в Telegram, автоматически становится владельцем:
так бот можно запустить на хостинге, ничего заранее не зная про ID.

ADMIN_IDS из окружения остаётся запасным входом в Telegram: если владелец
потерял доступ, достаточно вписать свой ID в переменную и перезапустить бота.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import db
from .config import cfg

log = logging.getLogger(__name__)
Row = Any

TG = "tg"

_cache: dict[str, set[int]] = {}
_has_any = False   # чтобы не дёргать базу на каждое сообщение после назначения владельца


def invalidate() -> None:
    global _has_any
    _cache.clear()
    _has_any = False


def _as_id(user_id: str | int) -> Optional[int]:
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


async def ids(channel: str = TG) -> set[int]:
    """Все, у кого есть доступ в этом мессенджере."""
    if channel not in _cache:
        rows = await db.fetchall("SELECT user_id FROM admins WHERE channel = ?", (channel,))
        _cache[channel] = {int(row["user_id"]) for row in rows}
    extra = set(cfg.admin_ids) if channel == TG else set()
    return _cache[channel] | extra


async def is_admin(user_id: str | int, channel: str = TG) -> bool:
    value = _as_id(user_id)
    return value is not None and value in await ids(channel)


async def targets() -> list[tuple[str, int]]:
    """Все администраторы во всех мессенджерах — кому слать личные уведомления."""
    rows = await db.fetchall("SELECT channel, user_id FROM admins ORDER BY channel, id")
    found = [(row["channel"], int(row["user_id"])) for row in rows]
    for admin_id in sorted(cfg.admin_ids):
        if (TG, admin_id) not in found:
            found.append((TG, admin_id))
    return found


async def count() -> int:
    """Сколько админов заведено (окружение не считаем — оно только про доступ)."""
    return int(await db.fetchval("SELECT COUNT(*) FROM admins", (), 0))


async def listing(channel: Optional[str] = None) -> list[Row]:
    if channel is None:
        return await db.fetchall("SELECT * FROM admins ORDER BY is_owner DESC, channel, id")
    return await db.fetchall(
        "SELECT * FROM admins WHERE channel = ? ORDER BY is_owner DESC, id", (channel,))


async def get(user_id: int, channel: str = TG) -> Optional[Row]:
    value = _as_id(user_id)
    if value is None:
        return None
    return await db.fetchone(
        "SELECT * FROM admins WHERE channel = ? AND user_id = ?", (channel, value))


async def add(
    user_id: int, username: str = "", full_name: str = "", added_by: str = "",
    owner: bool = False, channel: str = TG,
) -> bool:
    """Выдать доступ. False — если он уже был."""
    if await get(user_id, channel) is not None:
        return False
    await db.execute(
        """INSERT INTO admins (channel, user_id, username, full_name, is_owner, added_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (channel, int(user_id), username, full_name, 1 if owner else 0, added_by),
    )
    invalidate()
    return True


async def remove(user_id: int, channel: str = TG) -> tuple[bool, str]:
    row = await get(user_id, channel)
    if row is None:
        return False, "Такого администратора нет"
    if row["is_owner"]:
        return False, "Владельца бота убрать нельзя"
    await db.execute("DELETE FROM admins WHERE channel = ? AND user_id = ?",
                     (channel, int(user_id)))
    invalidate()
    return True, "Доступ отозван"


async def is_owner(user_id: str | int, channel: str = TG) -> bool:
    row = await get(user_id, channel)
    return bool(row and row["is_owner"])


async def claim_owner(user_id: int, username: str = "", full_name: str = "") -> bool:
    """Первый написавший боту в Telegram становится владельцем.

    Срабатывает ровно один раз: пока в базе нет ни одного админа и в окружении
    не задан ADMIN_IDS. Возвращает True, если доступ только что выдан.
    """
    global _has_any
    if _has_any or cfg.admin_ids:
        return False
    if await count():
        _has_any = True
        return False
    await add(user_id, username, full_name, added_by="первый запуск", owner=True)
    _has_any = True
    log.warning(
        "Владельцем бота назначен первый написавший: id=%s %s (@%s)",
        user_id, full_name, username or "-",
    )
    return True
