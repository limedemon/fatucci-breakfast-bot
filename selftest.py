"""Самопроверка бота без реальных мессенджеров.

Прогоняет весь путь гостя и действия менеджера на временной базе:
    python selftest.py

Ничего не отправляет наружу — вместо Telegram/MAX подставляется заглушка.
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
# гасим DATABASE_URL, иначе .env увёл бы тест в боевую базу.
if not os.getenv("DB_SCHEMA"):
    for name in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "DB_URL"):
        os.environ[name] = ""
    os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["TELEGRAM_TOKEN"] = "0:test"
os.environ["TELEGRAM_BOT_USERNAME"] = "fatucci_test_bot"
os.environ["MAX_BOT_USERNAME"] = "fatucci_test_bot"
os.environ["MAX_TOKEN"] = ""
os.environ["ADMIN_IDS"] = "777"
os.environ["ORDERS_CHAT_ID"] = "-100777"
os.environ["PAYMENT_PROVIDER_TOKEN"] = ""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import courier, db, repo, statuses  # noqa: E402
from app.channels import base  # noqa: E402
from app.channels.base import Channel, Event, Out  # noqa: E402
from app.router import route  # noqa: E402
from app.utils import fmt_date_iso, today  # noqa: E402

OK, FAIL = "✅", "❌"
failures: list[str] = []


def check(condition: bool, title: str) -> None:
    print(f"{OK if condition else FAIL} {title}")
    if not condition:
        failures.append(title)


class FakeChannel(Channel):
    """Заглушка мессенджера: всё «отправленное» складывается в список."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.title = name
        self.username = "fatucci_test_bot"
        self.sent: list[tuple[str, Out]] = []
        self.invoices: list[dict] = []
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

    async def send_document(self, chat_id: str, data: bytes, filename: str,
                            caption: str = "") -> bool:
        self.sent.append((str(chat_id), Out(text=f"[файл] {filename} {len(data)}b {caption}")))
        return True

    async def send_bytes(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> bool:
        self.sent.append((str(chat_id), Out(text=f"[png {len(data)}b] {filename}")))
        return True

    async def download_bytes(self, file_id: str) -> bytes:
        return bytes([0x89]) + b"PNG" + b"0" * 64

    async def send_invoice(self, chat_id, title, description, payload, amount_kop,
                           provider_token, label="К оплате", provider_data="") -> tuple[bool, str]:
        self.invoices.append({"chat_id": str(chat_id), "title": title,
                              "description": description, "payload": payload,
                              "amount": amount_kop, "token": provider_token})
        self.sent.append((str(chat_id), Out(text=f"[счёт] {title} — {amount_kop} коп.")))
        return True, ""

    # --- помощники теста ---
    def last(self) -> Out:
        return self.sent[-1][1]

    def texts(self) -> str:
        return "\n".join(out.text for _, out in self.sent)

    def buttons(self) -> list[str]:
        out = self.last()
        return [b.data or b.url for row in (out.kb or []) for b in row]

    def find_button(self, prefix: str) -> str:
        for _, out in reversed(self.sent):
            for row in out.kb or []:
                for btn in row:
                    if btn.data.startswith(prefix):
                        return btn.data
        return ""

    def clear(self) -> None:
        self.sent.clear()
        self.invoices.clear()


GUEST = "555"
ADMIN = "777"


def guest_event(kind: str, **kwargs) -> Event:
    return Event(channel="tg", user_id=GUEST, chat_id=GUEST, kind=kind,
                 full_name="Тестовый Гость", **kwargs)


def admin_event(kind: str, chat_id: str = ADMIN, **kwargs) -> Event:
    return Event(channel="tg", user_id=ADMIN, chat_id=chat_id, kind=kind,
                 username="manager", full_name="Менеджер", **kwargs)


async def main() -> None:
    ch = FakeChannel("tg")
    base.REGISTRY["tg"] = ch
    await db.init_db()
    await repo.set_setting("pay_enabled", "0")   # оплату включаем ниже, отдельным блоком

    print("\n— Сценарий гостя —")
    await route(guest_event("start", payload="demo1"), ch)
    check("Fatucci" in ch.texts(), "приветствие показано")
    check(any("g:order" in b for b in ch.buttons()), "есть кнопка «Заказать завтрак»")

    ch.clear()
    await route(guest_event("callback", payload="g:order", callback_id="c1"), ch)
    date_btn = ch.find_button("g:date:")
    check(bool(date_btn), f"предложены даты доставки ({date_btn})")

    ch.clear()
    await route(guest_event("callback", payload=date_btn, callback_id="c2"), ch)
    qty_btn = ch.find_button("g:qty:")
    check(bool(qty_btn), "показан выбор количества")
    check("Сет" in ch.texts(), "показан сет дня")

    ch.clear()
    await route(guest_event("callback", payload="g:qty:2", callback_id="c3"), ch)
    check("апартамент" in ch.texts().lower(), "запрошен номер апартаментов")

    ch.clear()
    await route(guest_event("text", text="45"), ch)
    check("телефон" in ch.texts().lower(), "запрошен телефон")
    check(ch.last().reply_contact != "", "предложена кнопка «Поделиться контактом»")

    ch.clear()
    await route(guest_event("contact", phone="+7 999 123-45-67"), ch)
    check("пожелания" in ch.texts().lower() or "комментар" in ch.texts().lower(),
          "запрошен комментарий")

    ch.clear()
    await route(guest_event("callback", payload="g:skip", callback_id="c4"), ch)
    check("Проверьте заказ" in ch.texts(), "показана карточка проверки")
    check("1 800" in ch.texts().replace(" ", " "), "итоговая сумма 900 × 2 = 1 800 ₽")

    ch.clear()
    await route(guest_event("callback", payload="g:confirm", callback_id="c5"), ch)
    orders = await repo.list_orders(limit=5)
    check(len(orders) == 1, "заказ создан в базе")
    order = orders[0]
    check(order["status"] == statuses.NEW, "статус нового заказа — «не оплачен»")
    check(order["apartment"] == "45" and order["qty"] == 2, "апартаменты и количество сохранены")
    check(order["phone"] == "+79991234567", "телефон нормализован")
    check(order["total_kop"] == 180000, "сумма посчитана верно")
    check(order["source_code"] == "demo1", "QR-метка объекта записана")
    check(any(chat == "-100777" for chat, _ in ch.sent), "заказ ушёл в рабочий чат менеджеров")
    check("НОВЫЙ ЗАКАЗ" in ch.texts(), "менеджер видит карточку заказа")

    print("\n— Действия менеджера —")
    ch.clear()
    await route(admin_event("callback", chat_id="-100777",
                            payload=f"a:ord:{order['id']}:{statuses.ACCEPTED}",
                            callback_id="a1"), ch)
    order = await repo.get_order(order["id"])
    check(order["status"] == statuses.ACCEPTED, "статус → «принят в работу»")
    check("оплат" in ch.texts().lower(), "гостю ушло сообщение про оплату")

    ch.clear()
    await route(admin_event("callback", chat_id="-100777",
                            payload=f"a:ord:{order['id']}:{statuses.PAID}", callback_id="a2"), ch)
    order = await repo.get_order(order["id"])
    check(order["status"] == statuses.PAID, "статус → «оплачен»")
    check(order["paid_at"] != "", "зафиксировано время оплаты")

    ch.clear()
    await route(admin_event("callback", chat_id="-100777",
                            payload=f"a:ord:{order['id']}:{statuses.DELIVERED}",
                            callback_id="a3"), ch)
    order = await repo.get_order(order["id"])
    check(order["status"] == statuses.DELIVERED, "статус → «доставлен»")

    ch.clear()
    await route(guest_event("callback", payload=f"g:got:{order['id']}", callback_id="c6"), ch)
    order = await repo.get_order(order["id"])
    check(order["status"] == statuses.RECEIVED, "гость подтвердил получение → «получен»")

    print("\n— Выгрузка курьерам —")
    await repo.set_setting("courier_statuses", "received,paid,accepted,delivered")
    digest = await courier.build_digest(order["delivery_date"] and
                                        __import__("datetime").date.fromisoformat(
                                            order["delivery_date"]))
    check("апарт. 45" in digest, "в сводке есть номер апартаментов")
    check("Северная" in digest, "в сводке есть адрес объекта")
    check(digest.count("\n") > 2, "сводка — одно многострочное сообщение")
    # форма курьерской службы, присланная заказчиком 18.08
    for field in ("📥 Откуда:", "📍 Адрес:", "👥 Получатель:", "⏰ Когда:",
                  "✏️ Комментарий:", "Доставка:"):
        check(field in digest, f"в сводке есть поле формы курьера «{field}»")
    check("Fatucci fine food" in digest, "подставлен адрес, откуда забирать заказ")
    check("отчитаться о доставке" in digest, "подставлен комментарий курьеру")
    check("+7 (999) 123-45-67" in digest, "телефон получателя в читаемом виде")

    print("\n— Админ-панель —")
    ch.clear()
    await route(admin_event("text", text="/admin"), ch)
    check("Админ-панель" in ch.texts(), "админка открывается по /admin")
    for section, title in [("a:o:l:new:0", "заказы"), ("a:m:l", "меню"), ("a:r:l", "ротация"),
                           ("a:b:l", "объекты"), ("a:q:l", "QR"), ("a:cur:m", "курьеры"),
                           ("a:t:l", "тексты"), ("a:f:l", "доп. предложения"),
                           ("a:u:l:0", "гости"), ("a:bc:m", "рассылка"), ("a:cfg:m", "настройки"),
                           ("a:st:m", "статистика"), ("a:x:m", "экспорт")]:
        ch.clear()
        await route(admin_event("callback", payload=section, callback_id="x"), ch)
        check(len(ch.sent) > 0 and len(ch.last().text) > 10, f"раздел админки: {title}")

    print("\n— Справка в админке —")
    from app import guide
    ch.clear()
    await route(admin_event("callback", payload="a:hp", callback_id="h0"), ch)
    check("Справка по админ-панели" in ch.texts(), "оглавление справки открывается")
    check(len(ch.last().kb) >= len(guide.ORDER), "в оглавлении есть все разделы")
    missing = [k for k in guide.ORDER if not guide.body(k).strip()]
    check(not missing, f"у каждого раздела есть текст (пустых: {len(missing)})")
    broken: list[str] = []
    for key in guide.ORDER:
        ch.clear()
        await route(admin_event("callback", payload=f"a:hp:{key}", callback_id="h"), ch)
        text = ch.texts()
        has_nav = any("a:hp" == (b.data or "") for r in (ch.last().kb or []) for b in r)
        if len(text) < 150 or not has_nav:
            broken.append(guide.title(key))
    check(not broken, f"все {len(guide.ORDER)} разделов справки открываются "
                      f"с текстом и навигацией{' — сломаны: ' + ', '.join(broken) if broken else ''}")

    ch.clear()
    await route(admin_event("callback", payload="a:g:menu", callback_id="g1"), ch)
    check("Скидки за количество" in ch.texts() or
          any("cfg:s:price" in (b.data or "") for r in ch.last().kb for b in r),
          "группа «Меню и цены» ведёт к скидкам")
    ch.clear()
    await route(admin_event("callback", payload="a:cfg:s:price", callback_id="g2"), ch)
    check("скидк" in ch.texts().lower(), "раздел скидок открывается")

    ch.clear()
    await route(admin_event("callback", payload="a:cfg:s:cur", callback_id="g3"), ch)
    check("Откуда забирать" in ch.texts(), "настройки формы курьера доступны")

    ch.clear()
    await route(admin_event("callback", payload="a:st:d30", callback_id="x"), ch)
    check("Статистика" in ch.texts(), "статистика за 30 дней считается")

    ch.clear()
    await route(admin_event("callback", payload="a:x:p30", callback_id="x"), ch)
    check("[файл]" in ch.texts(), "CSV-выгрузка формируется")

    ch.clear()
    await route(admin_event("callback", payload="a:q:o:1", callback_id="x"), ch)
    check("[png" in ch.texts(), "QR-код генерируется картинкой")
    check("t.me/fatucci_test_bot?start=demo1" in ch.texts(), "ссылка QR содержит код объекта")

    print("\n— Проверка ограничений —")
    ch.clear()
    obj = await repo.get_object_by_code("demo1")
    await repo.update_object(obj["id"], delivery_days="1")   # только понедельник
    from app import availability
    obj = await repo.get_object_by_code("demo1")
    dates = await availability.available_dates(obj)
    check(all(d.isoweekday() == 1 for d, _ in dates), "даты фильтруются по дням доставки")
    await repo.update_object(obj["id"], delivery_days="1,2,3,4,5,6,7")

    ch.clear()
    await route(guest_event("start", payload="obshiy"), ch)
    await route(guest_event("callback", payload="g:order", callback_id="c7"), ch)
    check(bool(ch.find_button("g:obj:")), "общий QR предлагает выбрать объект")

    ch.clear()
    user = await repo.get_user("tg", GUEST)
    await repo.set_blocked(user["id"], True)
    await route(guest_event("text", text="привет"), ch)
    check("недоступно" in ch.texts(), "чёрный список работает")
    await repo.set_blocked(user["id"], False)

    print("\n— Время отсечки заказов —")
    obj = await repo.get_object_by_code("demo1")
    await repo.update_object(obj["id"], cutoff_time="00:01", lead_days=1)
    obj = await repo.get_object_by_code("demo1")
    dates = [d for d, _ in await availability.available_dates(obj)]
    tomorrow = today() + __import__("datetime").timedelta(days=1)
    check(tomorrow not in dates, "после отсечки завтрашняя дата не предлагается")
    await repo.update_object(obj["id"], cutoff_time="23:59")
    obj = await repo.get_object_by_code("demo1")
    dates = [d for d, _ in await availability.available_dates(obj)]
    check(tomorrow in dates, "до отсечки завтрашняя дата доступна")
    ok, reason = await availability.check_date(obj, today() - __import__("datetime").timedelta(days=3))
    check(not ok, f"заказ в прошлое отклоняется ({reason})")

    print("\n— Правка данных из админки —")
    ch.clear()
    await route(admin_event("callback", payload=f"a:b:e:{obj['id']}:price_kop", callback_id="x"), ch)
    await route(admin_event("text", text="1250"), ch)
    obj = await repo.get_object_by_code("demo1")
    check(obj["price_kop"] == 125000, "цена объекта изменилась на 1 250 ₽")

    ch.clear()
    await route(admin_event("callback", payload="a:cfg:e:gen:manager_phone", callback_id="x"), ch)
    await route(admin_event("text", text="+7 900 111-22-33"), ch)
    check(await repo.get_setting("manager_phone") == "+7 900 111-22-33", "настройка сохраняется")

    ch.clear()
    await route(admin_event("callback", payload="a:cfg:t:gen:orders_paused", callback_id="x"), ch)
    check(await repo.get_bool("orders_paused"), "переключатель в настройках работает")
    await route(admin_event("callback", payload="a:cfg:t:gen:orders_paused", callback_id="x"), ch)
    check(not await repo.get_bool("orders_paused"), "переключатель возвращается обратно")

    ch.clear()
    await route(admin_event("callback", payload="a:t:e:welcome", callback_id="x"), ch)
    await route(admin_event("text", text="Новое приветствие"), ch)
    check(await repo.get_text("welcome") == "Новое приветствие", "текст бота редактируется")

    print("\n— Отмена и отказ —")
    ch.clear()
    await route(guest_event("start", payload="demo1"), ch)
    await route(guest_event("callback", payload="g:order", callback_id="d1"), ch)
    date_btn = ch.find_button("g:date:")
    await route(guest_event("callback", payload=date_btn, callback_id="d2"), ch)
    await route(guest_event("callback", payload="g:qty:1", callback_id="d3"), ch)
    await route(guest_event("text", text="12А"), ch)
    await route(guest_event("text", text="89991112233"), ch)
    await route(guest_event("callback", payload="g:skip", callback_id="d4"), ch)
    await route(guest_event("callback", payload="g:confirm", callback_id="d5"), ch)
    second = (await repo.list_orders(limit=1))[0]
    check(second["qty"] == 1 and second["apartment"] == "12А", "второй заказ оформлен")

    ch.clear()
    await route(guest_event("callback", payload=f"g:cancel:{second['id']}", callback_id="d6"), ch)
    second = await repo.get_order(second["id"])
    check(second["status"] == statuses.CANCELLED, "гость может отменить свой заказ")

    ch.clear()
    await route(guest_event("callback", payload="g:order", callback_id="d7"), ch)
    date_btn = ch.find_button("g:date:")
    await route(guest_event("callback", payload=date_btn, callback_id="d8"), ch)
    await route(guest_event("callback", payload="g:qty:1", callback_id="d9"), ch)
    await route(guest_event("callback", payload="g:reapt", callback_id="d10"), ch)
    await route(guest_event("callback", payload="g:rephone", callback_id="d11"), ch)
    await route(guest_event("callback", payload="g:skip", callback_id="d12"), ch)
    await route(guest_event("callback", payload="g:confirm", callback_id="d13"), ch)
    third = (await repo.list_orders(limit=1))[0]
    check(third["apartment"] == "12А" and third["phone"] == "+79991112233",
          "повторный заказ подставляет сохранённые данные")

    ch.clear()
    await route(admin_event("callback", chat_id="-100777",
                            payload=f"a:ord:{third['id']}:{statuses.REJECTED}", callback_id="r1"), ch)
    await route(admin_event("text", text="Нет свободных курьеров"), ch)
    third = await repo.get_order(third["id"])
    check(third["status"] == statuses.REJECTED, "менеджер может отклонить заказ")
    check("Нет свободных курьеров" in ch.texts(), "причина отказа доходит до гостя")

    print("\n— Незавершённый заказ и напоминание —")
    ch.clear()
    await route(guest_event("callback", payload="g:order", callback_id="e1"), ch)
    date_btn = ch.find_button("g:date:")
    await route(guest_event("callback", payload=date_btn, callback_id="e2"), ch)
    session = await repo.get_session("tg", GUEST)
    check(session is not None and session["state"] != "", "черновик заказа сохранён в базе")
    from app.utils import utc_stamp
    await db.execute("UPDATE sessions SET updated_at = ? "
                     "WHERE channel = 'tg' AND ext_id = ?", (utc_stamp(minutes=-90), GUEST))
    from app import scheduler
    ch.clear()
    await scheduler.reminder_tick()
    check("не закончили" in ch.texts() or "Продолжить" in str(ch.buttons()) or
          bool(ch.find_button("g:resume")), "напоминание о брошенном заказе отправлено")
    ch.clear()
    await route(guest_event("callback", payload="g:resume", callback_id="e3"), ch)
    check(bool(ch.find_button("g:qty:")), "по кнопке «Продолжить» гость возвращается на свой шаг")

    print("\n— Адаптер MAX —")
    from app.channels.max import MaxChannel
    mx = MaxChannel("test-token", "fatucci_max_bot")
    started = mx._parse({"update_type": "bot_started", "chat_id": 900,
                         "user": {"user_id": 11, "name": "Гость MAX", "username": "guest"},
                         "payload": "demo1"})
    check(started is not None and started.kind == "start" and started.payload == "demo1",
          "MAX: bot_started разбирается, стартовая метка получена")
    created = mx._parse({"update_type": "message_created", "message": {
        "sender": {"user_id": 11, "name": "Гость MAX"},
        "recipient": {"chat_id": 900, "chat_type": "dialog"},
        "body": {"mid": "mid.1", "text": "45"}}})
    check(created is not None and created.kind == "text" and created.text == "45",
          "MAX: обычное сообщение разбирается")
    contact = mx._parse({"update_type": "message_created", "message": {
        "sender": {"user_id": 11}, "recipient": {"chat_id": 900},
        "body": {"mid": "mid.2", "attachments": [
            {"type": "contact", "payload": {
                "vcf_info": "BEGIN:VCARD\nTEL;TYPE=CELL:+7 999 123-45-67\nEND:VCARD"}}]}}})
    check(contact is not None and contact.kind == "contact" and contact.phone == "+79991234567",
          "MAX: контакт разбирается, телефон извлечён")
    cb = mx._parse({"update_type": "message_callback",
                    "callback": {"callback_id": "cb1", "payload": "g:order",
                                 "user": {"user_id": 11}},
                    "message": {"recipient": {"chat_id": 900}, "body": {"mid": "mid.3"}}})
    check(cb is not None and cb.kind == "callback" and cb.payload == "g:order"
          and cb.callback_id == "cb1", "MAX: нажатие кнопки разбирается")
    body = await mx._body(Out(text="Привет", kb=[[base.Btn(text="Заказать", data="g:order"),
                                                  base.Btn(text="Сайт", url="https://a.ru")]],
                              reply_contact="Поделиться"))
    kbd = [a for a in body["attachments"] if a["type"] == "inline_keyboard"][0]["payload"]["buttons"]
    check(kbd[0][0]["type"] == "request_contact", "MAX: кнопка запроса контакта собрана")
    check(kbd[1][0]["type"] == "callback" and kbd[1][0]["payload"] == "g:order",
          "MAX: callback-кнопка собрана")
    check(kbd[1][1]["type"] == "link" and kbd[1][1]["url"] == "https://a.ru",
          "MAX: кнопка-ссылка собрана")
    check(body["format"] == "html" and len(body["text"]) <= 4000, "MAX: тело сообщения корректно")
    check(mx.start_link("demo1") == "https://max.ru/fatucci_max_bot?start=demo1",
          "MAX: ссылка для QR собирается верно")
    await mx.close()

    print("\n— Скидки за количество —")
    from app import pricing
    tiers = pricing.parse_tiers("3=5, 5=10, 10=15")
    check([t.qty for t in tiers] == [10, 5, 3], "пороги разобраны и отсортированы")
    check(pricing.percent_for(1, tiers) == 0, "на 1 набор скидки нет")
    check(pricing.percent_for(3, tiers) == 5, "от 3 наборов — 5%")
    check(pricing.percent_for(4, tiers) == 5, "между порогами держится нижний")
    check(pricing.percent_for(12, tiers) == 15, "выше верхнего порога — максимальная скидка")
    check(pricing.parse_tiers("ерунда") == [], "мусор в настройке скидок игнорируется")

    price = pricing.calc(90000, 5, tiers)
    check(price.percent == 10 and price.per_set == 81000,
          "цена сета со скидкой 10%: 900 ₽ → 810 ₽")
    check(price.total == 405000 and price.saved == 45000,
          "итог и экономия посчитаны верно")
    check(pricing.calc(90000, 1, tiers).total == 90000, "без скидки итог не меняется")

    await repo.set_setting("discount_tiers", "2=10")
    ch.clear()
    await route(guest_event("start", payload="demo1"), ch)
    await route(guest_event("callback", payload="g:order", callback_id="s1"), ch)
    date_btn = ch.find_button("g:date:")
    await route(guest_event("callback", payload=date_btn, callback_id="s2"), ch)
    check("−10%" in ch.texts(), "гостю показана скидка на шаге количества")
    await route(guest_event("callback", payload="g:qty:2", callback_id="s3"), ch)
    await route(guest_event("callback", payload="g:reapt", callback_id="s4"), ch)
    await route(guest_event("callback", payload="g:rephone", callback_id="s5"), ch)
    ch.clear()
    await route(guest_event("callback", payload="g:skip", callback_id="s6"), ch)
    check("Скидка −10%" in ch.texts(), "скидка видна в карточке проверки заказа")
    check(len(ch.last().kb) == 2, "на экране подтверждения всего две строки кнопок")
    ch.clear()
    await route(guest_event("callback", payload="g:edit", callback_id="s6e"), ch)
    check("Что поправить" in ch.texts(), "кнопка «Изменить» открывает список правок")
    ch.clear()
    await route(guest_event("callback", payload="g:back", callback_id="s6b"), ch)
    check("Проверьте заказ" in ch.texts(), "возврат к карточке заказа работает")
    await route(guest_event("callback", payload="g:confirm", callback_id="s7"), ch)
    disc_order = (await repo.list_orders(limit=1))[0]
    obj_now = await repo.get_object_by_code("demo1")
    expect_base = obj_now["price_kop"]
    expect_set = round(expect_base * 0.9 / 100) * 100
    check(disc_order["discount_pct"] == 10, "скидка сохранена в заказе")
    check(disc_order["base_price_kop"] == expect_base
          and disc_order["price_kop"] == expect_set,
          "в заказе есть и базовая цена, и цена со скидкой")
    check(disc_order["total_kop"] == expect_set * 2,
          f"итог заказа со скидкой посчитан верно ({expect_set * 2 / 100:.0f} ₽)")
    check(disc_order["customer_name"] != "", "имя получателя записано в заказ")
    await repo.set_setting("discount_tiers", "")

    print("\n— Оплата через PayMaster —")
    from app import orders_service, payments
    TEST_TOKEN = "123456789:TEST:abcdef0123456789abcd"
    check(payments.token_looks_valid(TEST_TOKEN), "токен провайдера распознан")
    check(payments.is_test(TEST_TOKEN), "режим определён как тестовый")
    check(not payments.token_looks_valid("test_abc"), "чужой формат ключа отбраковывается")
    check(payments.parse_payload(payments.invoice_payload(42)) == 42,
          "метка заказа в счёте читается обратно")

    await repo.set_setting("pm_token", TEST_TOKEN)
    await repo.set_setting("pay_enabled", "1")
    ok, message = await payments.check_setup()
    check(ok and "тестовый" in message.lower(), "проверка настройки предупреждает о тест-режиме")

    ch.clear()
    await route(guest_event("start", payload="demo1"), ch)
    await route(guest_event("callback", payload="g:order", callback_id="p1"), ch)
    date_btn = ch.find_button("g:date:")
    await route(guest_event("callback", payload=date_btn, callback_id="p2"), ch)
    await route(guest_event("callback", payload="g:qty:1", callback_id="p3"), ch)
    await route(guest_event("callback", payload="g:reapt", callback_id="p4"), ch)
    await route(guest_event("callback", payload="g:rephone", callback_id="p5"), ch)
    await route(guest_event("callback", payload="g:skip", callback_id="p6"), ch)
    await route(guest_event("callback", payload="g:confirm", callback_id="p7"), ch)
    pay_order = (await repo.list_orders(limit=1))[0]

    ch.clear()
    await route(admin_event("callback", chat_id="-100777",
                            payload=f"a:ord:{pay_order['id']}:{statuses.ACCEPTED}",
                            callback_id="p8"), ch)
    check(len(ch.invoices) == 1, "при принятии в работу гостю выставлен счёт")
    if ch.invoices:
        inv = ch.invoices[0]
        check(inv["amount"] == pay_order["total_kop"], "сумма счёта совпадает с заказом")
        check(inv["token"] == TEST_TOKEN, "счёт выставлен с токеном провайдера")
        check(len(inv["title"]) <= 32 and len(inv["description"]) <= 255,
              "заголовок и описание счёта укладываются в лимиты Telegram")
        check(inv["chat_id"] == GUEST, "счёт ушёл именно гостю")

    ch.clear()
    await route(guest_event("payment", payload=payments.invoice_payload(pay_order["id"]),
                            text=""), ch)
    pay_order = await repo.get_order(pay_order["id"])
    check(pay_order["status"] == statuses.PAID, "после оплаты заказ автоматически «оплачен»")
    check(pay_order["paid_at"] != "", "время оплаты зафиксировано")
    check("оплачено" in ch.texts().lower(), "гостю пришло подтверждение оплаты")

    ch.clear()
    await route(guest_event("payment", payload="order:999999"), ch)
    check(len(ch.sent) == 0, "оплата по несуществующему заказу не роняет бота")

    max_ch = FakeChannel("max")
    base.REGISTRY["max"] = max_ch
    max_order = await repo.create_order(
        channel="max", ext_id="42", chat_id="900", object_id=1,
        object_title="Тест", object_address="Адрес", set_id=1, set_title="Сет",
        delivery_date=fmt_date_iso(today()), qty=1, apartment="7", phone="+79990000000",
        price_kop=90000, total_kop=90000, status=statuses.NEW, source_code="demo1")
    ok, error = await orders_service.send_invoice(max_order)
    check(not ok and not error, "в MAX счёт не выставляется — там нет встроенных платежей")
    check(len(max_ch.invoices) == 0, "и не пытается: гостю MAX счёт не уходит")

    max_ch.clear()
    await orders_service.change_status(max_order["id"], statuses.ACCEPTED, actor="тест")
    check("менеджер свяжется" in max_ch.texts().lower(),
          "гость в MAX получает сообщение, что с оплатой поможет менеджер")
    base.REGISTRY.pop("max", None)

    await repo.set_setting("pay_enabled", "0")

    print("\n— MAX подключается из админ-панели —")
    import main as botmain
    await repo.set_setting("max_token", "max-token-from-panel")
    await repo.set_setting("max_username", "fatucci_max_bot")
    token, username = await botmain.max_credentials()
    check(token == "max-token-from-panel" and username == "fatucci_max_bot",
          "токен и username MAX читаются из настроек, а не из .env")
    check(await botmain.max_still_on("max-token-from-panel"), "канал MAX поднимается")
    await repo.set_setting("max_enabled", "0")
    check(not await botmain.max_still_on("max-token-from-panel"),
          "выключение MAX в админке останавливает опрос")
    await repo.set_setting("max_enabled", "1")
    await repo.set_setting("max_token", "another-token")
    check(not await botmain.max_still_on("max-token-from-panel"),
          "смена токена в админке перезапускает канал")
    await repo.set_setting("max_token", "")

    print("\n— Доступ: владелец и менеджеры —")
    from app import admins
    from app.config import cfg as app_cfg
    saved_env_admins = app_cfg.admin_ids
    app_cfg.admin_ids = []                      # как на чистом хостинге без ADMIN_IDS
    await db.execute("DELETE FROM admins")
    admins.invalidate()

    OWNER, MANAGER = "901", "902"
    ch.clear()
    await route(Event(channel="tg", user_id=OWNER, chat_id=OWNER, kind="start",
                      full_name="Первый Написавший"), ch)
    check(await admins.is_admin(OWNER), "первый написавший боту стал владельцем")
    check(await admins.is_owner(OWNER), "он помечен именно как владелец")
    check("владелец" in ch.texts().lower(), "владельцу пришло сообщение о выданном доступе")

    ch.clear()
    await route(Event(channel="tg", user_id=MANAGER, chat_id=MANAGER, kind="start",
                      full_name="Второй Гость"), ch)
    check(not await admins.is_admin(MANAGER), "второй пользователь прав не получает")
    check("владелец" not in ch.texts().lower(), "второму про владельца не пишут")

    ch.clear()
    await route(Event(channel="tg", user_id=MANAGER, chat_id=MANAGER, kind="text",
                      text="/admin"), ch)
    check("Админ-панель" not in ch.texts(), "не-админ не может открыть админку")

    ch.clear()
    await route(Event(channel="tg", user_id=OWNER, chat_id=OWNER, kind="callback",
                      payload="a:acc:add", callback_id="z1"), ch)
    await route(Event(channel="tg", user_id=OWNER, chat_id=OWNER, kind="text",
                      text=MANAGER), ch)
    check(await admins.is_admin(MANAGER), "владелец добавил менеджера через админку")
    check("доступ к админ-панели" in ch.texts().lower(), "новому админу пришло уведомление")

    ch.clear()
    await route(Event(channel="tg", user_id=MANAGER, chat_id=MANAGER, kind="text",
                      text="/admin"), ch)
    check("Админ-панель" in ch.texts(), "новый менеджер видит админку")

    ok, _ = await admins.remove(int(MANAGER))
    check(ok and not await admins.is_admin(MANAGER), "менеджера можно убрать")
    ok, message = await admins.remove(int(OWNER))
    check(not ok, f"владельца убрать нельзя ({message})")

    ch.clear()
    await route(Event(channel="tg", user_id=OWNER, chat_id=OWNER, kind="callback",
                      payload="a:acc:l", callback_id="z2"), ch)
    check("Доступ к админ-панели" in ch.texts(), "раздел «Доступ» открывается")

    app_cfg.admin_ids = saved_env_admins
    admins.invalidate()
    check(await admins.is_admin(ADMIN), "ADMIN_IDS из окружения остаётся аварийным входом")

    await db.close()
    print()
    if failures:
        print(f"{FAIL} Провалено проверок: {len(failures)}")
        for item in failures:
            print("   •", item)
        sys.exit(1)
    print(f"{OK} Все проверки пройдены.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
