"""Fatucci — бот заказа завтраков в апартаменты.

Один процесс, один код — два мессенджера: Telegram и MAX.
Запуск:  python main.py
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app import db, net, repo, scheduler
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


async def max_supervisor(channel: MaxChannel) -> None:
    """Держит MAX включённым, пока он включён в админ-панели."""
    while True:
        if await repo.get_bool("max_enabled", True):
            try:
                await channel.run(route, enabled=lambda: repo.get_bool("max_enabled", True))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("MAX: цикл опроса упал, перезапуск через 15 с")
                await asyncio.sleep(15)
        await asyncio.sleep(20)


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
    if not cfg.admin_ids:
        log.warning(
            "Не задан ADMIN_IDS — админ-панель будет недоступна. "
            "Свой ID можно узнать, отправив боту /id."
        )

    await db.init_db()

    tasks: list[asyncio.Task] = []

    telegram = TelegramChannel(cfg.telegram_token, cfg.telegram_username)
    telegram.setup(route)
    base.REGISTRY[base.TG] = telegram
    tasks.append(asyncio.create_task(telegram_supervisor(telegram), name="telegram"))

    if cfg.max_token or cfg.max_username:
        max_channel = MaxChannel(cfg.max_token, cfg.max_username)
        base.REGISTRY[base.MAX] = max_channel
        if cfg.max_token:
            tasks.append(asyncio.create_task(max_supervisor(max_channel), name="max"))
        else:
            log.info("MAX_TOKEN не задан — MAX используется только для ссылок в QR.")
    else:
        log.info("MAX не подключён. Добавьте MAX_TOKEN в .env, когда будет готов бот в MAX.")

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
