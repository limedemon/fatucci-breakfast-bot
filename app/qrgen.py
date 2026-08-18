"""Генерация QR-кодов для объектов."""
from __future__ import annotations

import io

import aiosqlite
import qrcode
from qrcode.constants import ERROR_CORRECT_H

from .channels.base import MAX, TG, get_channel

Row = aiosqlite.Row


def make_qr(url: str, box_size: int = 12) -> bytes:
    """PNG с QR-кодом. Высокий уровень коррекции — код читается даже с логотипом сверху."""
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=box_size, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def object_links(obj: Row) -> dict[str, str]:
    """Ссылки на бота с меткой объекта — по одной на канал."""
    links: dict[str, str] = {}
    for name in (TG, MAX):
        channel = get_channel(name)
        if channel is None or not getattr(channel, "username", ""):
            continue  # без username бота ссылку не собрать
        links[name] = channel.start_link(obj["code"])
    return links


def link_for(channel_name: str, code: str) -> str:
    channel = get_channel(channel_name)
    if channel is None:
        return ""
    return channel.start_link(code)
