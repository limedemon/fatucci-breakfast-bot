"""Картинки в базе.

Раньше фото сетов и предложений лежали файлами в data/photos. Теперь всё
состояние бота хранится в одной базе: так бэкап — это один дамп, а на хостинге
не нужно заботиться о том, переживёт ли папка перезапуск или переезд.

В полях `sets.photo_path` и `offers.photo_path` теперь лежит не путь, а
**ключ** вида ``set:3``. Старые установки продолжают работать: если по ключу
в базе ничего нет, а на диске есть файл с таким именем — он будет прочитан
и при первой же отправке перенесён в базу.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from . import db, repo
from .utils import utc_stamp

log = logging.getLogger(__name__)

#: больше этого в базу не кладём — Telegram всё равно ужимает превью
MAX_BYTES = 8 * 1024 * 1024


def key_for(entity: str, entity_id: int) -> str:
    """Ключ картинки сущности: set:3, offer:1, broadcast."""
    return f"{entity}:{entity_id}"


async def save(key: str, data: bytes, mime: str = "image/jpeg") -> bool:
    if not key or not data:
        return False
    if len(data) > MAX_BYTES:
        log.warning("Картинка %s слишком большая (%s байт) — не сохраняю", key, len(data))
        return False
    await db.execute(
        "INSERT INTO media (key, data, mime, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET data = excluded.data, mime = excluded.mime, "
        "updated_at = excluded.updated_at",
        (key, data, mime, utc_stamp()),
    )
    # картинка поменялась — прошлые file_id и токены больше не годятся
    await repo.drop_media(key)
    return True


async def load(key: str) -> Optional[bytes]:
    """Байты картинки. Ключ может быть и путём к файлу — для старых установок."""
    if not key:
        return None
    value = await db.fetchval("SELECT data FROM media WHERE key = ?", (key,))
    if value is not None:
        return bytes(value)

    legacy = Path(key)
    if legacy.exists() and legacy.is_file():
        try:
            data = legacy.read_bytes()
        except OSError as exc:
            log.warning("Не удалось прочитать старый файл %s: %s", key, exc)
            return None
        await save(key, data)          # переносим в базу, чтобы файл больше не был нужен
        return data
    return None


async def exists(key: str) -> bool:
    if not key:
        return False
    if await db.fetchval("SELECT 1 FROM media WHERE key = ?", (key,)):
        return True
    path = Path(key)
    return path.exists() and path.is_file()


async def delete(key: str) -> None:
    if not key:
        return
    await db.execute("DELETE FROM media WHERE key = ?", (key,))
    await repo.drop_media(key)


async def total_size() -> tuple[int, int]:
    """Сколько картинок и сколько это весит — для админки."""
    row = await db.fetchone(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(LENGTH(data)), 0) AS bytes FROM media")
    if row is None:
        return 0, 0
    return int(row["cnt"]), int(row["bytes"])
