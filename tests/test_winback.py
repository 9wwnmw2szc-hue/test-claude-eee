"""Рассылка «давно не были»: кому пишем, как не повторяемся и как отписаться."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

from aiogram.methods import SendMessage

from app import booking, db
from app import bot as bot_module
from app.models import Client
from tests.bot_helpers import calls_of, cb_update, feed, command_update

CREATED = datetime(2026, 9, 1, 8, 0)
NOW = datetime(2026, 10, 19, 8, 0)
AFTER = 30


def visit(session, data, tg_id, start):
    """Состоявшийся визит клиента."""
    service, master = data
    client = booking.get_or_create_client(session, tg_id, f"Клиент{tg_id}")
    b = booking.create_booking(session, client, master.id, service.id, start, CREATED)
    booking.mark_done(session, b.id, start + timedelta(hours=2))
    session.commit()
    return client


def ids(session, now=NOW, after=AFTER):
    return [c.tg_id for c in booking.winback_candidates(session, now, after)]


def test_only_clients_absent_long_enough(session, data):
    visit(session, data, 1, datetime(2026, 9, 14, 11, 0))  # 35 дней назад — пора
    visit(session, data, 2, datetime(2026, 10, 12, 11, 0))  # неделя назад — рано
    assert ids(session) == [1]
    assert ids(session, after=0) == []  # рассылка выключена


def test_clients_without_completed_visits_are_not_contacted(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 5, "Новичок")
    booking.create_booking(session, c, master.id, service.id, datetime(2026, 9, 14, 11, 0), CREATED)
    session.commit()  # запись есть, но визит не состоялся
    assert ids(session) == []


def test_open_booking_optout_and_repeat_are_skipped(session, data):
    service, master = data
    a = visit(session, data, 1, datetime(2026, 9, 14, 11, 0))
    visit(session, data, 2, datetime(2026, 9, 14, 12, 0))
    visit(session, data, 3, datetime(2026, 9, 15, 11, 0))
    booking.create_booking(session, a, master.id, service.id, datetime(2026, 10, 21, 11, 0), NOW)
    booking.set_promo_optout(session, 2, True)
    session.commit()
    assert ids(session) == [3]  # у 1 уже есть запись, 2 отписался

    session.query(Client).filter_by(tg_id=3).one().last_promo_at = NOW
    session.commit()
    assert ids(session, now=NOW + timedelta(days=60)) == []  # после напоминания второй раз не пишем

    visit(session, data, 3, datetime(2026, 10, 20, 11, 0))  # снова пришёл — круг начинается заново
    assert ids(session, now=NOW + timedelta(days=60)) == [3]


def test_send_winback_message_and_single_delivery(bot_env, session, data, monkeypatch):
    env = bot_env(winback_after_days=AFTER)
    visit(session, data, 1, datetime(2026, 9, 14, 11, 0))
    monkeypatch.setattr(db, "now_local", lambda: NOW)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    assert asyncio.run(bot_module.send_winback(env.bot)) == 1
    msg = calls_of(env.fake, SendMessage)[-1]
    assert msg.chat_id == 1 and "давно вас не видели" in msg.text and "/stop" in msg.text
    assert asyncio.run(bot_module.send_winback(env.bot)) == 0


def test_stop_and_resume_commands(bot_env, session, data):
    env = bot_env()
    visit(session, data, 1, datetime(2026, 9, 14, 11, 0))
    feed(env.dp, env.bot, command_update(1, 1, "/stop"))
    session.expire_all()
    assert session.query(Client).filter_by(tg_id=1).one().no_promo is True
    feed(env.dp, env.bot, command_update(2, 1, "/resume"))
    session.expire_all()
    assert session.query(Client).filter_by(tg_id=1).one().no_promo is False
