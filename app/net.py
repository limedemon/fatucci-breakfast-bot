"""Сеть и TLS.

Зачем этот модуль:

1. **MAX**. API мессенджера (`platform-api2.max.ru`) работает по сертификату
   НУЦ Минцифры («Russian Trusted Root CA»). Этого корня нет ни в Windows,
   ни в большинстве сборок Linux, ни в certifi — без него бот к MAX не
   подключится вообще. Корневые сертификаты лежат в `app/certs/` и
   добавляются в доверенные **только для соединений с MAX**; для Telegram
   остаётся обычный набор.

2. **Windows**. Системное хранилище сертификатов бывает неполным, поэтому
   всему процессу подставляется набор certifi — иначе падает даже aiogram.

Проверка сертификатов нигде не отключается: мы только добавляем доверенные
корни, а не ослабляем защиту.
"""
from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path
from typing import Optional

import aiohttp
import certifi

log = logging.getLogger(__name__)

CERTS_DIR = Path(__file__).resolve().parent / "certs"

_default_ctx: Optional[ssl.SSLContext] = None
_max_ctx: Optional[ssl.SSLContext] = None


def apply_ca_bundle() -> None:
    """Подсунуть certifi всему процессу — иначе на Windows падает даже aiogram."""
    bundle = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


def default_ssl() -> ssl.SSLContext:
    """Обычное доверие (certifi): Telegram и всё остальное."""
    global _default_ctx
    if _default_ctx is None:
        _default_ctx = ssl.create_default_context(cafile=certifi.where())
    return _default_ctx


def max_ssl() -> ssl.SSLContext:
    """Доверие для MAX: certifi + корневые сертификаты НУЦ Минцифры."""
    global _max_ctx
    if _max_ctx is None:
        ctx = ssl.create_default_context(cafile=certifi.where())
        loaded = 0
        for path in sorted(CERTS_DIR.glob("*.cer")) + sorted(CERTS_DIR.glob("*.pem")):
            try:
                ctx.load_verify_locations(cafile=str(path))
                loaded += 1
            except ssl.SSLError as exc:
                log.warning("Не удалось загрузить сертификат %s: %s", path.name, exc)
        if not loaded:
            log.warning(
                "В %s нет корневых сертификатов Минцифры — подключение к MAX может "
                "не пройти проверку TLS. См. README, раздел про сертификаты.", CERTS_DIR
            )
        _max_ctx = ctx
    return _max_ctx


def connector(context: ssl.SSLContext) -> aiohttp.TCPConnector:
    """Коннектор aiohttp с нужным набором доверенных корней."""
    return aiohttp.TCPConnector(ssl=context, limit=50)
