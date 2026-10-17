"""Сквозная проверка сценариев бота без сети: Telegram API подменяется фейковой сессией."""

import asyncio
from dataclasses import replace
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, Message
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import bot as bot_module
from app import db, keyboards as kb
from app.models import ACTIVE, CANCELLED, Booking

NOW = datetime(2026, 10, 19, 8, 0)  # понедельник
ADMIN_ID = 999


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


@pytest.fixture()
def env(engine, data, monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(db, "now_local", lambda: NOW)
    monkeypatch.setattr(
        bot_module, "settings", replace(bot_module.settings, admin_ids=[ADMIN_ID])
    )
    fake = FakeSession()
    bot = Bot(
        "123456789:" + "A" * 35,
        session=fake,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return bot_module.build_dispatcher(), bot, fake, data


def user(uid, name="Иван"):
    return {"id": uid, "is_bot": False, "first_name": name, "username": f"user{uid}"}


def cb_update(n, uid, data):
    return {
        "update_id": n,
        "callback_query": {
            "id": str(n),
            "from": user(uid),
            "chat_instance": "x",
            "data": data,
            "message": {
                "message_id": 10,
                "date": 1760000000,
                "chat": {"id": uid, "type": "private"},
                "text": "prev",
            },
        },
    }


def start_update(n, uid):
    return {
        "update_id": n,
        "message": {
            "message_id": n,
            "date": 1760000000,
            "chat": {"id": uid, "type": "private"},
            "from": user(uid),
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }


def buttons(method):
    return [b.callback_data for row in method.reply_markup.inline_keyboard for b in row]


def last(fake, cls):
    return [c for c in fake.calls if isinstance(c, cls)][-1]


def feed(dp, bot, update):
    return asyncio.run(dp.feed_raw_update(bot, update))


def test_full_booking_flow_and_cancel(env, session):
    dp, bot, fake, (service, master) = env
    uid = 111

    feed(dp, bot, start_update(1, uid))
    assert "бот записи" in last(fake, SendMessage).text
    assert buttons(last(fake, SendMessage)) == ["book", "my"]

    feed(dp, bot, cb_update(2, uid, "book"))
    assert last(fake, EditMessageText).text == "Выберите услугу:"
    assert f"s:{service.id}" in buttons(last(fake, EditMessageText))

    # один мастер -> шаг выбора мастера пропускается, сразу даты
    feed(dp, bot, cb_update(3, uid, f"s:{service.id}"))
    assert "Выберите дату" in last(fake, EditMessageText).text
    day_cb = f"d:{service.id}:{master.id}:20261019"
    assert day_cb in buttons(last(fake, EditMessageText))

    feed(dp, bot, cb_update(4, uid, day_cb))
    assert "Выберите время" in last(fake, EditMessageText).text
    time_cb = f"t:{service.id}:{master.id}:202610191100"
    assert time_cb in buttons(last(fake, EditMessageText))

    feed(dp, bot, cb_update(5, uid, time_cb))
    assert "Проверьте запись" in last(fake, EditMessageText).text

    ok_cb = f"ok:{service.id}:{master.id}:202610191100"
    feed(dp, bot, cb_update(6, uid, ok_cb))
    assert "Вы записаны" in last(fake, EditMessageText).text
    admin_msg = last(fake, SendMessage)
    assert admin_msg.chat_id == ADMIN_ID and "Новая запись" in admin_msg.text

    session.expire_all()
    booking = session.scalars(select(Booking)).one()
    assert booking.status == ACTIVE and booking.start_at == datetime(2026, 10, 19, 11, 0)

    # «Мои записи» показывает запись и кнопку отмены
    feed(dp, bot, cb_update(7, uid, "my"))
    my = last(fake, EditMessageText)
    assert "19.10.2026" in my.text and f"x:{booking.id}" in buttons(my)

    feed(dp, bot, cb_update(8, uid, f"x:{booking.id}"))
    assert "нет предстоящих" in last(fake, EditMessageText).text
    assert "Отмена записи" in last(fake, SendMessage).text
    session.expire_all()
    assert session.get(Booking, booking.id).status == CANCELLED


def test_second_client_cannot_take_busy_slot(env, session):
    dp, bot, fake, (service, master) = env
    ok_cb = f"ok:{service.id}:{master.id}:202610191100"
    feed(dp, bot, cb_update(1, 111, ok_cb))
    feed(dp, bot, cb_update(2, 222, ok_cb))
    alerts = [c for c in fake.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]
    assert len(alerts) == 1 and "занято" in alerts[0].text
    session.expire_all()
    assert len(session.scalars(select(Booking)).all()) == 1


def test_user_cannot_cancel_foreign_booking(env, session):
    dp, bot, fake, (service, master) = env
    feed(dp, bot, cb_update(1, 111, f"ok:{service.id}:{master.id}:202610191100"))
    booking = session.scalars(select(Booking)).one()
    feed(dp, bot, cb_update(2, 222, f"x:{booking.id}"))
    alerts = [c for c in fake.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]
    assert any("не ваша" in a.text for a in alerts)
    session.expire_all()
    assert session.get(Booking, booking.id).status == ACTIVE


def test_reminder_sent_once(env, session, monkeypatch):
    dp, bot, fake, (service, master) = env
    feed(dp, bot, cb_update(1, 111, f"ok:{service.id}:{master.id}:202610191100"))
    # за 60 минут до визита — внутри окна напоминания (120 мин)
    monkeypatch.setattr(db, "now_local", lambda: datetime(2026, 10, 19, 10, 0))
    before = len(fake.calls)
    assert asyncio.run(bot_module.send_due_reminders(bot)) == 1
    reminder = fake.calls[before]
    assert isinstance(reminder, SendMessage) and reminder.chat_id == 111
    assert "Напоминание" in reminder.text
    assert asyncio.run(bot_module.send_due_reminders(bot)) == 0


def test_callback_data_within_telegram_limit():
    start = datetime(2026, 12, 31, 23, 30)
    big = 2**31
    samples = [
        f"s:{big}",
        f"m:{big}:{big}",
        f"d:{big}:{big}:{start:%Y%m%d}",
        f"t:{big}:{big}:{start:%Y%m%d%H%M}",
        f"ok:{big}:{big}:{start:%Y%m%d%H%M}",
        f"x:{big}",
    ]
    assert all(len(s.encode()) <= 64 for s in samples)
    assert kb.parse_ts(f"{start:%Y%m%d%H%M}") == start
