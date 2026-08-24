"""Хранилище: PostgreSQL на хостинге, SQLite — для локальной работы и тестов.

Какой движок использовать, решает переменная окружения ``DATABASE_URL``:
задана — работаем с PostgreSQL, не задана — с файлом SQLite рядом с кодом.

Весь SQL в проекте написан в одном диалекте (SQLite-подобном) и при работе
с PostgreSQL переводится здесь:

* плейсхолдеры ``?`` превращаются в ``$1, $2 …``;
* ``INTEGER PRIMARY KEY AUTOINCREMENT`` — в ``SERIAL PRIMARY KEY``;
* ``datetime('now')`` в схеме — в серверное время в том же текстовом формате;
* ``BLOB`` — в ``BYTEA``.

Времени вида ``datetime('now', '-45 minutes')`` в запросах намеренно нет:
такие метки считаются в Python и передаются параметром (см. utils.utc_stamp).
Так один и тот же запрос работает в обоих движках.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Iterable, Optional, Sequence

from .config import cfg

log = logging.getLogger(__name__)

#: True — работаем с PostgreSQL
IS_PG = bool(cfg.database_url)

_sqlite_conn: Any = None
_pg_pool: Any = None
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
    cutoff_time    TEXT NOT NULL DEFAULT '16:00',
    lead_days      INTEGER NOT NULL DEFAULT 1,
    max_days_ahead INTEGER NOT NULL DEFAULT 7,
    min_qty        INTEGER NOT NULL DEFAULT 1,
    max_qty        INTEGER NOT NULL DEFAULT 10,
    is_active      INTEGER NOT NULL DEFAULT 1,
    is_general     INTEGER NOT NULL DEFAULT 0,
    delivery_time  TEXT NOT NULL DEFAULT '09:00',
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel       TEXT NOT NULL,
    ext_id        TEXT NOT NULL,
    chat_id       TEXT NOT NULL DEFAULT '',
    username      TEXT NOT NULL DEFAULT '',
    full_name     TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    apartment     TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '',
    object_id     INTEGER,
    source_code   TEXT NOT NULL DEFAULT '',
    is_blocked    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
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
    customer_name  TEXT NOT NULL DEFAULT '',
    allergies      TEXT NOT NULL DEFAULT '',
    group_key      TEXT NOT NULL DEFAULT '',
    address_ok     INTEGER NOT NULL DEFAULT 1,
    discount_pct   INTEGER NOT NULL DEFAULT 0,
    base_price_kop INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS order_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    status   TEXT NOT NULL,
    actor    TEXT NOT NULL DEFAULT '',
    note     TEXT NOT NULL DEFAULT '',
    at       TEXT NOT NULL DEFAULT (datetime('now'))
);
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

CREATE TABLE IF NOT EXISTS media (
    key        TEXT PRIMARY KEY,
    data       BLOB NOT NULL,
    mime       TEXT NOT NULL DEFAULT 'image/jpeg',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS admins (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL UNIQUE,
    username   TEXT NOT NULL DEFAULT '',
    full_name  TEXT NOT NULL DEFAULT '',
    is_owner   INTEGER NOT NULL DEFAULT 0,
    added_by   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS qr_visits (
    code    TEXT NOT NULL,
    d       TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    visits  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, d, channel)
);

CREATE TABLE IF NOT EXISTS admin_state (
    admin_id INTEGER PRIMARY KEY,
    state    TEXT NOT NULL DEFAULT '',
    data     TEXT NOT NULL DEFAULT '{}'
);
"""

#: Индексы создаются ОТДЕЛЬНО и уже после миграций.
#: Иначе при обновлении бота на существующей базе индекс по новой колонке
#: пытался бы создаться раньше, чем сама колонка появится, и запуск падал бы.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_orders_date   ON orders (delivery_date)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_object ON orders (object_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_user   ON orders (channel, ext_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_group  ON orders (group_key)",
    "CREATE INDEX IF NOT EXISTS idx_events_order  ON order_events (order_id)",
]

#: серверное «сейчас» в том же текстовом виде, что и у SQLite
PG_NOW = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"


def _pg_schema(schema: str) -> str:
    schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    schema = schema.replace("(datetime('now'))", PG_NOW)
    schema = re.sub(r"\bBLOB\b", "BYTEA", schema)
    return schema


