"""Доступ к данным. Единственное место, где живёт SQL."""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Optional, Sequence

import aiosqlite

from . import db, statuses
from .utils import fmt_money, safe_format

Row = aiosqlite.Row


# =========================================================== настройки/тексты
async def get_setting(key: str, default: str = "") -> str:
    value = await db.fetchval("SELECT value FROM settings WHERE key = ?", (key,))
    return default if value is None else str(value)


async def set_setting(key: str, value: Any) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "" if value is None else str(value)),
    )


async def get_int(key: str, default: int = 0) -> int:
    raw = await get_setting(key, str(default))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


async def get_bool(key: str, default: bool = False) -> bool:
    raw = (await get_setting(key, "1" if default else "0")).strip().lower()
    return raw in ("1", "true", "yes", "on", "да")


async def get_text(key: str, default: str = "") -> str:
    value = await db.fetchval("SELECT value FROM texts WHERE key = ?", (key,))
    return default if value is None else str(value)


async def set_text(key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO texts (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def all_texts() -> list[Row]:
    return await db.fetchall("SELECT key, value FROM texts ORDER BY key")


async def render_text(key: str, **kwargs: Any) -> str:
    """Текст из БД с подстановкой общих плейсхолдеров."""
    template = await get_text(key)
    common = {
        "manager_contact": await get_setting("manager_contact"),
        "manager_phone": await get_setting("manager_phone"),
        "cutoff": kwargs.pop("cutoff", await default_cutoff()),
    }
    common.update(kwargs)
    return safe_format(template, **common)


async def default_cutoff() -> str:
    """Время отсечки самого «позднего» активного объекта — для общих текстов."""
    value = await db.fetchval(
        "SELECT cutoff_time FROM objects WHERE is_active = 1 ORDER BY id LIMIT 1"
    )
    return str(value or "20:00")


# ==================================================================== объекты
OBJECT_FIELDS = (
    "code", "title", "group_title", "address", "price_kop", "delivery_days",
    "cutoff_time", "lead_days", "max_days_ahead", "min_qty", "max_qty",
    "is_active", "is_general", "note",
)


async def list_objects(active_only: bool = False, selectable: bool = False) -> list[Row]:
    sql = "SELECT * FROM objects"
    where = []
    if active_only:
        where.append("is_active = 1")
    if selectable:
        where.append("is_general = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY group_title, title"
    return await db.fetchall(sql)


async def get_object(object_id: int | None) -> Optional[Row]:
    if not object_id:
        return None
    return await db.fetchone("SELECT * FROM objects WHERE id = ?", (object_id,))


async def get_object_by_code(code: str) -> Optional[Row]:
    return await db.fetchone("SELECT * FROM objects WHERE code = ? COLLATE NOCASE", (code,))


async def code_taken(code: str, exclude_id: int = 0) -> bool:
    row = await db.fetchone(
        "SELECT id FROM objects WHERE code = ? COLLATE NOCASE AND id <> ?", (code, exclude_id)
    )
    return row is not None


async def create_object(**fields: Any) -> int:
    data = {k: v for k, v in fields.items() if k in OBJECT_FIELDS}
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return await db.execute(f"INSERT INTO objects ({cols}) VALUES ({marks})", list(data.values()))


async def update_object(object_id: int, **fields: Any) -> None:
    data = {k: v for k, v in fields.items() if k in OBJECT_FIELDS}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    await db.execute(f"UPDATE objects SET {sets} WHERE id = ?", [*data.values(), object_id])


async def delete_object(object_id: int) -> None:
    await db.execute("DELETE FROM objects WHERE id = ?", (object_id,))


# ======================================================================= сеты
SET_FIELDS = ("title", "description", "price_kop", "photo_path", "is_active", "sort_order")


async def list_sets(active_only: bool = False) -> list[Row]:
    sql = "SELECT * FROM sets"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort_order, id"
    return await db.fetchall(sql)


async def get_set(set_id: int | None) -> Optional[Row]:
    if not set_id:
        return None
    return await db.fetchone("SELECT * FROM sets WHERE id = ?", (set_id,))


async def create_set(**fields: Any) -> int:
    data = {k: v for k, v in fields.items() if k in SET_FIELDS}
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return await db.execute(f"INSERT INTO sets ({cols}) VALUES ({marks})", list(data.values()))


async def update_set(set_id: int, **fields: Any) -> None:
    data = {k: v for k, v in fields.items() if k in SET_FIELDS}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    await db.execute(f"UPDATE sets SET {sets} WHERE id = ?", [*data.values(), set_id])


async def delete_set(set_id: int) -> None:
    await db.execute("DELETE FROM sets WHERE id = ?", (set_id,))
    await db.execute("UPDATE rotation_week SET set_id = NULL WHERE set_id = ?", (set_id,))
    await db.execute("DELETE FROM rotation_date WHERE set_id = ?", (set_id,))


# =================================================================== ротация
async def rotation_week() -> dict[int, Optional[int]]:
    rows = await db.fetchall("SELECT weekday, set_id FROM rotation_week")
    result = {wd: None for wd in range(1, 8)}
    for row in rows:
        result[row["weekday"]] = row["set_id"]
    return result


async def set_rotation_week(weekday: int, set_id: Optional[int]) -> None:
    await db.execute(
        "INSERT INTO rotation_week (weekday, set_id) VALUES (?, ?) "
        "ON CONFLICT(weekday) DO UPDATE SET set_id = excluded.set_id",
        (weekday, set_id),
    )


async def rotation_dates(from_date: Optional[date] = None) -> list[Row]:
    if from_date:
        return await db.fetchall(
            "SELECT d, set_id FROM rotation_date WHERE d >= ? ORDER BY d",
            (from_date.strftime("%Y-%m-%d"),),
        )
    return await db.fetchall("SELECT d, set_id FROM rotation_date ORDER BY d")


async def set_rotation_date(day: str, set_id: Optional[int]) -> None:
    await db.execute(
        "INSERT INTO rotation_date (d, set_id) VALUES (?, ?) "
        "ON CONFLICT(d) DO UPDATE SET set_id = excluded.set_id",
        (day, set_id),
    )


async def del_rotation_date(day: str) -> None:
    await db.execute("DELETE FROM rotation_date WHERE d = ?", (day,))


async def set_for_date(day: date) -> Optional[Row]:
    """Какой сет предусмотрен на дату: сначала точечная ротация, потом недельная."""
    iso = day.strftime("%Y-%m-%d")
    row = await db.fetchone(
        "SELECT s.* FROM rotation_date rd JOIN sets s ON s.id = rd.set_id "
        "WHERE rd.d = ? AND s.is_active = 1",
        (iso,),
    )
    if row:
        return row
    return await db.fetchone(
        "SELECT s.* FROM rotation_week rw JOIN sets s ON s.id = rw.set_id "
        "WHERE rw.weekday = ? AND s.is_active = 1",
        (day.isoweekday(),),
    )


# ============================================================== пользователи
async def upsert_user(
    channel: str,
    ext_id: str,
    chat_id: str = "",
    username: str = "",
    full_name: str = "",
) -> Row:
    await db.execute(
        """INSERT INTO users (channel, ext_id, chat_id, username, full_name)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(channel, ext_id) DO UPDATE SET
               chat_id   = CASE WHEN excluded.chat_id   <> '' THEN excluded.chat_id   ELSE users.chat_id   END,
               username  = CASE WHEN excluded.username  <> '' THEN excluded.username  ELSE users.username  END,
               full_name = CASE WHEN excluded.full_name <> '' THEN excluded.full_name ELSE users.full_name END,
               last_seen = datetime('now')""",
        (channel, str(ext_id), str(chat_id), username, full_name),
    )
    row = await db.fetchone(
        "SELECT * FROM users WHERE channel = ? AND ext_id = ?", (channel, str(ext_id))
    )
    assert row is not None
    return row


async def get_user(channel: str, ext_id: str) -> Optional[Row]:
    return await db.fetchone(
        "SELECT * FROM users WHERE channel = ? AND ext_id = ?", (channel, str(ext_id))
    )


async def get_user_pk(user_pk: int) -> Optional[Row]:
    return await db.fetchone("SELECT * FROM users WHERE id = ?", (user_pk,))


USER_FIELDS = ("phone", "apartment", "customer_name", "object_id", "source_code",
               "is_blocked", "chat_id")


async def update_user(channel: str, ext_id: str, **fields: Any) -> None:
    data = {k: v for k, v in fields.items() if k in USER_FIELDS}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    await db.execute(
        f"UPDATE users SET {sets} WHERE channel = ? AND ext_id = ?",
        [*data.values(), channel, str(ext_id)],
    )


async def set_blocked(user_id: int, blocked: bool) -> None:
    await db.execute("UPDATE users SET is_blocked = ? WHERE id = ?", (1 if blocked else 0, user_id))


async def list_users(
    limit: int = 20, offset: int = 0, blocked: Optional[bool] = None, channel: str = ""
) -> list[Row]:
    where, params = [], []
    if blocked is not None:
        where.append("is_blocked = ?")
        params.append(1 if blocked else 0)
    if channel:
        where.append("channel = ?")
        params.append(channel)
    sql = "SELECT * FROM users"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return await db.fetchall(sql, params)


async def count_users(blocked: Optional[bool] = None, channel: str = "") -> int:
    where, params = [], []
    if blocked is not None:
        where.append("is_blocked = ?")
        params.append(1 if blocked else 0)
    if channel:
        where.append("channel = ?")
        params.append(channel)
    sql = "SELECT COUNT(*) FROM users"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(await db.fetchval(sql, params, 0))


async def broadcast_targets(channel: str = "") -> list[Row]:
    sql = "SELECT channel, ext_id, chat_id FROM users WHERE is_blocked = 0"
    params: list[Any] = []
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    return await db.fetchall(sql, params)


# ==================================================================== сессии
async def get_session(channel: str, ext_id: str) -> Optional[Row]:
    return await db.fetchone(
        "SELECT * FROM sessions WHERE channel = ? AND ext_id = ?", (channel, str(ext_id))
    )


async def save_session(
    channel: str,
    ext_id: str,
    state: str,
    data: dict[str, Any],
    chat_id: str = "",
    object_id: Optional[int] = None,
    last_msg_id: str = "",
    reminded: int = 0,
) -> None:
    await db.execute(
        """INSERT INTO sessions (channel, ext_id, chat_id, state, data, object_id,
                                 last_msg_id, reminded, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(channel, ext_id) DO UPDATE SET
               chat_id     = CASE WHEN excluded.chat_id <> '' THEN excluded.chat_id ELSE sessions.chat_id END,
               state       = excluded.state,
               data        = excluded.data,
               object_id   = excluded.object_id,
               last_msg_id = excluded.last_msg_id,
               reminded    = excluded.reminded,
               updated_at  = datetime('now')""",
        (channel, str(ext_id), str(chat_id), state, json.dumps(data, ensure_ascii=False),
         object_id, last_msg_id, reminded),
    )


async def clear_session(channel: str, ext_id: str) -> None:
    await db.execute(
        "UPDATE sessions SET state = '', data = '{}', reminded = 0, updated_at = datetime('now') "
        "WHERE channel = ? AND ext_id = ?",
        (channel, str(ext_id)),
    )


async def mark_reminded(channel: str, ext_id: str) -> None:
    await db.execute(
        "UPDATE sessions SET reminded = 1 WHERE channel = ? AND ext_id = ?", (channel, str(ext_id))
    )


async def stale_sessions(older_than_min: int, max_hours: int) -> list[Row]:
    return await db.fetchall(
        """SELECT * FROM sessions
           WHERE state <> '' AND reminded = 0
             AND updated_at <= datetime('now', ?)
             AND updated_at >= datetime('now', ?)""",
        (f"-{max(1, older_than_min)} minutes", f"-{max(2, max_hours)} hours"),
    )


# ==================================================================== заказы
ORDER_FIELDS = (
    "user_pk", "channel", "ext_id", "chat_id", "object_id", "object_title", "object_address",
    "set_id", "set_title", "delivery_date", "qty", "apartment", "phone", "comment",
    "customer_name", "discount_pct", "base_price_kop",
    "price_kop", "total_kop", "status", "source_code", "payment_id", "payment_url",
    "paid_at", "admin_msgs",
)


async def create_order(**fields: Any) -> Row:
    """Создать заказ. Номер присваивается по id строки — без гонок при одновременных заказах."""
    data = {k: v for k, v in fields.items() if k in ORDER_FIELDS}
    data["number"] = f"tmp-{uuid.uuid4().hex[:16]}"
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    order_id = await db.execute(
        f"INSERT INTO orders ({cols}) VALUES ({marks})", list(data.values())
    )
    prefix = await get_setting("order_prefix", "F")
    await db.execute("UPDATE orders SET number = ? WHERE id = ?",
                     (f"{prefix}-{order_id:05d}", order_id))
    await add_event(order_id, data.get("status", statuses.NEW), "гость", "Заказ создан")
    row = await get_order(order_id)
    assert row is not None
    return row


async def update_order(order_id: int, **fields: Any) -> None:
    data = {k: v for k, v in fields.items() if k in ORDER_FIELDS}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    await db.execute(
        f"UPDATE orders SET {sets}, updated_at = datetime('now') WHERE id = ?",
        [*data.values(), order_id],
    )


async def get_order(order_id: int) -> Optional[Row]:
    return await db.fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))


async def get_order_by_number(number: str) -> Optional[Row]:
    return await db.fetchone(
        "SELECT * FROM orders WHERE number = ? COLLATE NOCASE", (number.strip(),)
    )


async def get_order_by_payment(payment_id: str) -> Optional[Row]:
    return await db.fetchone("SELECT * FROM orders WHERE payment_id = ?", (payment_id,))


async def set_status(order_id: int, status: str, actor: str = "", note: str = "") -> None:
    await db.execute(
        "UPDATE orders SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, order_id),
    )
    await add_event(order_id, status, actor, note)


async def add_event(order_id: int, status: str, actor: str = "", note: str = "") -> None:
    await db.execute(
        "INSERT INTO order_events (order_id, status, actor, note) VALUES (?, ?, ?, ?)",
        (order_id, status, actor, note),
    )


async def order_events(order_id: int) -> list[Row]:
    return await db.fetchall(
        "SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (order_id,)
    )


async def list_orders(
    *,
    status: str = "",
    object_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    created_from: str = "",
    channel: str = "",
    user_key: tuple[str, str] | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[Row]:
    sql, params = _orders_where(
        status=status, object_id=object_id, date_from=date_from, date_to=date_to,
        created_from=created_from, channel=channel, user_key=user_key,
    )
    return await db.fetchall(
        f"SELECT * FROM orders {sql} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
    )


async def count_orders(
    *,
    status: str = "",
    object_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    created_from: str = "",
    channel: str = "",
    user_key: tuple[str, str] | None = None,
) -> int:
    sql, params = _orders_where(
        status=status, object_id=object_id, date_from=date_from, date_to=date_to,
        created_from=created_from, channel=channel, user_key=user_key,
    )
    return int(await db.fetchval(f"SELECT COUNT(*) FROM orders {sql}", params, 0))


def _orders_where(
    *,
    status: str = "",
    object_id: int = 0,
    date_from: str = "",
    date_to: str = "",
    created_from: str = "",
    channel: str = "",
    user_key: tuple[str, str] | None = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        parts = [s for s in status.split(",") if s]
        where.append("status IN (%s)" % ", ".join("?" for _ in parts))
        params += parts
    if object_id:
        where.append("object_id = ?")
        params.append(object_id)
    if date_from:
        where.append("delivery_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("delivery_date <= ?")
        params.append(date_to)
    if created_from:
        where.append("created_at >= ?")
        params.append(created_from)
    if channel:
        where.append("channel = ?")
        params.append(channel)
    if user_key:
        where.append("channel = ? AND ext_id = ?")
        params += [user_key[0], str(user_key[1])]
    return ("WHERE " + " AND ".join(where) if where else "", params)


async def orders_for_delivery(day: str, status_list: Sequence[str]) -> list[Row]:
    marks = ", ".join("?" for _ in status_list)
    return await db.fetchall(
        f"""SELECT * FROM orders
            WHERE delivery_date = ? AND status IN ({marks})
            ORDER BY object_address, object_title,
                     CAST(apartment AS INTEGER), apartment, id""",
        [day, *status_list],
    )


async def pending_payments() -> list[Row]:
    return await db.fetchall(
        "SELECT * FROM orders WHERE payment_id <> '' AND status = ? ORDER BY id",
        (statuses.ACCEPTED,),
    )


async def stats_by_object(date_from: str, date_to: str) -> list[Row]:
    return await db.fetchall(
        """SELECT o.object_id,
                  COALESCE(ob.title, o.object_title) AS title,
                  COALESCE(ob.group_title, '')       AS group_title,
                  COUNT(*)                           AS orders_count,
                  SUM(o.qty)                         AS sets_count,
                  SUM(CASE WHEN o.status IN ('paid','delivered','received')
                           THEN o.total_kop ELSE 0 END) AS revenue_kop,
                  SUM(o.total_kop)                   AS gross_kop
           FROM orders o
           LEFT JOIN objects ob ON ob.id = o.object_id
           WHERE o.delivery_date BETWEEN ? AND ?
             AND o.status NOT IN ('rejected','cancelled')
           GROUP BY o.object_id
           ORDER BY revenue_kop DESC""",
        (date_from, date_to),
    )


async def stats_by_source(date_from: str, date_to: str) -> list[Row]:
    return await db.fetchall(
        """SELECT source_code, COUNT(*) AS orders_count, SUM(qty) AS sets_count
           FROM orders
           WHERE delivery_date BETWEEN ? AND ? AND status NOT IN ('rejected','cancelled')
           GROUP BY source_code ORDER BY orders_count DESC""",
        (date_from, date_to),
    )


async def stats_totals(date_from: str, date_to: str) -> dict[str, int]:
    row = await db.fetchone(
        """SELECT COUNT(*) AS cnt,
                  COALESCE(SUM(qty), 0) AS sets,
                  COALESCE(SUM(CASE WHEN status IN ('paid','delivered','received')
                                    THEN total_kop ELSE 0 END), 0) AS revenue
           FROM orders
           WHERE delivery_date BETWEEN ? AND ? AND status NOT IN ('rejected','cancelled')""",
        (date_from, date_to),
    )
    if row is None:
        return {"cnt": 0, "sets": 0, "revenue": 0}
    return {"cnt": row["cnt"], "sets": row["sets"], "revenue": row["revenue"]}


# ============================================================ допредложения
OFFER_FIELDS = ("title", "description", "photo_path", "url", "button_text", "is_active", "sort_order")


async def list_offers(active_only: bool = False) -> list[Row]:
    sql = "SELECT * FROM offers"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY sort_order, id"
    return await db.fetchall(sql)


async def get_offer(offer_id: int) -> Optional[Row]:
    return await db.fetchone("SELECT * FROM offers WHERE id = ?", (offer_id,))


async def create_offer(**fields: Any) -> int:
    data = {k: v for k, v in fields.items() if k in OFFER_FIELDS}
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return await db.execute(f"INSERT INTO offers ({cols}) VALUES ({marks})", list(data.values()))


async def update_offer(offer_id: int, **fields: Any) -> None:
    data = {k: v for k, v in fields.items() if k in OFFER_FIELDS}
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    await db.execute(f"UPDATE offers SET {sets} WHERE id = ?", [*data.values(), offer_id])


async def delete_offer(offer_id: int) -> None:
    await db.execute("DELETE FROM offers WHERE id = ?", (offer_id,))


# ============================================================== кэш вложений
async def get_media_ref(path: str, channel: str) -> str:
    value = await db.fetchval(
        "SELECT ref FROM media_cache WHERE path = ? AND channel = ?", (path, channel)
    )
    return str(value or "")


async def set_media_ref(path: str, channel: str, ref: str) -> None:
    await db.execute(
        "INSERT INTO media_cache (path, channel, ref) VALUES (?, ?, ?) "
        "ON CONFLICT(path, channel) DO UPDATE SET ref = excluded.ref",
        (path, channel, ref),
    )


async def drop_media(path: str, channel: str = "") -> None:
    if channel:
        await db.execute("DELETE FROM media_cache WHERE path = ? AND channel = ?", (path, channel))
    else:
        await db.execute("DELETE FROM media_cache WHERE path = ?", (path,))


# ================================================================== выгрузки
async def save_digest(day: str, body: str, auto: bool) -> int:
    return await db.execute(
        "INSERT INTO digests (d, body, auto) VALUES (?, ?, ?)", (day, body, 1 if auto else 0)
    )


async def digest_exists_today(day: str) -> bool:
    row = await db.fetchone(
        "SELECT id FROM digests WHERE d = ? AND auto = 1 AND created_at >= datetime('now', '-20 hours')",
        (day,),
    )
    return row is not None


async def last_digests(limit: int = 5) -> list[Row]:
    return await db.fetchall("SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,))


# ========================================================== состояние админа
async def get_admin_state(admin_id: int) -> tuple[str, dict[str, Any]]:
    row = await db.fetchone("SELECT state, data FROM admin_state WHERE admin_id = ?", (admin_id,))
    if row is None:
        return "", {}
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        data = {}
    return row["state"], data


async def set_admin_state(admin_id: int, state: str, data: Optional[dict[str, Any]] = None) -> None:
    await db.execute(
        "INSERT INTO admin_state (admin_id, state, data) VALUES (?, ?, ?) "
        "ON CONFLICT(admin_id) DO UPDATE SET state = excluded.state, data = excluded.data",
        (admin_id, state, json.dumps(data or {}, ensure_ascii=False)),
    )


async def clear_admin_state(admin_id: int) -> None:
    await db.execute("DELETE FROM admin_state WHERE admin_id = ?", (admin_id,))


# ================================================================== хелперы
def order_price_line(order: Row) -> str:
    return f"{fmt_money(order['price_kop'])} × {order['qty']} = {fmt_money(order['total_kop'])}"


def order_title(order: Row) -> str:
    return f"№{order['number']} · {statuses.label(order['status'])}"


def json_loads(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default
