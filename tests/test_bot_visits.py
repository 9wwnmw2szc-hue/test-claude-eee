"""Сквозные сценарии бота: перенос записи и оценка визита."""

import asyncio
from datetime import datetime

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select

from app import booking, db
from app import bot as bot_module
from app.models import ACTIVE, DONE, Booking
from tests.bot_helpers import (
    ADMIN_ID,
    buttons,
    calls_of,
    cb_update,
    feed,
    last,
)

PAYING = {"payment_provider_token": "TEST:PROVIDER", "prepayment_percent": 30}


def ok_cb(env, hhmm="1100"):
    return f"ok:{env.service.id}:{env.master.id}:20261019{hhmm}"


def alerts(fake):
    return [c.text for c in calls_of(fake, AnswerCallbackQuery) if c.show_alert]


def admin_messages(fake):
    return [c for c in calls_of(fake, SendMessage) if c.chat_id == ADMIN_ID]


def make_booking(env, session, uid=111, hhmm="1100"):
    feed(env.dp, env.bot, cb_update(1, uid, ok_cb(env, hhmm)))
    session.expire_all()
    return session.scalars(select(Booking).order_by(Booking.id.desc())).first()


# ---------- перенос ----------


def test_client_reschedules_booking(bot_env, session):
    env = bot_env()
    b = make_booking(env, session)

    feed(env.dp, env.bot, cb_update(2, 111, "my"))
    assert f"rs:{b.id}" in buttons(last(env.fake, EditMessageText))

    feed(env.dp, env.bot, cb_update(3, 111, f"rs:{b.id}"))
    days = last(env.fake, EditMessageText)
    assert "Выберите новую дату" in days.text and f"rd:{b.id}:20261019" in buttons(days)

    feed(env.dp, env.bot, cb_update(4, 111, f"rd:{b.id}:20261019"))
    times = last(env.fake, EditMessageText)
    assert "Выберите новое время" in times.text
    assert f"rt:{b.id}:202610191130" in buttons(times)  # соседнее время доступно: своё место не мешает

    feed(env.dp, env.bot, cb_update(5, 111, f"rt:{b.id}:202610191300"))
    assert "Перенести запись" in last(env.fake, EditMessageText).text
    assert f"rok:{b.id}:202610191300" in buttons(last(env.fake, EditMessageText))

    feed(env.dp, env.bot, cb_update(6, 111, f"rok:{b.id}:202610191300"))
    assert "Запись перенесена" in last(env.fake, EditMessageText).text
    admin = admin_messages(env.fake)[-1].text
    assert "Перенос записи" in admin and "19.10.2026 11:00" in admin and "19.10.2026 13:00" in admin

    session.expire_all()
    moved = session.get(Booking, b.id)
    assert (moved.start_at, moved.status) == (datetime(2026, 10, 19, 13, 0), ACTIVE)


def test_reschedule_into_taken_time_is_refused(bot_env, session):
    env = bot_env()
    mine = make_booking(env, session, uid=111, hhmm="1100")
    make_booking(env, session, uid=222, hhmm="1300")

    feed(env.dp, env.bot, cb_update(2, 111, f"rok:{mine.id}:202610191300"))
    assert any("занято" in t for t in alerts(env.fake))
    session.expire_all()
    assert session.get(Booking, mine.id).start_at == datetime(2026, 10, 19, 11, 0)


def test_reschedule_too_close_to_visit(bot_env, session, monkeypatch):
    env = bot_env()
    b = make_booking(env, session)
    monkeypatch.setattr(db, "now_local", lambda: datetime(2026, 10, 19, 10, 30))
    feed(env.dp, env.bot, cb_update(2, 111, f"rs:{b.id}"))
    assert any("не позднее чем за 60 мин" in t for t in alerts(env.fake))


def test_cannot_reschedule_foreign_pending_or_cancelled_booking(bot_env, session):
    env = bot_env()
    b = make_booking(env, session)
    feed(env.dp, env.bot, cb_update(2, 222, f"rs:{b.id}"))  # чужая запись
    feed(env.dp, env.bot, cb_update(3, 222, f"rok:{b.id}:202610191300"))
    assert all("уже нельзя" in t or "не ваша" in t for t in alerts(env.fake)) and alerts(env.fake)
    session.expire_all()
    assert session.get(Booking, b.id).start_at == datetime(2026, 10, 19, 11, 0)

    feed(env.dp, env.bot, cb_update(4, 111, f"x:{b.id}"))  # отменённую переносить нельзя
    before = len(alerts(env.fake))
    feed(env.dp, env.bot, cb_update(5, 111, f"rs:{b.id}"))
    assert len(alerts(env.fake)) == before + 1 and "уже нельзя" in alerts(env.fake)[-1]


def test_pending_booking_has_no_reschedule_button(bot_env, session):
    env = bot_env(**PAYING)
    b = make_booking(env, session)
    feed(env.dp, env.bot, cb_update(2, 111, "my"))
    shown = buttons(last(env.fake, EditMessageText))
    assert f"pay:{b.id}" in shown and f"rs:{b.id}" not in shown