def _to_pg(sql: str) -> str:
    """Заменить ? на $1, $2 … не трогая содержимое строковых литералов."""
    out: list[str] = []
    index = 0
    in_string = False
    for char in sql:
        if char == "'":
            in_string = not in_string
            out.append(char)
        elif char == "?" and not in_string:
            index += 1
            out.append(f"${index}")
        else:
            out.append(char)
    return "".join(out)


class StorageError(RuntimeError):
    """Понятная ошибка подключения к базе — с подсказкой, что чинить."""


def dsn_hint() -> str:
    """Приметы строки подключения, по которым её можно сверить с панелью хостинга.

    Пароль целиком не показываем — только длину и края, этого хватает,
    чтобы поймать обрезанное или лишний раз скопированное значение.
    """
    match = re.match(r"^(\w+)://([^:]+):([^@]*)@([^/]+)/(.+)$", cfg.database_url)
    if not match:
        return ("Строка подключения не похожа на адрес PostgreSQL. Ожидается вид:\n"
                "<code>postgresql://пользователь:пароль@хост:порт/база</code>")
    _, user, password, host, database = match.groups()
    edges = f"{password[:2]}…{password[-2:]}" if len(password) > 6 else "(очень короткий)"
    return (f"Пользователь: {user}\n"
            f"Сервер: {host}\n"
            f"База: {database}\n"
            f"Пароль: {len(password)} символов, {edges}")


async def _open_pool() -> Any:
    """Пул PostgreSQL с понятными ошибками и повтором при сетевых сбоях."""
    import asyncpg

    settings = {"search_path": cfg.db_schema} if cfg.db_schema else None
    last: Optional[Exception] = None

    for attempt in range(3):
        try:
            return await asyncpg.create_pool(
                cfg.database_url, min_size=1, max_size=5, command_timeout=30,
                max_inactive_connection_lifetime=180, server_settings=settings,
            )
        except asyncpg.InvalidPasswordError as exc:
            raise StorageError(
                "Пароль базы не принят.\n\n"
                "Сервер отвечает, значит адрес верный — не совпадает именно пароль "
                "в переменной DATABASE_URL.\n\n"
                "Скопируйте строку подключения из панели хостинга заново, целиком "
                "и одной строкой, и вставьте в переменную окружения.\n\n"
                f"{dsn_hint()}"
            ) from exc
        except asyncpg.InvalidCatalogNameError as exc:
            raise StorageError(
                "Базы с таким именем на сервере нет — проверьте последнюю часть "
                f"адреса в DATABASE_URL.\n\n{dsn_hint()}"
            ) from exc
        except asyncpg.InvalidAuthorizationSpecificationError as exc:
            raise StorageError(
                f"Пользователю базы отказано в доступе.\n\n{dsn_hint()}"
            ) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            last = exc
            log.warning("База не отвечает (попытка %s из 3): %s", attempt + 1, exc)
            await asyncio.sleep(2 * (attempt + 1))

    raise StorageError(
        "Сервер базы не отвечает. Проверьте адрес и порт в DATABASE_URL, "
        f"а также доступна ли база с хостинга.\n\n{dsn_hint()}\n\n"
        f"Последняя ошибка: {last}"
    )


# ------------------------------------------------------------------ соединение
async def connect() -> Any:
    global _sqlite_conn, _pg_pool
    if IS_PG:
        if _pg_pool is None:
            _pg_pool = await _open_pool()
        return _pg_pool

    if _sqlite_conn is None:
        import aiosqlite

        pending = aiosqlite.connect(cfg.db_path)
        # поток соединения не должен удерживать процесс при аварийном выходе
        pending.daemon = True
        _sqlite_conn = await pending
        _sqlite_conn.row_factory = aiosqlite.Row
        await _sqlite_conn.execute("PRAGMA journal_mode=WAL")
        await _sqlite_conn.execute("PRAGMA busy_timeout=5000")
        await _sqlite_conn.commit()
    return _sqlite_conn


async def close() -> None:
    global _sqlite_conn, _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
    if _sqlite_conn is not None:
        await _sqlite_conn.close()
        _sqlite_conn = None


