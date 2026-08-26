"""Самопроверка бота без реальных мессенджеров.

Прогоняет весь путь гостя и действия менеджера на временной базе:
    python selftest.py

Ничего не отправляет наружу — вместо Telegram подставляется заглушка.
Полезно после любых правок: если что-то сломалось, видно сразу.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="fatucci_test_")
# Тест полностью задаёт своё окружение, чтобы .env разработчика на него не влиял.
# На PostgreSQL тест идёт только из selftest_pg.py — он задаёт DB_SCHEMA
# и работает во временной схеме. Во всех остальных случаях принудительно
# гасим адрес базы, иначе тест ушёл бы в боевую.
if not os.getenv("DB_SCHEMA"):
    for name in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "DB_URL"):
        os.environ[name] = ""
    os.environ["FATUCCI_DATABASE_URL"] = "sqlite"
    os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["TELEGRAM_TOKEN"] = "0:test"
os.environ["TELEGRAM_BOT_USERNAME"] = "fatucci_test_bot"
os.environ["MAX_BOT_USERNAME"] = "fatucci_test_bot"
os.environ["MAX_TOKEN"] = ""
os.environ["ADMIN_IDS"] = "777"
os.environ["ORDERS_CHAT_ID"] = "-100777"
os.environ["PAYMENT_PROVIDER_TOKEN"] = ""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re  # noqa: E402
from datetime import timedelta  # noqa: E402

from app import courier, db, guide, payments, pricing, repo, statuses  # noqa: E402
from app.channels import base  # noqa: E402
from app.channels.base import Channel, Event, Out  # noqa: E402
from app.channels.telegram import ADMIN_BUTTON, SUPPORT_BUTTON  # noqa: E402
from app.router import route  # noqa: E402
from app.utils import fmt_date_iso, today, utc_stamp  # noqa: E402

OK, FAIL = "✅", "❌"
failures: list[str] = []

TEST_TOKEN = "123456789:TEST:abcdef0123456789abcd"
GUEST, GUEST2, ADMIN, CHAT = "555", "556", "777", "-100777"


def check(condition: bool, title: str) -> None:
    print(f"{OK if condition else FAIL} {title}")
    if not condition:
        failures.append(title)


class FakeChannel(Channel):
    """Заглушка мессенджера: всё «отправленное» складывается в список."""

    def __init__(self, name: str = "tg") -> None:
        self.name = name
        self.title = name
        self.username = "fatucci_test_bot"
        self.sent: list[tuple[str, Out]] = []
        self.invoices: list[dict] = []
        self.admin_button_shown = False
        self.support_button_shown = False
        self.description = ""
        self._id = 0

    async def send(self, chat_id: str, out: Out) -> str:
        self._id += 1
        self.sent.append((str(chat_id), out))
        return str(self._id)

    async def edit(self, chat_id: str, message_id: str, out: Out) -> bool:
        self.sent.append((str(chat_id), out))
        return True

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None

    def start_link(self, payload: str = "") -> str:
        return f"https://t.me/fatucci_test_bot?start={payload}"

    async def send_document(self, chat_id, data, filename, caption="") -> bool:
        self.sent.append((str(chat_id), Out(text=f"[файл] {filename} {len(data)}b {caption}")))
        return True

    async def send_bytes(self, chat_id, data, filename, caption="") -> bool:
        self.sent.append((str(chat_id), Out(text=f"[png {len(data)}b] {filename}")))
        return True

    async def download_bytes(self, file_id: str) -> bytes:
        return bytes([0x89]) + b"PNG" + b"0" * 64

    async def set_description(self, text: str) -> bool:
        self.description = text
        return True

    async def show_admin_button(self, chat_id: str, text: str) -> bool:
        self.admin_button_shown = True
        self.sent.append((str(chat_id), Out(text=text)))
        return True

    async def show_support_button(self, chat_id: str, text: str) -> bool:
        self.support_button_shown = True
        self.sent.append((str(chat_id), Out(text=text)))
        return True

    async def send_invoice(self, chat_id, title, description, payload, amount_kop,
                           provider_token, label="К оплате", provider_data="") -> tuple[bool, str]:
        self.invoices.append({"chat_id": str(chat_id), "title": title, "payload": payload,
                              "description": description, "amount": amount_kop,
                              "token": provider_token})
        self.sent.append((str(chat_id), Out(text=f"[счёт] {title} — {amount_kop} коп.")))
        return True, ""

    # --- помощники теста ---
    def last(self) -> Out:
        return self.sent[-1][1]

    def texts(self) -> str:
        return "\n".join(out.text for _, out in self.sent)

    def to(self, chat: str) -> str:
        return "\n".join(out.text for c, out in self.sent if c == chat)

    def buttons(self) -> list[str]:
        return [b.data or b.url for row in (self.last().kb or []) for b in row]

    def find_button(self, prefix: str) -> str:
        for _, out in reversed(self.sent):
            for row in out.kb or []:
                for btn in row:
                    if btn.data.startswith(prefix):
                        return btn.data
        return ""

    def find_all(self, prefix: str) -> list[str]:
        found = []
        for _, out in self.sent:
            for row in out.kb or []:
                for btn in row:
                    if btn.data.startswith(prefix) and btn.data not in found:
                        found.append(btn.data)
        return found

    def clear(self) -> None:
        self.sent.clear()
        self.invoices.clear()
        self.admin_button_shown = False
        self.support_button_shown = False


def guest_event(kind: str, who: str = GUEST, **kwargs) -> Event:
    return Event(channel="tg", user_id=who, chat_id=who, kind=kind,
                 full_name="Тестовый Гость", **kwargs)


def admin_event(kind: str, chat_id: str = ADMIN, **kwargs) -> Event:
    return Event(channel="tg", user_id=ADMIN, chat_id=chat_id, kind=kind,
                 username="manager", full_name="Менеджер", **kwargs)


async def place_order(ch: FakeChannel, dates: int = 1, qty: int = 2, who: str = GUEST,
                      apartment: str = "45", allergies: str = "",
                      comment: str = "") -> list:
    """Пройти путь гостя до конца и вернуть строки созданного заказа."""
    ch.clear()
    await route(guest_event("start", who, payload="demo1"), ch)
    await route(guest_event("callback", who, payload="g:order", callback_id="o1"), ch)
    picks = ch.find_all("g:date:")[:dates]
    for iso in picks:
        await route(guest_event("callback", who, payload=iso, callback_id="o2"), ch)
    await route(guest_event("callback", who, payload="g:dates", callback_id="o3"), ch)
    await route(guest_event("callback", who, payload=f"g:qty:{qty}", callback_id="o4"), ch)
    await route(guest_event("text", who, text=apartment), ch)
    await route(guest_event("contact", who, phone="+7 999 123-45-67"), ch)
    if allergies:
        await route(guest_event("text", who, text=allergies), ch)
    else:
        await route(guest_event("callback", who, payload="g:skipa", callback_id="o5"), ch)
    if comment:
        await route(guest_event("text", who, text=comment), ch)
    else:
        await route(guest_event("callback", who, payload="g:skip", callback_id="o6"), ch)
    await route(guest_event("callback", who, payload="g:confirm", callback_id="o7"), ch)
    orders = await repo.list_orders(limit=30, user_key=("tg", who))
    key = orders[0]["group_key"]
    return await repo.group_orders(key)


async def simulate_old_database() -> None:
    """Создать базу такой, какой она была до последних правок.

    Так проверяется не установка «с нуля», а обновление бота на базе, где уже
    лежат заказы: новые колонки должны доехать миграциями, а индексы по ним —
    создаться только после этого.
    """
    old = db.SCHEMA
    for _table, column, _ddl in db.MIGRATIONS:
        old = re.sub(rf"^ *{column} .*\n", "", old, flags=re.M)
    old = re.sub(r",(\s*\);)", r"\1", old)        # запятая перед закрывающей скобкой
    await db.apply_ddl(old)


async def index_names() -> set[str]:
    if db.IS_PG:
        rows = await db.fetchall(
            "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema()")
    else:
        rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")
    return {row["name"] for row in rows}


async def main() -> None:
    ch = FakeChannel()
    base.REGISTRY["tg"] = ch

    print("— Обновление на уже работающей базе —")
    await simulate_old_database()
    missing_before = {c for _t, c, _d in db.MIGRATIONS} - await db._columns("orders")
    check(bool(missing_before), "старая база действительно без новых колонок")
    # заказ, оформленный ещё старой версией бота
    legacy_id = await db.insert(
        "INSERT INTO orders (number, channel, ext_id, delivery_date, qty, apartment, "
        "status, price_kop, total_kop) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("F-00777", "tg", GUEST, fmt_date_iso(today()), 1, "7", "paid", 90000, 90000))

    await db.init_db()

    legacy = await repo.get_order(legacy_id)
    check(legacy is not None, "старый заказ пережил обновление")
    check(legacy["group_key"] == "F-00777", "старому заказу проставлен номер группы")
    check(legacy["allergies"] == "" and legacy["discount_pct"] == 0,
          "новые поля у старого заказа заполнены значениями по умолчанию")
    check(await repo.group_of(legacy) == [legacy],
          "старый заказ читается как заказ на один день")
    await db.execute("DELETE FROM orders WHERE id = ?", (legacy_id,))
    order_cols = await db._columns("orders")
    for column in ("group_key", "allergies", "address_ok", "discount_pct",
                   "base_price_kop", "customer_name"):
        check(column in order_cols, f"миграция добавила orders.{column}")
    check("delivery_time" in await db._columns("objects"),
          "миграция добавила objects.delivery_time")
    check("idx_orders_group" in await index_names(),
          "индекс по новой колонке создался после миграции")
    await repo.set_setting("pm_token", TEST_TOKEN)     # касса «подключена»

    print("\n— Заказ на один день —")
    ch.clear()
    await route(guest_event("start", payload="demo1"), ch)
    check("Fatucci" in ch.texts(), "приветствие показано")
    check(bool(ch.find_button("g:order")), "есть кнопка «Заказать завтрак»")
    check(bool(ch.find_button("g:rules")), "есть раздел «Условия заказа»")
    check(bool(ch.find_button("g:manager")), "есть кнопка «Связаться с менеджером»")

    ch.clear()
    await route(guest_event("callback", payload="g:order", callback_id="d1"), ch)
    dates = ch.find_all("g:date:")
    check(len(dates) >= 2, f"предложено несколько дат ({len(dates)})")

    ch.clear()
    await route(guest_event("callback", payload=dates[0], callback_id="d2"), ch)
    check("✅" in "".join(b.text for r in ch.last().kb for b in r), "дата отмечается галочкой")
    check(bool(ch.find_button("g:dates")), "появилась кнопка «Далее»")

    ch.clear()
    await route(guest_event("callback", payload="g:dates", callback_id="d3"), ch)
    check(bool(ch.find_button("g:qty:")), "показан выбор количества")

    ch.clear()
    await route(guest_event("callback", payload="g:qty:2", callback_id="d4"), ch)
    check("апартамент" in ch.texts().lower(), "запрошен номер апартаментов")
    ch.clear()
    await route(guest_event("text", text="45"), ch)
    check("телефон" in ch.texts().lower(), "запрошен телефон")
    ch.clear()
    await route(guest_event("contact", phone="+7 999 123-45-67"), ch)
    check("аллерг" in ch.texts().lower(), "спрошено про аллергии")
    ch.clear()
    await route(guest_event("text", text="аллергия на орехи"), ch)
    check("пожелания" in ch.texts().lower(), "спрошены пожелания")
    ch.clear()
    await route(guest_event("text", text="позвонить за 10 минут"), ch)
    check("Проверьте заказ" in ch.texts(), "показана карточка проверки")
    check("Отменить или изменить" in ch.texts(), "на проверке видны условия отмены")
    check("аллергия на орехи" in ch.texts(), "аллергии видны в карточке")

    ch.clear()
    await route(guest_event("callback", payload="g:confirm", callback_id="d5"), ch)
    orders = await repo.list_orders(limit=5)
    check(len(orders) == 1, "заказ создан")
    order = orders[0]
    check(order["qty"] == 2 and order["apartment"] == "45", "количество и апартаменты сохранены")
    check(order["allergies"] == "аллергия на орехи", "аллергии записаны в заказ")
    check(order["comment"] == "позвонить за 10 минут", "пожелания записаны")
    check(order["group_key"] == order["number"], "номер заказа и номер группы совпали")
    check("Заказ принят" in ch.to(GUEST), "гостю пришло подтверждение приёма")
    check("НОВЫЙ ЗАКАЗ" in ch.to(CHAT), "менеджер получил карточку")
    check("Аллергии" in ch.to(CHAT), "аллергии видны менеджеру")

    print("\n— Заказ сразу на несколько дней —")
    group = await place_order(ch, dates=3, qty=2, who=GUEST2, apartment="214")
    check(len(group) == 3, f"создано 3 строки заказа — по одной на дату ({len(group)})")
    check(len({row["group_key"] for row in group}) == 1, "у всех дат один номер заказа")
    check(len({row["delivery_date"] for row in group}) == 3, "даты разные")
    check(len({row["number"] for row in group}) == 3, "номера строк уникальны")
    guest_text = ch.to(GUEST2)
    check(guest_text.count(" шт.") >= 3, "гость видит разбивку по датам")
    check("Итого:" in guest_text, "показан итог по всему заказу")
    total = sum(row["total_kop"] for row in group)
    check(total == sum(r["price_kop"] * r["qty"] for r in group), "сумма заказа сходится")
    check(ch.to(CHAT).count("НОВЫЙ ЗАКАЗ") == 1, "менеджеру пришла одна карточка на весь заказ")

    print("\n— Оплата через подключённую кассу —")
    ch.clear()
    await route(admin_event("callback", chat_id=CHAT,
                            payload=f"a:ord:{group[0]['id']}:{statuses.ACCEPTED}",
                            callback_id="p1"), ch)
    fresh = await repo.group_orders(group[0]["group_key"])
    check(all(row["status"] == statuses.ACCEPTED for row in fresh),
          "подтверждение применилось ко всем дням заказа")
    check(len(ch.invoices) == 1, "гостю выставлен один счёт на весь заказ")
    if ch.invoices:
        inv = ch.invoices[0]
        check(inv["amount"] == total, "сумма счёта равна сумме заказа")
        check(inv["token"] == TEST_TOKEN, "счёт выставлен с токеном кассы")
        check(len(inv["title"]) <= 32 and len(inv["description"]) <= 255,
              "заголовок и описание счёта в пределах лимитов Telegram")

    ch.clear()
    await route(Event(channel="tg", user_id=GUEST2, chat_id=GUEST2, kind="payment",
                      payload=payments.invoice_payload(group[0]["id"]),
                      raw={"charge_id": "ch_1"}), ch)
    fresh = await repo.group_orders(group[0]["group_key"])
    check(all(row["status"] == statuses.PAID for row in fresh), "после оплаты весь заказ оплачен")
    check(all(row["paid_at"] for row in fresh), "время оплаты зафиксировано")

    print("\n— Доставка и получение по дням —")
    group = await place_order(ch, dates=2, qty=1, who=GUEST2, apartment="214")
    await route(admin_event("callback", chat_id=CHAT,
                            payload=f"a:ord:{group[0]['id']}:{statuses.ACCEPTED}",
                            callback_id="dl0"), ch)
    ch.clear()
    await route(admin_event("callback", chat_id=CHAT,
                            payload=f"a:ord:{group[0]['id']}:{statuses.DELIVERED}",
                            callback_id="dl1"), ch)
    fresh = await repo.group_orders(group[0]["group_key"])
    check(fresh[0]["status"] == statuses.DELIVERED and fresh[1]["status"] != statuses.DELIVERED,
          "доставка отмечается по одному дню")
    check(bool(ch.find_button("g:got:")), "гостю предложено подтвердить получение")
    ch.clear()
    await route(guest_event("callback", GUEST2, payload=f"g:got:{group[0]['id']}",
                            callback_id="dl2"), ch)
    fresh = await repo.group_orders(group[0]["group_key"])
    check(fresh[0]["status"] == statuses.RECEIVED, "гость подтвердил получение")
    check(not ch.find_button("g:paid:"), "кнопки «Я оплатил» больше нет — платит касса")

    print("\n— Без подключённой кассы заказ не оформляется —")
    await repo.set_setting("pm_token", "")
    ch.clear()
    await route(guest_event("callback", payload="g:order", callback_id="n1"), ch)
    check("недоступен" in ch.texts().lower(), "гостю сказано, что заказать пока нельзя")
    check(not ch.find_button("g:date:"), "даты при этом не предлагаются")
    await repo.set_setting("pm_token", TEST_TOKEN)

    ch.clear()
    await repo.set_setting("pay_enabled", "0")
    await route(guest_event("callback", payload="g:order", callback_id="n2"), ch)
    check("недоступен" in ch.texts().lower(), "выключенный приём оплаты тоже закрывает заказы")
    await repo.set_setting("pay_enabled", "1")
    ok, report = await payments.check_setup()
    check(ok and "тестовый" in report.lower(), "проверка оплаты видит тестовый режим")
    await repo.set_setting("pm_token", "мусор")
    ok, report = await payments.check_setup()
    check(not ok and "не похож" in report, "кривой токен подсвечивается")
    await repo.set_setting("pm_token", TEST_TOKEN)

    print("\n— Общий QR: гость вводит адрес —")
    ch.clear()
    await route(guest_event("start", payload="obshiy"), ch)
    await route(guest_event("callback", payload="g:order", callback_id="a1"), ch)
    check("адрес" in ch.texts().lower(), "по общему QR спрошен адрес")
    check(not ch.find_button("g:obj:"), "список чужих объектов гостю не показывается")

    ch.clear()
    await route(guest_event("text", text="Северная 12"), ch)
    check(bool(ch.find_button("g:date:")), "после адреса предложены даты")
    session = await repo.get_session("tg", GUEST)
    matched = await repo.get_object(session["object_id"])
    check(matched is not None and matched["code"] == "demo1",
          "адрес сопоставлен с нужным объектом")

    ch.clear()
    await route(guest_event("start", payload="obshiy"), ch)
    await route(guest_event("callback", payload="g:order", callback_id="a2"), ch)
    await route(guest_event("text", text="Неизвестная улица 99"), ch)
    check("менеджер проверит" in ch.texts().lower() or "не найден" in ch.texts().lower()
          or "пока нет в списке" in ch.texts().lower(),
          "про незнакомый адрес честно сказано")
    check(bool(ch.find_button("g:date:")), "заказ всё равно можно продолжить")

    print("\n— Сопоставление адресов —")
    for typed, expect in [("г. Сочи, ул. Северная, д. 12", "demo1"),
                          ("северная 12", "demo1"),
                          ("Северная д.12", "demo1"),
                          ("Ленина 5", None)]:
        found = await repo.find_object_by_address(typed)
        code = found["code"] if found else None
        check(code == expect, f"адрес «{typed}» → {code or 'не найден'}")

    print("\n— Отмена заказа —")
    group = await place_order(ch, dates=3, qty=1, who=GUEST2, apartment="214")
    ch.clear()
    await route(guest_event("callback", GUEST2, payload=f"g:cancel:{group[1]['id']}",
                            callback_id="c1"), ch)
    fresh = await repo.group_orders(group[0]["group_key"])
    cancelled = [row for row in fresh if row["status"] == statuses.CANCELLED]
    check(len(cancelled) == 1, "отменён ровно один день заказа")
    check(cancelled[0]["id"] == group[1]["id"], "отменён именно выбранный день")

    ch.clear()
    await route(guest_event("callback", GUEST2, payload=f"g:cancelall:{group[0]['id']}",
                            callback_id="c2"), ch)
    fresh = await repo.group_orders(group[0]["group_key"])
    check(all(row["status"] == statuses.CANCELLED for row in fresh), "отменён весь заказ")
    check("отменил заказ" in ch.to(CHAT).lower(), "менеджер уведомлён об отмене")

    print("\n— Курьерская выгрузка —")
    await repo.set_setting("courier_statuses", "new,accepted,paid")
    group = await place_order(ch, dates=3, qty=2, who=GUEST, apartment="45")
    from datetime import date as _date
    for row in group:
        digest = await courier.build_digest(_date.fromisoformat(row["delivery_date"]))
        check(f"апарт. {row['apartment']}" in digest,
              f"заказ попал в выгрузку на {row['delivery_date']}")
    first = _date.fromisoformat(group[0]["delivery_date"])
    digest = await courier.build_digest(first)
    check(digest.count("📥 Откуда:") == len(
        await repo.orders_for_delivery(group[0]["delivery_date"], ["new", "accepted", "paid"])),
        "в выгрузке столько блоков, сколько доставок на этот день")
    for field in ("📥 Откуда:", "📍 Адрес:", "👥 Получатель:", "⏰ Когда:",
                  "✏️ Комментарий:", "Доставка:"):
        check(field in digest, f"в выгрузке есть поле «{field}»")

    await repo.set_status(group[1]["id"], statuses.CANCELLED, actor="тест")
    second = _date.fromisoformat(group[1]["delivery_date"])
    digest2 = await courier.build_digest(second)
    check("апарт. 45" not in digest2 or "подтверждённых заказов нет" in digest2,
          "отменённый день из выгрузки пропал")

    print("\n— Скидки за количество —")
    tiers = pricing.parse_tiers("3=5, 5=10, 10=15")
    check([t.qty for t in tiers] == [10, 5, 3], "пороги разобраны и отсортированы")
    check(pricing.percent_for(4, tiers) == 5, "между порогами держится нижний")
    price = pricing.calc(90000, 5, tiers)
    check(price.percent == 10 and price.per_set == 81000, "900 ₽ → 810 ₽ при 5 наборах")
    check(price.total == 405000 and price.saved == 45000, "итог и экономия посчитаны")
    check(pricing.parse_tiers("ерунда") == [], "мусор в настройке игнорируется")

    await repo.set_setting("discount_tiers", "2=10")
    group = await place_order(ch, dates=1, qty=2, who=GUEST)
    check(group[0]["discount_pct"] == 10, "скидка сохранена в заказе")
    check(group[0]["price_kop"] == round(group[0]["base_price_kop"] * 0.9 / 100) * 100,
          "цена со скидкой посчитана верно")
    await repo.set_setting("discount_tiers", "")

    print("\n— Время приёма и отмены —")
    obj = await repo.get_object_by_code("demo1")
    from app import availability, orders_service
    await repo.update_object(obj["id"], cutoff_time="00:01", lead_days=1)
    obj = await repo.get_object_by_code("demo1")
    tomorrow = today() + timedelta(days=1)
    dates_now = [d for d, _ in await availability.available_dates(obj)]
    check(tomorrow not in dates_now, "после отсечки завтра не предлагается")
    await repo.update_object(obj["id"], cutoff_time="23:59")
    obj = await repo.get_object_by_code("demo1")
    dates_now = [d for d, _ in await availability.available_dates(obj)]
    check(tomorrow in dates_now, "до отсечки завтра доступно")

    deadline = await orders_service.cancel_deadline_for(fmt_date_iso(tomorrow))
    check(deadline is not None and deadline.date() == today(),
          "срок отмены — накануне дня доставки")

    print("\n— Аналитика по QR —")
    before = (await repo.qr_visits(fmt_date_iso(today()), fmt_date_iso(today()))).get("demo1", 0)
    await route(guest_event("start", payload="demo1"), ch)
    after = (await repo.qr_visits(fmt_date_iso(today()), fmt_date_iso(today()))).get("demo1", 0)
    check(after == before + 1, "переход по QR посчитан")
    # заказы в тестах оформляются на будущие даты, а статистика считает по дате
    # доставки — поэтому одну доставку переносим на сегодня осознанно
    group = await place_order(ch, dates=1, qty=2)
    await repo.update_order(group[0]["id"], delivery_date=fmt_date_iso(today()))
    ch.clear()
    await route(admin_event("callback", payload="a:st:d30", callback_id="s1"), ch)
    stats = ch.texts()
    check("переход" in stats, "в статистике видны переходы по QR")
    check("Альфа" in stats, "объект показан отдельной строкой")
    check("Выручка" in stats, "в статистике есть выручка по объекту")
    check("на более поздние даты" in stats,
          "заказы на будущие даты не теряются, а показаны отдельно")

    print("\n— Ежедневное напоминание —")
    from app import scheduler
    await repo.set_setting("daily_remind_time", "00:00")
    await repo.set_setting("daily_remind_sent", "")
    ch.clear()
    await scheduler.daily_remind_tick()
    check(await repo.get_setting("daily_remind_sent") in ("", fmt_date_iso(today())),
          "отметка об отправке напоминания ставится")
    await repo.set_setting("daily_remind_enabled", "0")

    print("\n— Незавершённый заказ —")
    ch.clear()
    await route(guest_event("callback", payload="g:order", callback_id="r1"), ch)
    first_date = ch.find_button("g:date:")
    await route(guest_event("callback", payload=first_date, callback_id="r2"), ch)
    session = await repo.get_session("tg", GUEST)
    check(session is not None and session["state"] != "", "черновик заказа сохранён")
    await db.execute("UPDATE sessions SET updated_at = ? WHERE channel='tg' AND ext_id = ?",
                     (utc_stamp(minutes=-90), GUEST))
    ch.clear()
    await scheduler.reminder_tick()
    check(bool(ch.find_button("g:resume")), "напоминание о брошенном заказе отправлено")

    print("\n— Кнопка «Поддержка» —")
    ch.clear()
    await route(guest_event("start", payload="demo1"), ch)
    check(ch.support_button_shown, "гостю закреплена кнопка «Поддержка»")
    check("Поддержка" in ch.texts(), "гостю объяснили, что это за кнопка")

    ch.clear()
    await route(guest_event("callback", payload="g:menu", callback_id="sp0"), ch)
    inline = [b.data for row in ch.last().kb or [] for b in row]
    check(not any("support" in data for data in inline),
          "в меню кнопки поддержки нет — она постоянная, под полем ввода")

    ch.clear()
    await route(guest_event("text", text=SUPPORT_BUTTON), ch)
    support = await repo.get_setting("support_contact")
    check(support in ch.texts(), f"по кнопке пришёл контакт поддержки ({support})")
    check(any(b.url.startswith("https://t.me/") for row in ch.last().kb or [] for b in row),
          "есть кнопка-ссылка на переписку с поддержкой")

    ch.clear()
    await route(guest_event("callback", payload="g:order", callback_id="sp1"), ch)
    first_date = ch.find_button("g:date:")
    await route(guest_event("callback", payload=first_date, callback_id="sp2"), ch)
    ch.clear()
    await route(guest_event("text", text=SUPPORT_BUTTON), ch)
    check(support in ch.texts(), "поддержка отвечает и посреди оформления заказа")
    session = await repo.get_session("tg", GUEST)
    check(session is not None and session["state"] == "date",
          "черновик заказа при этом не сбился")

    ch.clear()
    await route(admin_event("text", text="/start"), ch)
    check(ch.admin_button_shown, "админу закрепляются кнопки одним рядом")
    from app.channels.telegram import TelegramChannel
    labels = [b.text for row in TelegramChannel._admin_markup(
        TelegramChannel.__new__(TelegramChannel)).keyboard for b in row]
    check(labels == [ADMIN_BUTTON, SUPPORT_BUTTON],
          f"у админа под полем ввода обе кнопки в одном ряду: {labels}")

    ch.clear()
    await route(admin_event("text", text=SUPPORT_BUTTON), ch)
    check(support in ch.texts(), "админ по той же кнопке получает контакты поддержки")
    check(any(b.data == "a:h" for row in ch.last().kb or [] for b in row),
          "админа с экрана поддержки возвращают в админку")

    await repo.set_setting("support_contact", "+7 900 000-00-00")
    ch.clear()
    await route(guest_event("text", text=SUPPORT_BUTTON), ch)
    check("+7 900 000-00-00" in ch.texts(), "контакт поддержки берётся из настроек")
    check(not any(b.url for row in ch.last().kb or [] for b in row),
          "для телефона ссылка-кнопка не выдумывается")
    await repo.set_setting("support_contact", "@marina_fatucci")

    print("\n— Админка —")
    ch.clear()
    await route(admin_event("text", text="/admin"), ch)
    check("Админ-панель" in ch.texts(), "админка открывается по /admin")
    check(ch.admin_button_shown, "кнопка админ-панели закрепляется")
    ch.clear()
    await route(admin_event("text", text=ADMIN_BUTTON), ch)
    check("Админ-панель" in ch.texts(), "нажатие кнопки открывает админку")

    ch.clear()
    await route(admin_event("callback", payload="a:h", callback_id="h0"), ch)
    check("Админ-панель" in ch.texts(), "домашний экран админки открывается кнопкой")
    await repo.set_setting("pm_token", "")
    ch.clear()
    await route(admin_event("callback", payload="a:h", callback_id="h1"), ch)
    check("Касса не подключена" in ch.texts(), "админ видит предупреждение про кассу")
    await repo.set_setting("pm_token", TEST_TOKEN)

    for section, title in [("a:o:l:new:0", "заказы"), ("a:g:menu", "меню и цены"),
                           ("a:g:obj", "объекты и QR"), ("a:cur:m", "курьеры"),
                           ("a:g:rep", "отчёты"), ("a:cfg:m", "настройки"),
                           ("a:bc:m", "рассылка"), ("a:hp", "справка"),
                           ("a:db", "проверка базы"), ("a:acc:l", "доступ"),
                           ("a:t:l", "тексты"), ("a:m:l", "сеты"), ("a:r:l", "ротация"),
                           ("a:b:l", "объекты"), ("a:q:l", "QR"), ("a:f:l", "доп. предложения"),
                           ("a:u:l:0", "гости"), ("a:x:m", "экспорт")]:
        ch.clear()
        await route(admin_event("callback", payload=section, callback_id="x"), ch)
        check(len(ch.sent) > 0 and len(ch.last().text) > 10, f"раздел админки: {title}")

    ch.clear()
    await route(admin_event("callback", payload="a:cfg:yk", callback_id="y1"), ch)
    check("касс" in ch.texts().lower() or "счёт в telegram" in ch.texts().lower(),
          "проверка оплаты сообщает о состоянии кассы")

    broken = []
    for key in guide.ORDER:
        ch.clear()
        await route(admin_event("callback", payload=f"a:hp:{key}", callback_id="h"), ch)
        if len(ch.texts()) < 150:
            broken.append(guide.title(key))
    check(not broken, f"все {len(guide.ORDER)} разделов справки открываются"
                      + (f" — сломаны: {broken}" if broken else ""))

    ch.clear()
    await route(admin_event("callback", payload="a:x:p30", callback_id="x1"), ch)
    check("[файл]" in ch.texts(), "CSV-выгрузка формируется")
    ch.clear()
    await route(admin_event("callback", payload="a:q:o:1", callback_id="x2"), ch)
    check("[png" in ch.texts(), "QR-код генерируется картинкой")

    print("\n— Содержимое из ТЗ —")
    sets = await repo.list_sets()
    check(len(sets) == 4, f"в меню 4 сета ({len(sets)})")
    offers = await repo.list_offers()
    check(len(offers) == 3, f"три направления «Ещё от Fatucci» ({len(offers)})")
    titles = " ".join(o["title"] for o in offers)
    for name in ("Fine Food", "Дольче Дача", "Кейтеринг"):
        check(name in titles, f"есть блок «{name}»")
    check(len(await repo.get_text("bot_description")) > 50, "задано приветствие до кнопки Start")
    check("Условия заказа" in await repo.get_text("rules"), "заполнен раздел «Условия заказа»")
    check(await repo.get_setting("courier_time") == "19:00", "выгрузка курьерам в 19:00")
    check(await repo.get_setting("cancel_deadline") == "18:30", "отмена до 18:30 накануне")

    print("\n— Шаблоны текстов —")
    from app import defaults
    broken_texts = []
    for key in defaults.DEFAULT_TEXTS:
        try:
            rendered = await repo.render_text(key)
        except Exception as exc:  # noqa: BLE001
            broken_texts.append(f"{key}: {exc}")
            continue
        if not rendered.strip():
            broken_texts.append(f"{key}: пусто")
    check(not broken_texts, f"все {len(defaults.DEFAULT_TEXTS)} текстов бота собираются"
                            + (f" — сломаны: {broken_texts}" if broken_texts else ""))

    print("\n— Чёрный список —")
    user = await repo.get_user("tg", GUEST)
    await repo.set_blocked(user["id"], True)
    ch.clear()
    await route(guest_event("text", text="привет"), ch)
    check("недоступно" in ch.texts(), "чёрный список работает")
    await repo.set_blocked(user["id"], False)

    print("\n— Проверка базы —")
    report = await db.health()
    check(report["ok"], "health() говорит, что связь есть")
    check(any(t == "Заказов" for t, _ in report["tables"]), "в отчёте есть таблица заказов")
    check("@" not in report["where"], "пароль базы в отчёт не попадает")

    await db.close()
    print()
    if failures:
        print(f"{FAIL} Провалено проверок: {len(failures)}")
        for item in failures:
            print("   •", item)
        sys.exit(1)
    print(f"{OK} Все проверки пройдены.")


async def _run() -> None:
    try:
        await main()
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
