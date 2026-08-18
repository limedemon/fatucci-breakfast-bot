"""SQLite-хранилище: соединение, схема, первичное наполнение."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional, Sequence

import aiosqlite

from .config import cfg

log = logging.getLogger(__name__)

_conn: Optional[aiosqlite.Connection] = None
_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS texts (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS objects (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    group_title    TEXT NOT NULL DEFAULT '',
    address        TEXT NOT NULL DEFAULT '',
    price_kop      INTEGER NOT NULL DEFAULT 0,
    delivery_days  TEXT NOT NULL DEFAULT '1,2,3,4,5,6,7',
    cutoff_time    TEXT NOT NULL DEFAULT '20:00',
    lead_days      INTEGER NOT NULL DEFAULT 1,
    max_days_ahead INTEGER NOT NULL DEFAULT 7,
    min_qty        INTEGER NOT NULL DEFAULT 1,
    max_qty        INTEGER NOT NULL DEFAULT 10,
    is_active      INTEGER NOT NULL DEFAULT 1,
    is_general     INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_kop   INTEGER,
    photo_path  TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 100,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rotation_week (
    weekday INTEGER PRIMARY KEY,
    set_id  INTEGER
);

CREATE TABLE IF NOT EXISTS rotation_date (
    d      TEXT PRIMARY KEY,
    set_id INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    ext_id      TEXT NOT NULL,
    chat_id     TEXT NOT NULL DEFAULT '',
    username    TEXT NOT NULL DEFAULT '',
    full_name   TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    apartment   TEXT NOT NULL DEFAULT '',
    object_id   INTEGER,
    source_code TEXT NOT NULL DEFAULT '',
    is_blocked  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (channel, ext_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    number         TEXT NOT NULL UNIQUE,
    user_pk        INTEGER,
    channel        TEXT NOT NULL,
    ext_id         TEXT NOT NULL,
    chat_id        TEXT NOT NULL DEFAULT '',
    object_id      INTEGER,
    object_title   TEXT NOT NULL DEFAULT '',
    object_address TEXT NOT NULL DEFAULT '',
    set_id         INTEGER,
    set_title      TEXT NOT NULL DEFAULT '',
    delivery_date  TEXT NOT NULL,
    qty            INTEGER NOT NULL DEFAULT 1,
    apartment      TEXT NOT NULL DEFAULT '',
    phone          TEXT NOT NULL DEFAULT '',
    comment        TEXT NOT NULL DEFAULT '',
    price_kop      INTEGER NOT NULL DEFAULT 0,
    total_kop      INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'new',
    source_code    TEXT NOT NULL DEFAULT '',
    payment_id     TEXT NOT NULL DEFAULT '',
    payment_url    TEXT NOT NULL DEFAULT '',
    paid_at        TEXT NOT NULL DEFAULT '',
    admin_msgs     TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_date   ON orders (delivery_date);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_object ON orders (object_id);
CREATE INDEX IF NOT EXISTS idx_orders_user   ON orders (channel, ext_id);

CREATE TABLE IF NOT EXISTS order_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    status   TEXT NOT NULL,
    actor    TEXT NOT NULL DEFAULT '',
    note     TEXT NOT NULL DEFAULT '',
    at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events (order_id);

CREATE TABLE IF NOT EXISTS sessions (
    channel     TEXT NOT NULL,
    ext_id      TEXT NOT NULL,
    chat_id     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}',
    object_id   INTEGER,
    last_msg_id TEXT NOT NULL DEFAULT '',
    reminded    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, ext_id)
);

CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    photo_path  TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    button_text TEXT NOT NULL DEFAULT 'Открыть',
    is_active   INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS media_cache (
    path    TEXT NOT NULL,
    channel TEXT NOT NULL,
    ref     TEXT NOT NULL,
    PRIMARY KEY (path, channel)
);

CREATE TABLE IF NOT EXISTS digests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    d          TEXT NOT NULL,
    body       TEXT NOT NULL,
    auto       INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_state (
    admin_id INTEGER PRIMARY KEY,
    state    TEXT NOT NULL DEFAULT '',
    data     TEXT NOT NULL DEFAULT '{}'
);
"""


async def connect() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        pending = aiosqlite.connect(cfg.db_path)
        # поток соединения не должен удерживать процесс при аварийном выходе
        pending.daemon = True
        _conn = await pending
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA busy_timeout=5000")
        await _conn.commit()
    return _conn


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    conn = await connect()
    async with _lock:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur.lastrowid or 0


async def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    conn = await connect()
    async with _lock:
        await conn.executemany(sql, list(seq))
        await conn.commit()


async def fetchall(sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
    conn = await connect()
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return list(rows)


async def fetchone(sql: str, params: Sequence[Any] = ()) -> Optional[aiosqlite.Row]:
    conn = await connect()
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetchval(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    row = await fetchone(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


async def init_db() -> None:
    conn = await connect()
    async with _lock:
        await conn.executescript(SCHEMA)
        await conn.commit()
    await _seed()
    log.info("База данных готова: %s", cfg.db_path)


async def _seed() -> None:
    """Первичное наполнение: настройки, тексты, демо-объекты, демо-сеты, допредложения."""
    from .defaults import DEFAULT_SETTINGS, DEFAULT_TEXTS, DEMO_OBJECTS, DEMO_OFFERS, DEMO_SETS

    for key, value in DEFAULT_SETTINGS.items():
        await execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    for key, value in DEFAULT_TEXTS.items():
        await execute("INSERT OR IGNORE INTO texts (key, value) VALUES (?, ?)", (key, value))

    if not await fetchval("SELECT COUNT(*) FROM sets"):
        for item in DEMO_SETS:
            await execute(
                "INSERT INTO sets (title, description, price_kop, sort_order) VALUES (?, ?, ?, ?)",
                (item["title"], item["description"], item.get("price_kop"), item["sort_order"]),
            )
        rows = await fetchall("SELECT id FROM sets ORDER BY sort_order, id")
        set_ids = [r["id"] for r in rows]
        for weekday in range(1, 8):
            await execute(
                "INSERT OR REPLACE INTO rotation_week (weekday, set_id) VALUES (?, ?)",
                (weekday, set_ids[(weekday - 1) % len(set_ids)] if set_ids else None),
            )

    if not await fetchval("SELECT COUNT(*) FROM objects"):
        for obj in DEMO_OBJECTS:
            await execute(
                """INSERT INTO objects (code, title, group_title, address, price_kop,
                                        is_general, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (obj["code"], obj["title"], obj["group_title"], obj["address"],
                 obj["price_kop"], obj["is_general"], obj["note"]),
            )

    if not await fetchval("SELECT COUNT(*) FROM offers"):
        for offer in DEMO_OFFERS:
            await execute(
                """INSERT INTO offers (title, description, url, button_text, sort_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (offer["title"], offer["description"], offer["url"],
                 offer["button_text"], offer["sort_order"]),
            )
