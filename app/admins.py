"""Администраторы бота.

Список живёт в базе и правится из админ-панели — заново разворачивать бота
ради нового менеджера не нужно.

Первый, кто написал боту, автоматически становится владельцем: так бот
можно запустить на хостинге, ничего заранее не зная про ID.

ADMIN_IDS из окружения остаётся запасным входом: если владелец потерял
доступ, достаточно вписать свой ID в переменную и перезапустить бота.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiosqlite

from . import db
from .config import cfg

log = logging.getLogger(__name__)
Row = aiosqlite.Row

_cache: Optional[set[int]] = None
_has_any = False   # чтобы не дёргать базу на каждое сообщение после назначения владельца


def invalidate() -> None:
    global _cache, _has_any
    _cache = None
    _has_any = False


async def ids() -> set[int]:
    """Все, у кого есть доступ: из базы плюс аварийный список из окружения."""
    global _cache
    if _cache is None:
        rows = await db.fetchall("SELECT user_id FROM admins")
        _cache = {int(row["user_id"]) for row in rows}
    return _cache | set(cfg.admin_ids)


async def is_admin(user_id: str | int) -> bool:
    try:
        return int(user_id) in await ids()
    except (TypeError, ValueError):
        return False


async def count() -> int:
    """Сколько админов заведено (окружение не считаем — оно только про доступ)."""
    return int(await db.fetchval("SELECT COUNT(*) FROM admins", (), 0))


async def listing() -> list[Row]:
    return await db.fetchall("SELECT * FROM admins ORDER BY is_owner DESC, id")


async def get(user_id: int) -> Optional[Row]:
    return await db.fetchone("SELECT * FROM admins WHERE user_id = ?", (int(user_id),))


async def add(
    user_id: int, username: str = "", full_name: str = "", added_by: str = "", owner: bool = False
) -> bool:
    """Выдать доступ. False — если он уже был."""
    if await get(user_id) is not None:
        return False
    await db.execute(
        """INSERT INTO admins (user_id, username, full_name, is_owner, added_by)
           VALUES (?, ?, ?, ?, ?)""",
        (int(user_id), username, full_name, 1 if owner else 0, added_by),
    )
    invalidate()
    return True


async def remove(user_id: int) -> tuple[bool, str]:
    row = await get(user_id)
    if row is None:
        return False, "Такого администратора нет"
    if row["is_owner"]:
        return False, "Владельца бота убрать нельзя"
    await db.execute("DELETE FROM admins WHERE user_id = ?", (int(user_id),))
    invalidate()
    return True, "Доступ отозван"


async def is_owner(user_id: str | int) -> bool:
    row = await get(int(user_id))
    return bool(row and row["is_owner"])


async def claim_owner(user_id: int, username: str = "", full_name: str = "") -> bool:
    """Первый написавший боту становится владельцем.

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
