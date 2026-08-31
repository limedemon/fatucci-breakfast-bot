"""Общий контракт для каналов (Telegram и MAX).

Вся бизнес-логика написана против этих типов, поэтому один и тот же сценарий
работает в обоих мессенджерах без дублирования кода.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

TG = "tg"
MAX = "max"

CHANNEL_TITLES = {TG: "Telegram", MAX: "MAX"}


@dataclass
class Btn:
    """Кнопка под сообщением."""

    text: str
    data: str = ""            # callback-данные
    url: str = ""             # кнопка-ссылка
    contact: bool = False     # запрос контакта (телефона)
    intent: str = ""          # 'positive' / 'negative' — подсветка кнопки в MAX


@dataclass
class Out:
    """Исходящее сообщение в канало-независимом виде."""

    text: str = ""
    kb: Optional[list[list[Btn]]] = None
    photo: str = ""               # ключ картинки в базе (см. app/media.py)
    reply_contact: str = ""       # подпись кнопки «Поделиться контактом»
    disable_preview: bool = True


@dataclass
class Event:
    """Входящее событие в канало-независимом виде."""

    channel: str
    user_id: str
    chat_id: str
    kind: str                     # start | text | callback | contact | payment
    text: str = ""
    payload: str = ""             # start-payload или callback-данные
    phone: str = ""
    username: str = ""
    full_name: str = ""
    message_id: str = ""
    callback_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return self.channel, str(self.user_id)


class Channel(ABC):
    """Адаптер конкретного мессенджера."""

    name: str = ""
    title: str = ""
    username: str = ""   # username бота в этом мессенджере (нужен для QR-ссылок)

    @abstractmethod
    async def send(self, chat_id: str, out: Out) -> str:
        """Отправить сообщение. Возвращает id сообщения (или '' если недоступно)."""

    @abstractmethod
    async def edit(self, chat_id: str, message_id: str, out: Out) -> bool:
        """Отредактировать сообщение. False — если не получилось (тогда шлём новое)."""

    @abstractmethod
    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Ответить на нажатие кнопки (всплывающее уведомление)."""

    @abstractmethod
    def start_link(self, payload: str = "") -> str:
        """Ссылка на бота с deep-link параметром (для QR-кодов)."""

    async def send_or_edit(self, chat_id: str, message_id: str, out: Out) -> str:
        """Отредактировать, а если не вышло — отправить заново."""
        if message_id and not out.photo and not out.reply_contact:
            if await self.edit(chat_id, message_id, out):
                return message_id
        return await self.send(chat_id, out)

    async def reply_to_callback(self, ev: "Event", out: Out) -> str:
        """Обновить сообщение, к которому прикреплена нажатая кнопка."""
        return await self.send_or_edit(ev.chat_id, ev.message_id, out)

    async def send_document(self, chat_id: str, data: bytes, filename: str,
                            caption: str = "") -> bool:
        """Отправить файл из памяти (нужно только админ-панели)."""
        return False

    async def download_bytes(self, file_id: str) -> bytes:
        """Скачать присланный файл в память."""
        return b''

    async def set_description(self, text: str) -> bool:
        """Описание бота до первого запуска (есть только в Telegram)."""
        return False

    async def show_admin_button(self, chat_id: str, text: str) -> bool:
        """Закрепить кнопку админ-панели под полем ввода (есть только в Telegram)."""
        return False

    async def is_chat_admin(self, chat_id: str, user_id: str) -> bool:
        """Админ ли пользователь в этом чате (есть только в Telegram)."""
        return False

    async def show_support_button(self, chat_id: str, text: str) -> bool:
        """Закрепить кнопку «Поддержка» под полем ввода (есть только в Telegram)."""
        return False

    async def send_invoice(self, chat_id: str, title: str, description: str, payload: str,
                           amount_kop: int, provider_token: str, label: str = "К оплате",
                           provider_data: str = "") -> tuple[bool, str]:
        """Счёт на оплату. Есть только в Telegram — в MAX встроенных платежей нет."""
        return False, "В этом мессенджере встроенная оплата недоступна"


#: Заполняется при старте в main.py — {'tg': TelegramChannel, 'max': MaxChannel}
REGISTRY: dict[str, Channel] = {}


def get_channel(name: str) -> Optional[Channel]:
    return REGISTRY.get(name)


def channel_title(name: str) -> str:
    return CHANNEL_TITLES.get(name, name)
