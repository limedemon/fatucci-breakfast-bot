"""Прогон самопроверки на настоящем PostgreSQL.

Запуск:
    python selftest_pg.py

Берёт адрес базы из DATABASE_URL и создаёт для теста **отдельную схему**,
которую удаляет в конце. Боевые таблицы при этом не трогаются, поэтому
скрипт безопасно запускать и на рабочей базе.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

SCHEMA = "bot_selftest"


def dsn() -> str:
    """Адрес базы: из окружения или из .env — но в окружение .env не подмешиваем."""
    names = ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "DB_URL")
    for name in names:
        value = (os.getenv(name) or "").strip().strip("\"'")
        if value:
            return value
    # адрес зашит в коде — берём его оттуда
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from app.dsn import decode, EMBEDDED

        embedded = decode(EMBEDDED)
        if embedded.startswith(("postgres://", "postgresql://")):
            return embedded
    except Exception:  # noqa: BLE001
        pass

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in names and value.strip():
                return value.strip().strip("\"'")
    return ""


async def prepare(url: str, drop_only: bool = False) -> None:
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        if not drop_only:
            await conn.execute(f"CREATE SCHEMA {SCHEMA}")
    finally:
        await conn.close()


def main() -> int:
    url = dsn()
    if not url:
        print("DATABASE_URL не задан — проверять нечего.")
        return 1

    print(f"Готовлю временную схему {SCHEMA}…")
    asyncio.run(prepare(url))

    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env["FATUCCI_DATABASE_URL"] = url
    env["DB_SCHEMA"] = SCHEMA
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("DB_PATH", None)

    print(f"Прогоняю selftest.py на PostgreSQL (схема {SCHEMA})\n")
    result = subprocess.run([sys.executable, "selftest.py"], env=env,
                            cwd=os.path.dirname(os.path.abspath(__file__)))

    print(f"\nУбираю схему {SCHEMA}…")
    asyncio.run(prepare(url, drop_only=True))
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