def test_reminder_is_resent_for_new_time(bot_env, session, monkeypatch):
    env = bot_env()
    b = make_booking(env, session)
    monkeypatch.setattr(db, "now_local", lambda: datetime(2026, 10, 19, 9, 30))
    assert asyncio.run(bot_module.send_due_reminders(env.bot)) == 1  # про 11:00

    feed(env.dp, env.bot, cb_update(2, 111, f"rok:{b.id}:202610191300"))  # последний старт в окне 10–14
    monkeypatch.setattr(db, "now_local", lambda: datetime(2026, 10, 19, 11, 30))
    assert asyncio.run(bot_module.send_due_reminders(env.bot)) == 1  # про 13:00


# ---------- оценка ----------


def finish_visit(session, b, now):
    booking.mark_done(session, b.id, now)
    session.commit()


def test_review_request_and_rating_flow(bot_env, session, monkeypatch):
    env = bot_env()
    b = make_booking(env, session)
    after = datetime(2026, 10, 19, 12, 30)
    monkeypatch.setattr(db, "now_local", lambda: after)

    # визит ещё не отмечен — просить нечего
    assert asyncio.run(bot_module.send_review_requests(env.bot)) == 0

    finish_visit(session, b, after)
    assert asyncio.run(bot_module.send_review_requests(env.bot)) == 1
    request = last(env.fake, SendMessage)
    assert request.chat_id == 111 and "Оцените визит" in request.text
    assert buttons(request) == [f"rate:{b.id}:{n}" for n in range(1, 6)]
    assert asyncio.run(bot_module.send_review_requests(env.bot)) == 0  # повторно не просим

    admin_before = len(admin_messages(env.fake))
    feed(env.dp, env.bot, cb_update(2, 111, f"rate:{b.id}:5"))
    assert "Спасибо за оценку" in last(env.fake, EditMessageText).text
    session.expire_all()
    assert session.get(Booking, b.id).rating == 5
    assert len(admin_messages(env.fake)) == admin_before  # хорошая оценка админов не тревожит

    feed(env.dp, env.bot, cb_update(3, 111, f"rate:{b.id}:1"))  # переоценить нельзя
    assert any("уже оценили" in t for t in alerts(env.fake))
    session.expire_all()
    assert session.get(Booking, b.id).rating == 5


def test_low_rating_alerts_admin(bot_env, session, monkeypatch):
    env = bot_env()
    b = make_booking(env, session)
    after = datetime(2026, 10, 19, 12, 30)
    monkeypatch.setattr(db, "now_local", lambda: after)
    finish_visit(session, b, after)

    feed(env.dp, env.bot, cb_update(2, 111, f"rate:{b.id}:2"))
    alert = admin_messages(env.fake)[-1].text
    assert "Низкая оценка 2/5" in alert and f"#{b.id}" in alert


def test_only_the_client_can_rate_and_only_after_the_visit(bot_env, session, monkeypatch):
    env = bot_env()
    b = make_booking(env, session)
    feed(env.dp, env.bot, cb_update(2, 111, f"rate:{b.id}:5"))  # визит ещё не состоялся
    assert any("только свой состоявшийся визит" in t for t in alerts(env.fake))

    after = datetime(2026, 10, 19, 12, 30)
    monkeypatch.setattr(db, "now_local", lambda: after)
    finish_visit(session, b, after)
    feed(env.dp, env.bot, cb_update(3, 222, f"rate:{b.id}:5"))  # чужой визит
    session.expire_all()
    assert session.get(Booking, b.id).rating == 0 and session.get(Booking, b.id).status == DONE


def test_no_show_gets_no_review_request(bot_env, session, monkeypatch):
    env = bot_env()
    b = make_booking(env, session)
    after = datetime(2026, 10, 19, 12, 30)
    monkeypatch.setattr(db, "now_local", lambda: after)
    booking.mark_no_show(session, b.id, after)
    session.commit()
    assert asyncio.run(bot_module.send_review_requests(env.bot)) == 0


def test_maintenance_loop_runs_all_jobs(bot_env, monkeypatch):
    """Цикл вызывает все фоновые задачи и переживает ошибку в одной из них."""
    env = bot_env()
    ran = []

    async def ok(name):
        ran.append(name)

    async def boom(bot):
        ran.append("boom")
        raise RuntimeError("сбой")

    monkeypatch.setattr(bot_module, "send_due_reminders", lambda bot: boom(bot))
    monkeypatch.setattr(bot_module, "expire_unpaid_bookings", lambda bot: ok("expire"))
    monkeypatch.setattr(bot_module, "auto_close_old_visits", lambda bot: ok("auto"))
    monkeypatch.setattr(bot_module, "send_winback", lambda bot: ok("winback"))
    monkeypatch.setattr(bot_module, "send_review_requests", lambda bot: ok("review"))

    async def one_round():
        async def stop(_):
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", stop)
        try:
            await bot_module.reminder_loop(env.bot)
        except asyncio.CancelledError:
            pass

    asyncio.run(one_round())
    assert ran == ["boom", "expire", "auto", "review", "winback"]