# --------------------------------------------------------------------- запросы
async def execute(sql: str, params: Sequence[Any] = ()) -> int:
    conn = await connect()
    if IS_PG:
        await conn.execute(_to_pg(sql), *params)
        return 0
    async with _lock:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur.lastrowid or 0


async def insert(sql: str, params: Sequence[Any] = ()) -> int:
    """INSERT, который возвращает id новой строки."""
    conn = await connect()
    if IS_PG:
        return int(await conn.fetchval(_to_pg(sql) + " RETURNING id", *params))
    async with _lock:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur.lastrowid or 0


async def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    conn = await connect()
    rows = [tuple(item) for item in seq]
    if not rows:
        return
    if IS_PG:
        await conn.executemany(_to_pg(sql), rows)
        return
    async with _lock:
        await conn.executemany(sql, rows)
        await conn.commit()


async def fetchall(sql: str, params: Sequence[Any] = ()) -> list[Any]:
    conn = await connect()
    if IS_PG:
        return list(await conn.fetch(_to_pg(sql), *params))
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return list(rows)


async def fetchone(sql: str, params: Sequence[Any] = ()) -> Optional[Any]:
    conn = await connect()
    if IS_PG:
        return await conn.fetchrow(_to_pg(sql), *params)
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetchval(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    conn = await connect()
    if IS_PG:
        value = await conn.fetchval(_to_pg(sql), *params)
        return default if value is None else value
    row = await fetchone(sql, params)
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


# ------------------------------------------------------------------- миграции
#: Колонки, добавленные уже после первого релиза.
#: CREATE TABLE IF NOT EXISTS их не добавит, поэтому доливаем отдельно.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("orders", "customer_name", "TEXT NOT NULL DEFAULT ''"),
    ("orders", "discount_pct", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "base_price_kop", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "customer_name", "TEXT NOT NULL DEFAULT ''"),
    ("orders", "allergies", "TEXT NOT NULL DEFAULT ''"),
    ("orders", "group_key", "TEXT NOT NULL DEFAULT ''"),
    ("orders", "address_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("objects", "delivery_time", "TEXT NOT NULL DEFAULT '09:00'"),
]


async def _columns(table: str) -> set[str]:
    if IS_PG:
        rows = await fetchall(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,))
    else:
        rows = await fetchall(f"PRAGMA table_info({table})")
    return {row["name"] for row in rows}


async def _migrate() -> None:
    """Добавить недостающие колонки в уже работающую базу."""
    for table, column, ddl in MIGRATIONS:
        if column in await _columns(table):
            continue
        log.info("Миграция: добавляю %s.%s", table, column)
        await execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    await _backfill()


async def _backfill() -> None:
    """Привести старые записи к нынешним правилам.

    Заказы, оформленные до появления многодневных заказов, остались без номера
    группы. Считаем каждый такой заказ группой из одного дня — тогда карточки,
    статусы и отмена работают с ними так же, как с новыми.
    """
    count = int(await fetchval("SELECT COUNT(*) FROM orders WHERE group_key = ''", (), 0))
    if not count:
        return
    await execute("UPDATE orders SET group_key = number WHERE group_key = ''")
    log.info("Миграция: проставлен номер группы у %s старых заказов", count)


async def apply_ddl(ddl: str) -> None:
    """Выполнить набор DDL-команд одним куском (нужен и тестам миграций)."""
    conn = await connect()
    if IS_PG:
        async with conn.acquire() as pg:
            await pg.execute(_pg_schema(ddl))
    else:
        async with _lock:
            await conn.executescript(ddl)
            await conn.commit()


async def _create_indexes() -> None:
    """Индексы — после миграций, чтобы новые колонки уже существовали.

    Индекс ускоряет работу, но не влияет на правильность: если конкретный
    создать не вышло, пишем предупреждение и работаем дальше, а не роняем бота.
    """
    for statement in INDEXES:
        try:
            await execute(statement)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось создать индекс (%s): %s", statement.split()[5], exc)


async def init_db() -> None:
    await apply_ddl(SCHEMA)
    await _migrate()
    await _create_indexes()
    await _seed()
    log.info("База данных готова: %s", where())


