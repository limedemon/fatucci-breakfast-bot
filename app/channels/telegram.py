"""Адаптер Telegram (aiogram 3)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .. import repo
from .base import TG, Btn, Channel, Event, Out

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096

Router = Callable[[Event, Channel], Awaitable[None]]


class TelegramChannel(Channel):
    name = TG
    title = "Telegram"

    def __init__(self, token: str, username: str = "") -> None:
        self.bot = Bot(
            token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        self.username = username
        self.dp = Dispatcher()

    # ------------------------------------------------------------- отправка
    async def send(self, chat_id: str, out: Out) -> str:
        try:
            if out.remove_reply_kb and (out.kb or out.photo):
                await self._drop_reply_keyboard(chat_id)
            if out.photo:
                return await self._send_photo(chat_id, out)
            msg = await self.bot.send_message(
                chat_id=chat_id,
                text=_cut(out.text or "…", TEXT_LIMIT),
                reply_markup=self._markup(out),
                disable_notification=False,
            )
            return str(msg.message_id)
        except TelegramForbiddenError:
            log.info("TG: пользователь %s заблокировал бота", chat_id)
        except TelegramAPIError as exc:
            log.warning("TG send error (%s): %s", chat_id, exc)
        return ""

    async def _send_photo(self, chat_id: str, out: Out) -> str:
        path = Path(out.photo)
        if not path.exists():
            plain = Out(text=out.text, kb=out.kb, reply_contact=out.reply_contact,
                        remove_reply_kb=out.remove_reply_kb)
            plain.photo = ""
            return await self.send(chat_id, plain)

        cached = await repo.get_media_ref(str(path), TG)
        photo: object = cached or FSInputFile(str(path))
        long_text = len(out.text) > CAPTION_LIMIT
        caption = "" if long_text else out.text
        markup = None if long_text else self._markup(out)
        try:
            msg = await self.bot.send_photo(
                chat_id=chat_id, photo=photo, caption=caption or None, reply_markup=markup
            )
        except TelegramAPIError as exc:
            if cached:  # протухший file_id — грузим файл заново
                await repo.drop_media(str(path), TG)
                msg = await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(str(path)),
                    caption=caption or None,
                    reply_markup=markup,
                )
            else:
                log.warning("TG send_photo error: %s", exc)
                return ""
        if msg.photo:
            await repo.set_media_ref(str(path), TG, msg.photo[-1].file_id)
        if long_text:
            tail = await self.bot.send_message(
                chat_id=chat_id, text=_cut(out.text, TEXT_LIMIT), reply_markup=self._markup(out)
            )
            return str(tail.message_id)
        return str(msg.message_id)

    async def _drop_reply_keyboard(self, chat_id: str) -> None:
        """Снять reply-клавиатуру: служебное сообщение, которое сразу удаляем."""
        try:
            msg = await self.bot.send_message(chat_id, "⌛", reply_markup=ReplyKeyboardRemove())
            await self.bot.delete_message(chat_id, msg.message_id)
        except TelegramAPIError:
            pass

    async def edit(self, chat_id: str, message_id: str, out: Out) -> bool:
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=_cut(out.text or "…", TEXT_LIMIT),
                reply_markup=self._inline(out.kb),
            )
            return True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return True
            return False
        except TelegramAPIError:
            return False

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self.bot.answer_callback_query(callback_id, text=text or None, show_alert=False)
        except TelegramAPIError:
            pass

    async def send_file(self, chat_id: str, path: str, caption: str = "") -> bool:
        try:
            await self.bot.send_document(chat_id, FSInputFile(path), caption=caption or None)
            return True
        except TelegramAPIError as exc:
            log.warning("TG send_file error: %s", exc)
            return False

    async def send_bytes(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> bool:
        try:
            await self.bot.send_photo(
                chat_id, BufferedInputFile(data, filename), caption=caption or None
            )
            return True
        except TelegramAPIError as exc:
            log.warning("TG send_bytes error: %s", exc)
            return False

    async def download_photo(self, file_id: str, dest: Path) -> bool:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            await self.bot.download(file_id, destination=dest)
            return True
        except TelegramAPIError as exc:
            log.warning("TG download error: %s", exc)
            return False

    def start_link(self, payload: str = "") -> str:
        base = f"https://t.me/{self.username}" if self.username else "https://t.me/"
        return f"{base}?start={payload}" if payload else base

    # ------------------------------------------------------------ клавиатуры
    def _markup(self, out: Out):
        if out.reply_contact:
            rows = [[KeyboardButton(text=out.reply_contact, request_contact=True)]]
            for row in out.kb or []:
                labels = [KeyboardButton(text=b.text) for b in row if not b.url]
                if labels:
                    rows.append(labels)
            return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)
        if out.remove_reply_kb and not out.kb:
            return ReplyKeyboardRemove()
        if out.remove_reply_kb and out.kb:
            # inline-клавиатуру и снятие reply-клавиатуры в одном сообщении не совместить:
            # reply-клавиатуру снимет предыдущий шаг, здесь отдаём inline.
            return self._inline(out.kb)
        return self._inline(out.kb)

    @staticmethod
    def _inline(kb: Optional[list[list[Btn]]]) -> Optional[InlineKeyboardMarkup]:
        if not kb:
            return None
        rows = []
        for row in kb:
            buttons = []
            for btn in row:
                if btn.url:
                    buttons.append(InlineKeyboardButton(text=btn.text, url=btn.url))
                elif btn.contact:
                    continue  # в Telegram контакт запрашивается reply-кнопкой
                else:
                    buttons.append(InlineKeyboardButton(text=btn.text, callback_data=btn.data[:64]))
            if buttons:
                rows.append(buttons)
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    # -------------------------------------------------------------- события
    def setup(self, route: Router) -> None:
        dp = self.dp

        @dp.message(CommandStart(deep_link=True))
        async def _start_deep(message: Message, command: CommandObject) -> None:
            await route(self._event(message, "start", payload=command.args or ""), self)

        @dp.message(CommandStart())
        async def _start(message: Message) -> None:
            await route(self._event(message, "start"), self)

        @dp.message(Command("admin", "menu", "help", "id"))
        async def _cmd(message: Message) -> None:
            await route(self._event(message, "text", text=message.text or ""), self)

        @dp.message(F.contact)
        async def _contact(message: Message) -> None:
            phone = message.contact.phone_number if message.contact else ""
            await route(self._event(message, "contact", phone=phone), self)

        @dp.message(F.photo)
        async def _photo(message: Message) -> None:
            ev = self._event(message, "text", text=message.caption or "")
            ev.raw["photo_file_id"] = message.photo[-1].file_id
            await route(ev, self)

        @dp.message(F.text)
        async def _text(message: Message) -> None:
            await route(self._event(message, "text", text=message.text or ""), self)

        @dp.callback_query()
        async def _callback(query: CallbackQuery) -> None:
            message = query.message
            ev = Event(
                channel=TG,
                user_id=str(query.from_user.id),
                chat_id=str(message.chat.id) if message else str(query.from_user.id),
                kind="callback",
                payload=query.data or "",
                callback_id=query.id,
                message_id=str(message.message_id) if message else "",
                username=query.from_user.username or "",
                full_name=_full_name(query.from_user.first_name, query.from_user.last_name),
            )
            await route(ev, self)

    def _event(self, message: Message, kind: str, **kwargs) -> Event:
        user = message.from_user
        return Event(
            channel=TG,
            user_id=str(user.id) if user else "",
            chat_id=str(message.chat.id),
            kind=kind,
            username=(user.username or "") if user else "",
            full_name=_full_name(user.first_name, user.last_name) if user else "",
            message_id=str(message.message_id),
            **kwargs,
        )

    # ---------------------------------------------------------------- запуск
    async def run(self) -> None:
        me = await self.bot.get_me()
        if not self.username:
            self.username = me.username or ""
        log.info("Telegram-бот запущен: @%s", self.username)
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        await self.bot.session.close()


def _full_name(first: Optional[str], last: Optional[str]) -> str:
    return " ".join(part for part in (first, last) if part).strip()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
