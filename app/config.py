"""Конфигурация из .env. Всё, что можно менять на лету, лежит в БД (см. repo.settings)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int_list(raw: str) -> list[int]:
    out: list[int] = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


@dataclass
class Config:
    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "").strip()
    telegram_username: str = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    max_token: str = os.getenv("MAX_TOKEN", "").strip()
    max_username: str = os.getenv("MAX_BOT_USERNAME", "").strip().lstrip("@")
    admin_ids: list[int] = field(default_factory=lambda: _int_list(os.getenv("ADMIN_IDS", "")))
    orders_chat_id: str = os.getenv("ORDERS_CHAT_ID", "").strip()
    tz_offset: int = int(os.getenv("TZ_OFFSET", "3") or 3)
    yookassa_shop_id: str = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    yookassa_secret: str = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    db_path: Path = BASE_DIR / os.getenv("DB_PATH", "data/bot.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

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


cfg = Config()
cfg.ensure_dirs()
