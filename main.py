"""Fatucci — бот заказа завтраков в апартаменты.

Один процесс, один код — два мессенджера: Telegram и MAX.
Запуск:  python main.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

from app import admins, db, net, repo, scheduler
from app.channels import base
from app.channels.max import MaxChannel
from app.channels.telegram import TelegramChannel
from app.config import cfg
from app.router import route

log = logging.getLogger("fatucci")


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%d.%m %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def telegram_supervisor(channel: TelegramChannel) -> None:
    """Держит Telegram-бота живым: сеть на хостинге может моргать."""
    from aiogram.exceptions import TelegramUnauthorizedError

    while True:
        try:
            await channel.run()
        except asyncio.CancelledError:
            raise
        except TelegramUnauthorizedError:
            log.error("Telegram: токен не принят. Проверьте TELEGRAM_TOKEN в .env "
                      "и перезапустите бота.")
            return
        except Exception as exc:  # noqa: BLE001
            log.error("Telegram: связь потеряна (%s). Повтор через 15 с.", exc)
            await asyncio.sleep(15)


async def max_credentials() -> tuple[str, str]:
    """Токен и username бота MAX: сначала админ-панель, потом переменные окружения."""
    token = (await repo.get_setting("max_token")) or cfg.max_token
    username = (await repo.get_setting("max_username")) or cfg.max_username
    return token.strip(), username.strip().lstrip("@")


async def max_still_on(token: str) -> bool:
    """MAX всё ещё включён и токен не поменяли? Иначе цикл опроса надо остановить."""
    if not await repo.get_bool("max_enabled", True):
        return False
    current, _ = await max_credentials()
    return current == token


async def max_supervisor() -> None:
    """MAX подключается прямо из админ-панели — без правки .env и перезапуска.

    Раз в 15 секунд смотрим, что сейчас записано в настройках: появился токен —
    поднимаем канал, поменяли или выключили — гасим и пересоздаём.
    """
    channel: Optional[MaxChannel] = None
    signature: Optional[tuple[str, str]] = None
    announced = False

    while True:
        token, username = await max_credentials()
        enabled = await repo.get_bool("max_enabled", True)

        if (token, username) != signature:
            if channel is not None:
                await channel.close()
                channel = None
            signature = (token, username)
            announced = False
            if token or username:
                channel = MaxChannel(token, username)
                base.REGISTRY[base.MAX] = channel
            else:
                base.REGISTRY.pop(base.MAX, None)

        if channel is not None and token and enabled:
            try:
                await channel.run(route, enabled=lambda: max_still_on(token))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("MAX: цикл опроса упал, перезапуск через 15 с")
                await asyncio.sleep(15)
        elif not announced:
            announced = True
            if not token:
                log.info("MAX не подключён. Токен вводится в /admin → 📱 Каналы.")
            elif not enabled:
                log.info("MAX выключен переключателем в админ-панели.")

        await asyncio.sleep(15)


async def main() -> None:
    setup_logging()
    net.apply_ca_bundle()
    log.info("Запуск Fatucci-бота…")
    for line in cfg.describe():
        log.info("  %s", line)

    if not cfg.telegram_token:
        log.error(
            "Токен Telegram не найден. Задайте переменную окружения TELEGRAM_TOKEN "
            "(подойдёт и BOT_TOKEN) в панели хостинга — либо положите её в файл .env "
            "рядом с main.py. Запускать нечего."
        )
        return
    await db.init_db()

    admin_count = await admins.count()
    if admin_count:
        log.info("Администраторов в базе: %s", admin_count)
    elif cfg.admin_ids:
        log.info("Доступ выдан по ADMIN_IDS: %s", cfg.admin_ids)
    else:
        log.info("Администраторов пока нет — владельцем станет первый написавший боту.")

    tasks: list[asyncio.Task] = []

    telegram = TelegramChannel(cfg.telegram_token, cfg.telegram_username)
    telegram.setup(route)
    base.REGISTRY[base.TG] = telegram
    tasks.append(asyncio.create_task(telegram_supervisor(telegram), name="telegram"))
    tasks.append(asyncio.create_task(max_supervisor(), name="max"))
    tasks.append(asyncio.create_task(scheduler.run_all(), name="scheduler"))

    log.info("Бот запущен. Админ-панель: команда /admin в Telegram.")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        for channel in base.REGISTRY.values():
            close = getattr(channel, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass
        await db.close()
        log.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
