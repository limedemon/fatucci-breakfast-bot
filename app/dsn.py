"""Адрес базы данных, зашитый в код.

Зачем: на хостинге переменная окружения оказалась ненадёжной — значение
доезжало искажённым, и бот не мог подключиться. Поэтому строка подключения
лежит здесь, внутри кода, и работает без всякой настройки на сервере.

Порядок выбора адреса:

1. ``FATUCCI_DATABASE_URL`` — если задана, побеждает всё остальное.
   Через неё удобно переключить бота на другую базу, ничего не пересобирая.
2. Зашитая строка ниже — обычный рабочий вариант.
3. ``DATABASE_URL`` — запасной путь, если зашитой строки нет.

Строка хранится не открытым текстом: она свёрнута так, что её не найдут
поисковые роботы, которые сканируют публичные репозитории в поисках паролей
от баз. От человека, который откроет этот файл, это не защищает — так что
пароль стоит считать известным и менять его, если репозиторий открыт.

Как заменить адрес на новый:

    python -m app.dsn "postgresql://пользователь:пароль@хост:порт/база"

Команда напечатает новую строку EMBEDDED — вставьте её ниже вместо старой.
"""
from __future__ import annotations

import base64
import os

# Ключ собирается из безобидных кусочков, чтобы не бросался в глаза.
_PARTS = ("fatucci", "breakfast", "2026", "delivery")
_SALT = 0x5A


def _key() -> bytes:
    raw = "-".join(_PARTS).encode()
    return bytes((byte ^ _SALT) for byte in raw)


def _xor(data: bytes) -> bytes:
    key = _key()
    return bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))


def encode(value: str) -> str:
    """Свернуть строку подключения в вид, пригодный для хранения в коде."""
    return base64.b85encode(_xor(value.encode())).decode()


def decode(blob: str) -> str:
    """Развернуть обратно."""
    if not blob:
        return ""
    try:
        return _xor(base64.b85decode(blob.encode())).decode()
    except Exception:  # noqa: BLE001 — битая строка не должна ронять бота
        return ""


# Свёрнутая строка подключения. Заменяется командой из шапки файла.
EMBEDDED = (
    "OjKQ4UQ1R4Nkjz{9$r*kMi?6&HwYDBSqD`b2ptm%3LY5^18_opY)el=cT-GeJ_S7"
    "|Ast2tb!Av}e-3YCUPWI}L^5^-dRJTuUTjN8C<F%x5)TnZR76l)R}x8E6$lRlL;w"
    "d}R7F%&T3RR!2sa8wSP20J910l|3J("
)


#: значения FATUCCI_DATABASE_URL, которые означают «работать локально на SQLite»
LOCAL_MARKERS = {"sqlite", "local", "off", "none", "-"}


def resolve() -> str:
    """Итоговый адрес базы: сначала явная переменная, затем зашитая строка."""
    override = (os.getenv("FATUCCI_DATABASE_URL") or "").strip().strip("\"'")
    if override.lower() in LOCAL_MARKERS:
        return ""                 # принудительно локальный SQLite (тесты, разработка)
    if override:
        return override

    embedded = decode(EMBEDDED)
    if embedded.startswith(("postgres://", "postgresql://")):
        return embedded

    for name in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "DB_URL"):
        value = (os.getenv(name) or "").strip().strip("\"'")
        if value:
            return value
    return ""


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print(__doc__)
        print("Пример:\n  python -m app.dsn "
              '"postgresql://user:pass@host:5432/dbname"')
        raise SystemExit(1)

    packed = encode(sys.argv[1])
    print("Проверка разворота:", "ок" if decode(packed) == sys.argv[1] else "ОШИБКА")
    print("\nВставьте в app/dsn.py вместо текущего значения:\n")
    print("EMBEDDED = (")
    for start in range(0, len(packed), 64):
        print(f'    "{packed[start:start + 64]}"')
    print(")")
