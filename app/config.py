"""Конфигурация.

Источники, в порядке приоритета:
  1. переменные окружения (панель хостинга, docker, systemd);
  2. файл .env рядом с кодом;
  3. значения по умолчанию.

Файл .env необязателен: если его нет, бот спокойно поднимется на одних
переменных окружения. Всё, что можно менять на лету, лежит не здесь,
а в базе — см. repo.get_setting.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# override=False — переменные окружения важнее файла
load_dotenv(BASE_DIR / ".env", override=False)

#: какие переменные окружения были реально найдены (для лога при старте)
FOUND: dict[str, str] = {}


def _clean(value: str) -> str:
    """Убрать пробелы и кавычки, которые часто прилипают при вставке в панель хостинга."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def env(*names: str, default: str = "") -> str:
    """Первое непустое значение из перечисленных переменных окружения."""
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        value = _clean(raw)
        if value:
            FOUND[names[0]] = name
            return value
    return default


def env_int(*names: str, default: int = 0) -> int:
    raw = env(*names)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _int_list(raw: str) -> list[int]:
    out: list[int] = []
    for part in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


@dataclass
class Config:
    # Telegram. BOT_TOKEN — самое частое имя в панелях хостингов, поддерживаем и его.
    telegram_token: str = field(
        default_factory=lambda: env("TELEGRAM_TOKEN", "BOT_TOKEN", "TG_TOKEN")
    )
    telegram_username: str = field(
        default_factory=lambda: env("TELEGRAM_BOT_USERNAME", "BOT_USERNAME").lstrip("@")
    )

    # MAX
    max_token: str = field(default_factory=lambda: env("MAX_TOKEN", "MAX_BOT_TOKEN"))
    max_username: str = field(
        default_factory=lambda: env("MAX_BOT_USERNAME", "MAX_USERNAME").lstrip("@")
    )

    admin_ids: list[int] = field(
        default_factory=lambda: _int_list(env("ADMIN_IDS", "ADMIN_ID", "ADMINS"))
    )
    orders_chat_id: str = field(default_factory=lambda: env("ORDERS_CHAT_ID", "ORDERS_CHAT"))
    tz_offset: int = field(default_factory=lambda: env_int("TZ_OFFSET", default=3))

    yookassa_shop_id: str = field(
        default_factory=lambda: env("YOOKASSA_SHOP_ID", "YK_SHOP_ID")
    )
    yookassa_secret: str = field(
        default_factory=lambda: env("YOOKASSA_SECRET_KEY", "YK_SECRET_KEY")
    )

    db_path: Path = field(
        default_factory=lambda: BASE_DIR / env("DB_PATH", default="data/bot.db")
    )
    log_level: str = field(default_factory=lambda: env("LOG_LEVEL", default="INFO").upper())

    data_dir: Path = BASE_DIR / "data"
    photos_dir: Path = BASE_DIR / "data" / "photos"
    export_dir: Path = BASE_DIR / "data" / "export"

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.tz_offset))

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.photos_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def describe(self) -> list[str]:
        """Строки для лога при старте — что нашли и откуда, без утечки секретов."""
        env_file = "есть" if (BASE_DIR / ".env").exists() else "нет"
        lines = [f"Файл .env: {env_file}. Переменные окружения имеют приоритет."]
        lines.append(f"Telegram-токен: {_mask(self.telegram_token)}"
                     + (f" (из {FOUND['TELEGRAM_TOKEN']})" if "TELEGRAM_TOKEN" in FOUND else ""))
        lines.append(f"MAX-токен: {_mask(self.max_token)}"
                     + (f" (из {FOUND['MAX_TOKEN']})" if "MAX_TOKEN" in FOUND else ""))
        lines.append(f"Админы: {self.admin_ids or 'не заданы'}")
        lines.append(f"Чат заказов: {self.orders_chat_id or 'не задан (укажите в /admin)'}")
        lines.append(f"Часовой пояс: UTC+{self.tz_offset}")
        return lines


def _mask(secret: str) -> str:
    if not secret:
        return "не задан"
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 12 else "задан"


cfg = Config()
cfg.ensure_dirs()
