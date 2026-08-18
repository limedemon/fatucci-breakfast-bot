"""Мелкие помощники: даты, деньги, телефоны, экранирование, коды объектов."""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional, Sequence, TypeVar

from .config import cfg

T = TypeVar("T")

WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


# --------------------------------------------------------------------- время
def now() -> datetime:
    """Текущее время в часовом поясе проекта (по умолчанию МСК)."""
    return datetime.now(cfg.tz)


def today() -> date:
    return now().date()


def parse_time(value: str, fallback: str = "20:00") -> time:
    raw = (value or fallback).strip()
    m = re.match(r"^(\d{1,2})[:.\s]?(\d{2})$", raw)
    if not m:
        m = re.match(r"^(\d{1,2})[:.\s]?(\d{2})$", fallback)
    hour, minute = int(m.group(1)), int(m.group(2))
    return time(hour=min(hour, 23), minute=min(minute, 59))


def parse_date(value: str) -> Optional[date]:
    raw = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if fmt == "%d.%m":
            parsed = parsed.replace(year=today().year)
        return parsed
    return None


def fmt_date(d: date | str, with_weekday: bool = True) -> str:
    """19 августа, вторник"""
    d = _as_date(d)
    base = f"{d.day} {MONTHS_GEN[d.month - 1]}"
    if with_weekday:
        return f"{base}, {WEEKDAYS_FULL[d.weekday()]}"
    return base


def fmt_date_btn(d: date | str) -> str:
    """Пн, 19.08 — короткая подпись для кнопки."""
    d = _as_date(d)
    return f"{WEEKDAYS_SHORT[d.weekday()]}, {d.strftime('%d.%m')}"


def fmt_date_iso(d: date | str) -> str:
    return _as_date(d).strftime("%Y-%m-%d")


def fmt_dt(value: str | datetime) -> str:
    """Читаемая дата-время из строки БД (UTC) в местном поясе."""
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ""
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                return raw
        dt = dt.replace(tzinfo=timezone_utc()) if dt.tzinfo is None else dt
    else:
        dt = value
    return dt.astimezone(cfg.tz).strftime("%d.%m.%Y %H:%M")


def timezone_utc():
    from datetime import timezone

    return timezone.utc


def _as_date(d: date | str) -> date:
    if isinstance(d, date):
        return d
    parsed = parse_date(d)
    if parsed is None:
        raise ValueError(f"Не удалось разобрать дату: {d!r}")
    return parsed


# --------------------------------------------------------------------- деньги
def fmt_money(kop: int) -> str:
    rub, rem = divmod(int(kop), 100)
    body = f"{rub:,}".replace(",", " ")
    if rem:
        return f"{body},{rem:02d} ₽"
    return f"{body} ₽"


def parse_money(value: str) -> Optional[int]:
    """'900', '900.50', '1 250,50' -> копейки."""
    raw = (value or "").replace(" ", "").replace(" ", "").replace("₽", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", raw):
        return None
    if "." in raw:
        whole, frac = raw.split(".")
        frac = (frac + "00")[:2]
        return int(whole) * 100 + int(frac)
    return int(raw) * 100


# ------------------------------------------------------------------- телефоны
def norm_phone(value: str) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    if 10 <= len(digits) <= 15:
        return "+" + digits
    return None


def fmt_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return value or ""


# --------------------------------------------------------------------- строки
def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    """Убрать теги (для каналов/мест, где HTML не поддерживается)."""
    text = (value or "").replace("<br>", "\n").replace("<br/>", "\n")
    return html.unescape(TAG_RE.sub("", text))


def slug_code(value: str, fallback: str = "obj") -> str:
    """Код объекта для deep-link: только [A-Za-z0-9_-], MAX другого не пропускает."""
    src = unicodedata.normalize("NFKD", (value or "").strip().lower())
    out: list[str] = []
    for ch in src:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in " -_./":
            out.append("_")
    code = re.sub(r"_+", "_", "".join(out)).strip("_")
    code = code[:40]
    return code or fallback


def safe_format(template: str, **kwargs: Any) -> str:
    """format(), который не падает на неизвестном плейсхолдере в тексте из админки."""

    class _Safe(dict):
        def __missing__(self, key: str) -> str:  # noqa: D105
            return "{" + key + "}"

    try:
        return template.format_map(_Safe(**kwargs))
    except (ValueError, IndexError):
        return template


def chunk(items: Sequence[T], size: int) -> list[list[T]]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def parse_days(value: str) -> set[int]:
    """'1,2,3' -> {1,2,3} (1=Пн ... 7=Вс)."""
    days: set[int] = set()
    for part in (value or "").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 7:
            days.add(int(part))
    return days or {1, 2, 3, 4, 5, 6, 7}


def fmt_days(value: str) -> str:
    days = sorted(parse_days(value))
    if len(days) == 7:
        return "ежедневно"
    return ", ".join(WEEKDAYS_SHORT[d - 1] for d in days)


def daterange(start: date, days: int) -> Iterable[date]:
    for i in range(days):
        yield start + timedelta(days=i)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