def where() -> str:
    """Человекочитаемое описание хранилища — без пароля."""
    if not IS_PG:
        return f"SQLite {cfg.db_path}"
    match = re.match(r"^\w+://[^:]+:[^@]*@(.+)$", cfg.database_url)
    return f"PostgreSQL {match.group(1) if match else '(адрес скрыт)'}"


#: таблицы, которые показываем в проверке базы: (таблица, подпись)
HEALTH_TABLES = [
    ("orders", "Заказов"),
    ("order_events", "Записей истории"),
    ("users", "Гостей"),
    ("objects", "Объектов"),
    ("sets", "Сетов"),
    ("offers", "Доп. предложений"),
    ("sessions", "Незавершённых заказов"),
    ("admins", "Администраторов"),
    ("media", "Картинок"),
    ("digests", "Выгрузок курьерам"),
    ("qr_visits", "Записей переходов по QR"),
]


async def health() -> dict[str, Any]:
    """Проверка хранилища для админ-панели: связь, объём, содержимое."""
    import time

    report: dict[str, Any] = {
        "engine": "PostgreSQL" if IS_PG else "SQLite",
        "where": where(),
        "schema": cfg.db_schema or ("public" if IS_PG else ""),
        "ok": False,
        "ping_ms": 0,
        "error": "",
        "tables": [],
        "size": 0,
        "media_bytes": 0,
        "version": "",
    }

    started = time.perf_counter()
    try:
        await fetchval("SELECT 1")
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)[:300]
        return report
    finally:
        report["ping_ms"] = round((time.perf_counter() - started) * 1000)

    for table, title in HEALTH_TABLES:
        try:
            count = int(await fetchval(f"SELECT COUNT(*) FROM {table}", (), 0))
        except Exception:  # noqa: BLE001 — таблицы может не быть на старой базе
            continue
        report["tables"].append((title, count))

    try:
        if IS_PG:
            report["version"] = str(await fetchval("SELECT version()", (), ""))[:40]
            report["size"] = int(await fetchval(
                "SELECT pg_database_size(current_database())", (), 0))
        else:
            report["version"] = "SQLite"
            report["size"] = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
        report["media_bytes"] = int(await fetchval(
            "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM media", (), 0))
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось собрать размеры базы: %s", exc)

    return report


async def _seed() -> None:
    """Первичное наполнение: настройки, тексты, демо-объекты, демо-сеты, допредложения."""
    from .defaults import DEFAULT_SETTINGS, DEFAULT_TEXTS, DEMO_OBJECTS, DEMO_OFFERS, DEMO_SETS

    for key, value in DEFAULT_SETTINGS.items():
        await execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT (key) DO NOTHING", (key, value))
    for key, value in DEFAULT_TEXTS.items():
        await execute("INSERT INTO texts (key, value) VALUES (?, ?) "
                      "ON CONFLICT (key) DO NOTHING", (key, value))

    if not await fetchval("SELECT COUNT(*) FROM sets", (), 0):
        for item in DEMO_SETS:
            await execute(
                "INSERT INTO sets (title, description, price_kop, sort_order) VALUES (?, ?, ?, ?)",
                (item["title"], item["description"], item.get("price_kop"), item["sort_order"]),
            )
        rows = await fetchall("SELECT id FROM sets ORDER BY sort_order, id")
        set_ids = [row["id"] for row in rows]
        for weekday in range(1, 8):
            await execute(
                "INSERT INTO rotation_week (weekday, set_id) VALUES (?, ?) "
                "ON CONFLICT (weekday) DO UPDATE SET set_id = excluded.set_id",
                (weekday, set_ids[(weekday - 1) % len(set_ids)] if set_ids else None),
            )

    if not await fetchval("SELECT COUNT(*) FROM objects", (), 0):
        for obj in DEMO_OBJECTS:
            await execute(
                """INSERT INTO objects (code, title, group_title, address, price_kop,
                                        is_general, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (obj["code"], obj["title"], obj["group_title"], obj["address"],
                 obj["price_kop"], obj["is_general"], obj["note"]),
            )

    if not await fetchval("SELECT COUNT(*) FROM offers", (), 0):
        for offer in DEMO_OFFERS:
            await execute(
                """INSERT INTO offers (title, description, url, button_text, sort_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (offer["title"], offer["description"], offer["url"],
                 offer["button_text"], offer["sort_order"]),
            )
