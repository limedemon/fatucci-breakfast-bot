"""Адаптер мессенджера MAX (max.ru), Bot API.

Работает поверх официального HTTP API:
    базовый адрес   https://platform-api2.max.ru
    авторизация     заголовок Authorization: <token>
    события         long polling GET /updates
    отправка        POST /messages?chat_id=... | ?user_id=...
    редактирование  PUT /messages?message_id=...  и POST /answers?callback_id=...
    вложения        POST /uploads?type=image  ->  загрузка файла  ->  token

Библиотек сверх aiohttp не требуется — так меньше рисков при обновлениях API.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .. import net, repo
from ..utils import strip_html
from .base import MAX, Btn, Channel, Event, Out

log = logging.getLogger(__name__)

API_BASE = "https://platform-api2.max.ru"
TEXT_LIMIT = 4000

Router = Callable[[Event, Channel], Awaitable[None]]


class MaxApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"MAX API {status}: {body}")
        self.status = status
        self.body = body


class MaxChannel(Channel):
    name = MAX
    title = "MAX"

    def __init__(self, token: str, username: str = "", api_base: str = API_BASE) -> None:
        self.token = token
        self.username = username
        self.api_base = api_base.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._marker: Optional[int] = None
        self._running = False
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------- транспорт
    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self.token},
                timeout=aiohttp.ClientTimeout(total=120),
                connector=net.connector(net.max_ssl()),
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        session = await self.session()
        url = f"{self.api_base}{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                async with session.request(method, url, params=clean, json=json_body) as resp:
                    text = await resp.text()
                    if resp.status == 429:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    if resp.status >= 400:
                        raise MaxApiError(resp.status, text[:400])
                    if not text:
                        return {}
                    try:
                        return await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        return {}
            except MaxApiError as exc:
                # «вложение ещё не готово» — единственная ошибка, которую есть смысл повторить
                if "not.ready" in exc.body and attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    last_error = exc
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
        if last_error:
            raise last_error
        return {}

    # -------------------------------------------------------------- отправка
    async def send(self, chat_id: str, out: Out) -> str:
        body = await self._body(out)
        params = self._target(chat_id)
        try:
            async with self._send_lock:
                data = await self._request("POST", "/messages", params=params, json_body=body)
                await asyncio.sleep(0.35)  # лимит MAX: не чаще 2 сообщений в секунду
        except MaxApiError as exc:
            if _is_format_error(exc):
                body.pop("format", None)
                body["text"] = strip_html(body.get("text", ""))
                try:
                    data = await self._request("POST", "/messages", params=params, json_body=body)
                except MaxApiError as exc2:
                    log.warning("MAX send error (%s): %s", chat_id, exc2)
                    return ""
            else:
                log.warning("MAX send error (%s): %s", chat_id, exc)
                return ""
        except Exception as exc:  # noqa: BLE001 — сеть не должна ронять бота
            log.warning("MAX send failed (%s): %s", chat_id, exc)
            return ""
        return str(((data or {}).get("message") or {}).get("body", {}).get("mid", ""))

    async def edit(self, chat_id: str, message_id: str, out: Out) -> bool:
        if not message_id:
            return False
        body = await self._body(out)
        try:
            await self._request("PUT", "/messages", params={"message_id": message_id}, json_body=body)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("MAX edit не удался (%s): %s", message_id, exc)
            return False

    async def reply_to_callback(self, ev: Event, out: Out) -> str:
        """Обновить сообщение с кнопкой — штатный для MAX способ навигации."""
        if not ev.callback_id:
            return await self.send(ev.chat_id, out)
        body = await self._body(out)
        try:
            await self._request(
                "POST", "/answers", params={"callback_id": ev.callback_id},
                json_body={"message": body},
            )
            ev.raw["_answered"] = True
            return ev.message_id
        except Exception as exc:  # noqa: BLE001
            log.debug("MAX answers не удался: %s", exc)
            return await self.send(ev.chat_id, out)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        if not callback_id:
            return
        try:
            await self._request(
                "POST", "/answers", params={"callback_id": callback_id},
                json_body={"notification": text[:200]} if text else {},
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("MAX answer_callback: %s", exc)

    def start_link(self, payload: str = "") -> str:
        base = f"https://max.ru/{self.username}" if self.username else "https://max.ru/"
        return f"{base}?start={payload}" if payload else base

    # ------------------------------------------------------------ сообщение
    def _target(self, chat_id: str) -> dict[str, Any]:
        raw = str(chat_id)
        if raw.startswith("u"):          # внутренняя пометка «слать по user_id»
            return {"user_id": int(raw[1:])}
        try:
            return {"chat_id": int(raw)}
        except ValueError:
            return {"chat_id": raw}

    async def _body(self, out: Out) -> dict[str, Any]:
        attachments: list[dict[str, Any]] = []
        if out.photo:
            token = await self._image_token(out.photo)
            if token:
                attachments.append({"type": "image", "payload": {"token": token}})
        keyboard = self._keyboard(out)
        if keyboard:
            attachments.append(keyboard)
        body: dict[str, Any] = {
            "text": _cut(out.text or "…", TEXT_LIMIT),
            "format": "html",
            "notify": True,
        }
        if attachments:
            body["attachments"] = attachments
        return body

    def _keyboard(self, out: Out) -> Optional[dict[str, Any]]:
        rows: list[list[dict[str, Any]]] = []
        if out.reply_contact:
            rows.append([{"type": "request_contact", "text": out.reply_contact}])
        for row in out.kb or []:
            buttons: list[dict[str, Any]] = []
            for btn in row:
                buttons.append(_button(btn))
            if buttons:
                rows.append(buttons)
        if not rows:
            return None
        return {"type": "inline_keyboard", "payload": {"buttons": rows}}

    async def _image_token(self, path_str: str) -> str:
        path = Path(path_str)
        if not path.exists():
            return ""
        cached = await repo.get_media_ref(str(path), MAX)
        if cached:
            return cached
        try:
            upload = await self._request("POST", "/uploads", params={"type": "image"})
            url = upload.get("url", "")
            if not url:
                return ""
            session = await self.session()
            form = aiohttp.FormData()
            form.add_field("data", path.read_bytes(), filename=path.name,
                           content_type="application/octet-stream")
            async with session.post(url, data=form) as resp:
                payload = await resp.json(content_type=None)
            token = _extract_photo_token(payload) or upload.get("token", "")
            if token:
                await repo.set_media_ref(str(path), MAX, token)
            return token
        except Exception as exc:  # noqa: BLE001
            log.warning("MAX: не удалось загрузить фото %s: %s", path, exc)
            return ""

    # -------------------------------------------------------------- события
    def _parse(self, upd: dict[str, Any]) -> Optional[Event]:
        kind = upd.get("update_type", "")

        if kind == "bot_started":
            user = upd.get("user") or {}
            return Event(
                channel=MAX,
                user_id=str(user.get("user_id", "")),
                chat_id=str(upd.get("chat_id") or f"u{user.get('user_id', '')}"),
                kind="start",
                payload=str(upd.get("payload") or ""),
                username=user.get("username", "") or "",
                full_name=_name(user),
                raw=upd,
            )

        if kind == "message_created":
            message = upd.get("message") or {}
            sender = message.get("sender") or {}
            recipient = message.get("recipient") or {}
            body = message.get("body") or {}
            chat_id = recipient.get("chat_id") or f"u{sender.get('user_id', '')}"
            phone = _phone_from_attachments(body.get("attachments") or [])
            text = body.get("text") or ""
            payload_start = _start_payload(text)
            base = Event(
                channel=MAX,
                user_id=str(sender.get("user_id", "")),
                chat_id=str(chat_id),
                kind="text",
                text=text,
                username=sender.get("username", "") or "",
                full_name=_name(sender),
                message_id=str(body.get("mid", "")),
                raw=upd,
            )
            if phone:
                base.kind = "contact"
                base.phone = phone
            elif payload_start is not None:
                base.kind = "start"
                base.payload = payload_start
            return base

        if kind == "message_callback":
            callback = upd.get("callback") or {}
            message = upd.get("message") or {}
            user = callback.get("user") or {}
            recipient = message.get("recipient") or {}
            body = message.get("body") or {}
            chat_id = recipient.get("chat_id") or f"u{user.get('user_id', '')}"
            return Event(
                channel=MAX,
                user_id=str(user.get("user_id", "")),
                chat_id=str(chat_id),
                kind="callback",
                payload=str(callback.get("payload") or ""),
                callback_id=str(callback.get("callback_id") or ""),
                message_id=str(body.get("mid", "")),
                username=user.get("username", "") or "",
                full_name=_name(user),
                raw=upd,
            )
        return None

    # ---------------------------------------------------------------- запуск
    async def run(self, route: Router, enabled=None) -> None:
        """Long polling. `enabled` — корутина-проверка, что канал ещё включён в админке."""
        self._running = True
        try:
            me = await self._request("GET", "/me")
            if not self.username:
                self.username = me.get("username", "") or ""
            log.info("MAX-бот запущен: @%s (%s)", self.username, me.get("name", ""))
        except Exception as exc:  # noqa: BLE001
            log.error("MAX: не удалось получить /me — проверьте MAX_TOKEN (%s)", exc)
            return

        backoff = 1
        while self._running:
            if enabled is not None and not await enabled():
                log.info("MAX отключён в админ-панели — останавливаю опрос")
                return
            try:
                data = await self._request(
                    "GET", "/updates",
                    params={"limit": 100, "timeout": 30, "marker": self._marker},
                    retries=0,
                )
                backoff = 1
            except Exception as exc:  # noqa: BLE001
                log.warning("MAX polling error: %s", exc)
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2
                continue

            for upd in data.get("updates") or []:
                event = self._parse(upd)
                if event is None:
                    continue
                try:
                    await route(event, self)
                except Exception:  # noqa: BLE001
                    log.exception("MAX: ошибка обработки события")
            self._marker = data.get("marker") or self._marker

    async def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()


# ------------------------------------------------------------------ хелперы
def _button(btn: Btn) -> dict[str, Any]:
    if btn.url:
        return {"type": "link", "text": btn.text, "url": btn.url}
    if btn.contact:
        return {"type": "request_contact", "text": btn.text}
    button: dict[str, Any] = {"type": "callback", "text": btn.text, "payload": btn.data[:1024]}
    if btn.intent in ("positive", "negative"):
        button["intent"] = btn.intent
    return button


def _name(user: dict[str, Any]) -> str:
    return (user.get("name") or user.get("first_name") or "").strip()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_format_error(exc: MaxApiError) -> bool:
    body = exc.body.lower()
    return exc.status == 400 and ("format" in body or "markup" in body or "html" in body)


TEL_RE = re.compile(r"TEL[^:]*:\s*\+?([\d\s()\-]{6,})", re.IGNORECASE)


def _phone_from_attachments(attachments: list[dict[str, Any]]) -> str:
    for att in attachments:
        if att.get("type") != "contact":
            continue
        payload = att.get("payload") or {}
        vcf = payload.get("vcf_info") or ""
        match = TEL_RE.search(vcf)
        if match:
            return "+" + re.sub(r"\D", "", match.group(1))
        phone = payload.get("phone") or ""
        if phone:
            return str(phone)
    return ""


START_RE = re.compile(r"^/start(?:\s+(\S+))?$", re.IGNORECASE)


def _start_payload(text: str) -> Optional[str]:
    match = START_RE.match((text or "").strip())
    if not match:
        return None
    return match.group(1) or ""


def _extract_photo_token(payload: dict[str, Any]) -> str:
    photos = payload.get("photos")
    if isinstance(photos, dict):
        for value in photos.values():
            if isinstance(value, dict) and value.get("token"):
                return str(value["token"])
    if payload.get("token"):
        return str(payload["token"])
    return ""
