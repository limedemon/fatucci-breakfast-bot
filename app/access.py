"""Заявки на права администратора: команда /admin request.

Человек пишет боту «/admin request» — в Telegram или в MAX. Заявка приходит
в личку бота всем администраторам, в каждом мессенджере — там, где у админа
есть доступ. Под заявкой три кнопки: принять, отклонить, забанить.

Решает тот, кто нажал первым. У остальных сообщение с заявкой обновляется:
видно, что уже решено и кем, — чтобы двое не выдали доступ одновременно.

Права выдаются в том мессенджере, откуда пришла заявка: админ-панель есть
и в Telegram, и в MAX.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import admins, repo
from .channels.base import TG, Btn, Channel, Event, Out, channel_title, get_channel
from .utils import esc, fmt_dt

log = logging.getLogger(__name__)
Row = Any

PENDING, ACCEPTED, REJECTED, BANNED = "pending", "accepted", "rejected", "banned"

VERDICTS = {
    ACCEPTED: "✅ Принято",
    REJECTED: "❌ Отклонено",
    BANNED: "⛔ Забанен",
}

#: действие кнопки → итоговый статус заявки
ACTIONS = {"ok": ACCEPTED, "no": REJECTED, "ban": BANNED}


def is_request_command(text: str) -> bool:
    """«/admin request» — с любым регистром и лишними пробелами."""
    words = (text or "").strip().lower().split()
    return len(words) == 2 and words[0].split("@")[0] == "/admin" and words[1] == "request"


def dm_chat(channel: str, user_id: int | str) -> str:
    """Адрес личного чата с человеком по его ID."""
    return f"u{user_id}" if channel != TG else str(user_id)


def _who(row: Row) -> str:
    name = esc(row["full_name"] or "без имени")
    if row["username"]:
        name += f" (@{esc(row['username'])})"
    return name


# ------------------------------------------------------------------ заявка
async def request_rights(ev: Event, ch: Channel) -> None:
    """Принять заявку от человека и разослать её администраторам."""
    # в Telegram у личного чата номер совпадает с номером человека; в группе
    # заявку не принимаем — там её увидели бы все участники
    if (str(ev.chat_id) != str(ev.user_id) if ch.name == TG
            else ev.raw.get("chat_type") == "chat"):
        await ch.send(ev.chat_id, Out(
            text="ℹ️ Запрос прав отправляется в личном чате с ботом."))
        return

    if await admins.is_admin(ev.user_id, ev.channel):
        await ch.send(ev.chat_id, Out(text="✅ У вас уже есть права администратора."))
        return

    user = await repo.upsert_user(ev.channel, ev.user_id, ev.chat_id, ev.username,
                                  ev.full_name)
    if user["is_blocked"]:
        await ch.send(ev.chat_id, Out(text=await repo.render_text("blocked")))
        return

    pending = await repo.pending_admin_request(ev.channel, str(ev.user_id))
    if pending is not None:
        await ch.send(ev.chat_id, Out(
            text="⏳ <b>Запрос уже отправлен</b>\n\n"
                 "Администраторы его увидели — ответ придёт сюда."))
        return

    request_id = await repo.create_admin_request(
        ev.channel, str(ev.user_id), str(ev.chat_id), ev.username or "", ev.full_name or "")
    request = await repo.get_admin_request(request_id)
    delivered = await _notify_admins(request)

    if not delivered:
        log.warning("Заявку на права #%s некому показать — админов нет", request_id)
    await ch.send(ev.chat_id, Out(
        text="📨 <b>Запрос отправлен</b>\n\n"
             "Администраторы решат, выдать ли вам доступ. Ответ придёт сюда."))


async def _notify_admins(request: Row) -> int:
    """Показать заявку каждому администратору в его мессенджере."""
    text = _request_text(request)
    kb = [[Btn(text="✅ Принять", data=f"ar:ok:{request['id']}", intent="positive"),
           Btn(text="❌ Отклонить", data=f"ar:no:{request['id']}")],
          [Btn(text="⛔ Забанить", data=f"ar:ban:{request['id']}", intent="negative")]]

    sent: list[dict[str, str]] = []
    for channel_name, admin_id in await admins.targets():
        channel = get_channel(channel_name)
        if channel is None:
            continue
        chat = dm_chat(channel_name, admin_id)
        message_id = await channel.send(chat, Out(text=text, kb=kb))
        if message_id:
            sent.append({"channel": channel_name, "chat_id": chat, "message_id": message_id})

    await repo.update_admin_request(request["id"], messages=json.dumps(sent))
    return len(sent)


def _request_text(request: Row, verdict: str = "") -> str:
    lines = [
        "🙋 <b>Запрос прав администратора</b>",
        "",
        f"👤 {_who(request)}",
        f"🆔 <code>{esc(request['ext_id'])}</code> · {channel_title(request['channel'])}",
        f"🕐 {fmt_dt(request['created_at'])}",
    ]
    if verdict:
        lines += ["", verdict]
    else:
        lines += ["", "Выдать доступ к управлению ботом?"]
    return "\n".join(lines)


# ----------------------------------------------------------------- решение
async def decide(ev: Event, ch: Channel, action: str, request_id: int) -> None:
    """Администратор нажал «Принять», «Отклонить» или «Забанить»."""
    if not await admins.is_admin(ev.user_id, ev.channel):
        await ch.answer_callback(ev.callback_id, "Решать заявки могут только администраторы")
        return

    status = ACTIONS.get(action)
    request = await repo.get_admin_request(request_id)
    if status is None or request is None:
        await ch.answer_callback(ev.callback_id, "Заявка не найдена")
        return
    if request["status"] != PENDING:
        await ch.answer_callback(
            ev.callback_id,
            f"Уже решено: {VERDICTS.get(request['status'], request['status'])}"
            + (f" — {request['decided_by']}" if request["decided_by"] else ""))
        return

    actor = f"@{ev.username}" if ev.username else (ev.full_name or f"id {ev.user_id}")
    if not await repo.claim_admin_request(request_id, status, actor):
        latest = await repo.get_admin_request(request_id)
        await ch.answer_callback(
            ev.callback_id,
            f"Уже решено: {VERDICTS.get(latest['status'], latest['status'])}"
            + (f" — {latest['decided_by']}" if latest and latest["decided_by"] else ""))
        return

    if status == ACCEPTED:
        await admins.add(int(request["ext_id"]), request["username"], request["full_name"],
                         added_by=f"заявка, решил {actor}", channel=request["channel"])
        await _tell_requester(request, (
            "✅ <b>Вам выданы права администратора</b>\n\n"
            + "Откройте /admin — там управление ботом."))
        requester = get_channel(request["channel"])
        if request["channel"] == TG and requester is not None:
            await requester.show_admin_button(
                dm_chat(TG, request["ext_id"]),
                "🛠 Внизу закреплены <b>Админ-панель</b> и <b>Поддержка</b>.")
    elif status == REJECTED:
        await _tell_requester(request, "❌ <b>Запрос отклонён</b>\n\n"
                                       "Администраторы не выдали доступ.")
    else:
        user = await repo.get_user(request["channel"], request["ext_id"])
        if user is not None:
            await repo.set_blocked(user["id"], True)
        await _tell_requester(request, await repo.render_text("blocked"))

    await ch.answer_callback(ev.callback_id, VERDICTS[status])
    await _refresh_copies(await repo.get_admin_request(request_id))


async def _tell_requester(request: Row, text: str) -> None:
    channel = get_channel(request["channel"])
    if channel is None:
        return
    await channel.send(request["chat_id"] or dm_chat(request["channel"], request["ext_id"]),
                       Out(text=text))


async def _refresh_copies(request: Row) -> None:
    """У всех админов заявка превращается в итог — без кнопок."""
    verdict = VERDICTS.get(request["status"], request["status"])
    if request["decided_by"]:
        verdict += f" — {esc(request['decided_by'])}"
    text = _request_text(request, f"<b>{verdict}</b>")
    for item in repo.json_loads(request["messages"], []):
        channel = get_channel(item.get("channel", ""))
        if channel is None:
            continue
        try:
            await channel.edit(item["chat_id"], item["message_id"], Out(text=text, kb=[]))
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось обновить копию заявки: %s", exc)
