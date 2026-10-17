"""Предоплата: логика бронирования с удержанием времени и сквозные сценарии оплаты в боте."""

import asyncio
from datetime import datetime, timedelta

import pytest
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    EditMessageText,
    SendInvoice,
    SendMessage,
)
from sqlalchemy import select

from app import booking, db
from app import bot as bot_module
from app.booking import BookingError
from app.config import Settings
from app.models import ACTIVE, CANCELLED, PENDING, Booking
from tests.bot_helpers import (
    ADMIN_ID,
    NOW,
    buttons,
    calls_of,
    cb_update,
    feed,
    last,
    payment_update,
    pre_checkout_update,
)

START = datetime(2026, 10, 19, 11, 0)
PAYING = {"payment_provider_token": "TEST:PROVIDER", "prepayment_percent": 30}  # 30% от 1500 = 450


def ok_cb(env, hhmm="1100"):
    return f"ok:{env.service.id}:{env.master.id}:20261019{hhmm}"


def admin_messages(fake):
    return [c for c in calls_of(fake, SendMessage) if c.chat_id == ADMIN_ID]


# ---------- логика ----------


@pytest.mark.parametrize(
    "price, percent, expected",
    [(1500, 30, 450), (1001, 30, 301), (0, 30, 0), (1500, 0, 0), (1500, 100, 1500), (100, 150, 100)],
)
def test_prepayment_amount(price, percent, expected):
    assert booking.prepayment_for(price, percent) == expected


def test_prepayment_needs_token_and_percent():
    assert not Settings(payment_provider_token="", prepayment_percent=30).prepayment_enabled
    assert not Settings(payment_provider_token="T", prepayment_percent=0).prepayment_enabled
    assert Settings(payment_provider_token="T", prepayment_percent=30).prepayment_enabled
    with pytest.raises(SystemExit):
        Settings(prepayment_percent=101)


def make_pending(session, data, tg_id=111, start=START):
    service, master = data
    client = booking.get_or_create_client(session, tg_id, "Иван")
    return booking.create_booking(
        session, client, master.id, service.id, start, NOW, prepay_amount=450
    )


def test_pending_booking_holds_slot_but_gets_no_reminder(session, data):
    service, master = data
    b = make_pending(session, data)
    assert b.status == PENDING and b.prepay_amount == 450
    free = booking.get_free_slots(session, master.id, service.id, START.date(), NOW)
    assert START not in free
    assert booking.due_reminders(session, START - timedelta(hours=1)) == []
    assert [x.id for x in booking.client_bookings(session, 111, NOW)] == [b.id]


def test_cancelling_pending_frees_slot(session, data):
    service, master = data
    b = make_pending(session, data)
    booking.cancel_booking(session, b.id, tg_id=111)
    assert START in booking.get_free_slots(session, master.id, service.id, START.date(), NOW)


def test_confirm_payment_activates_once(session, data):
    b = make_pending(session, data)
    got, outcome = booking.confirm_payment(session, b.id, 450, "ch-1", NOW)
    assert outcome == "confirmed" and got.status == ACTIVE
    assert (got.paid_amount, got.payment_charge_id) == (450, "ch-1")
    assert booking.confirm_payment(session, b.id, 450, "ch-1", NOW)[1] == "duplicate"


def test_confirm_payment_for_unknown_booking(session):
    assert booking.confirm_payment(session, 12345, 450, "ch", NOW) == (None, "unknown")


def test_expire_unpaid_respects_hold_window(session, data):
    service, master = data
    pending = make_pending(session, data)
    paid = make_pending(session, data, tg_id=222, start=datetime(2026, 10, 19, 13, 0))
    booking.confirm_payment(session, paid.id, 450, "ch-2", NOW)

    assert booking.expire_unpaid(session, NOW + timedelta(minutes=14), 15) == []
    expired = booking.expire_unpaid(session, NOW + timedelta(minutes=15), 15)
    assert [b.id for b in expired] == [pending.id]
    assert pending.status == CANCELLED and paid.status == ACTIVE
    assert START in booking.get_free_slots(session, master.id, service.id, START.date(), NOW)


def test_late_payment_reactivates_when_slot_still_free(session, data):
    b = make_pending(session, data)
    booking.expire_unpaid(session, NOW + timedelta(minutes=20), 15)
    got, outcome = booking.confirm_payment(session, b.id, 450, "ch-1", NOW + timedelta(minutes=21))
    assert outcome == "reactivated" and got.status == ACTIVE


def test_late_payment_needs_refund_when_slot_taken(session, data):
    service, master = data
    b = make_pending(session, data)
    booking.expire_unpaid(session, NOW + timedelta(minutes=20), 15)
    other = booking.get_or_create_client(session, 222, "Пётр")
    booking.create_booking(session, other, master.id, service.id, START, NOW + timedelta(minutes=21))
    got, outcome = booking.confirm_payment(session, b.id, 450, "ch-1", NOW + timedelta(minutes=22))
    assert outcome == "refund_needed" and got.status == CANCELLED
    assert got.paid_amount == 450  # деньги получены — это видно в админке


def test_late_payment_for_past_visit_needs_refund(session, data):
    b = make_pending(session, data)
    booking.expire_unpaid(session, NOW + timedelta(minutes=20), 15)
    after_visit = START + timedelta(hours=2)
    assert booking.confirm_payment(session, b.id, 450, "ch-1", after_visit)[1] == "refund_needed"


# ---------- сценарии в боте ----------


def test_no_invoice_when_prepayment_disabled(bot_env, session):
    env = bot_env()
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    assert calls_of(env.fake, SendInvoice) == []
    assert "Вы записаны" in last(env.fake, EditMessageText).text
    session.expire_all()
    assert session.scalars(select(Booking)).one().status == ACTIVE


def test_prepayment_holds_slot_and_sends_invoice(bot_env, session):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    session.expire_all()
    b = session.scalars(select(Booking)).one()
    assert b.status == PENDING and b.prepay_amount == 450

    invoice = last(env.fake, SendInvoice)
    assert invoice.chat_id == 111
    assert invoice.payload == f"b:{b.id}"
    assert invoice.provider_token == "TEST:PROVIDER" and invoice.currency == "RUB"
    assert invoice.prices[0].amount == 45000  # копейки
    assert "зарезервировано" in last(env.fake, EditMessageText).text
    assert f"pay:{b.id}" in buttons(last(env.fake, EditMessageText))
    assert admin_messages(env.fake) == []  # админа зовём после оплаты, а не после брони

    # пока бронь удерживается, время недоступно другим
    feed(env.dp, env.bot, cb_update(2, 222, ok_cb(env)))
    alerts = [c for c in calls_of(env.fake, AnswerCallbackQuery) if c.show_alert]
    assert len(alerts) == 1 and "занято" in alerts[0].text


def test_confirmation_screen_mentions_prepayment(bot_env):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, f"t:{env.service.id}:{env.master.id}:202610191100"))
    assert "Предоплата: <b>450 ₽</b>" in last(env.fake, EditMessageText).text


def test_pre_checkout_validation(bot_env, session, monkeypatch):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    b = session.scalars(select(Booking)).one()

    def verdict(update):
        feed(env.dp, env.bot, update)
        return last(env.fake, AnswerPreCheckoutQuery)

    assert verdict(pre_checkout_update(2, 111, b.id, 450)).ok is True
    assert verdict(pre_checkout_update(3, 222, b.id, 450)).ok is False  # чужая запись
    assert verdict(pre_checkout_update(4, 111, b.id, 300)).ok is False  # не та сумма
    assert verdict(pre_checkout_update(5, 111, b.id, 450, currency="USD")).ok is False
    assert verdict(pre_checkout_update(6, 111, 9999, 450)).ok is False  # нет такой записи

    monkeypatch.setattr(db, "now_local", lambda: NOW + timedelta(minutes=16))
    expired = verdict(pre_checkout_update(7, 111, b.id, 450))
    assert expired.ok is False and "истекло" in expired.error_message


def test_successful_payment_confirms_booking(bot_env, session, monkeypatch):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    b = session.scalars(select(Booking)).one()
    feed(env.dp, env.bot, payment_update(2, 111, b.id, 450, charge="ch-42"))

    session.expire_all()
    b = session.get(Booking, b.id)
    assert (b.status, b.paid_amount, b.payment_charge_id) == (ACTIVE, 450, "ch-42")
    client_msg = calls_of(env.fake, SendMessage)[-2]
    assert client_msg.chat_id == 111 and "Предоплата 450 ₽ получена" in client_msg.text
    admin_msg = last(env.fake, SendMessage)
    assert admin_msg.chat_id == ADMIN_ID and "Новая запись" in admin_msg.text

    # теперь запись оплачена и получит напоминание
    monkeypatch.setattr(db, "now_local", lambda: datetime(2026, 10, 19, 10, 0))
    assert asyncio.run(bot_module.send_due_reminders(env.bot)) == 1


def test_unpaid_booking_expires_and_slot_returns(bot_env, session, monkeypatch):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    monkeypatch.setattr(db, "now_local", lambda: NOW + timedelta(minutes=16))

    assert asyncio.run(bot_module.expire_unpaid_bookings(env.bot)) == 1
    notice = last(env.fake, SendMessage)
    assert notice.chat_id == 111 and "бронь снята" in notice.text
    session.expire_all()
    assert session.scalars(select(Booking)).one().status == CANCELLED

    # слот снова свободен: другой клиент успешно бронирует то же время
    feed(env.dp, env.bot, cb_update(2, 222, ok_cb(env)))
    session.expire_all()
    assert [b.status for b in session.scalars(select(Booking).order_by(Booking.id))] == [CANCELLED, PENDING]
    assert asyncio.run(bot_module.expire_unpaid_bookings(env.bot)) == 0  # новая бронь ещё свежая


def test_invoice_failure_releases_slot(bot_env, session):
    env = bot_env(**PAYING)
    env.fake.fail_invoice = True
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    session.expire_all()
    assert session.scalars(select(Booking)).one().status == CANCELLED
    assert "не получилось выставить счёт" in last(env.fake, EditMessageText).text.lower()
    assert "Не удалось выставить счёт" in admin_messages(env.fake)[-1].text


def test_cancelling_paid_booking_flags_refund_for_admin(bot_env, session):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    b = session.scalars(select(Booking)).one()
    feed(env.dp, env.bot, payment_update(2, 111, b.id, 450, charge="ch-42"))

    feed(env.dp, env.bot, cb_update(3, 111, f"x:{b.id}"))
    answers = [c.text for c in calls_of(env.fake, AnswerCallbackQuery) if c.text]
    assert any("предоплатой решит администратор" in t for t in answers)
    admin_msg = admin_messages(env.fake)[-1]
    assert "возврат" in admin_msg.text and "450" in admin_msg.text and "ch-42" in admin_msg.text
    session.expire_all()
    assert session.get(Booking, b.id).status == CANCELLED


def test_my_bookings_lets_client_pay_again(bot_env, session):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    b = session.scalars(select(Booking)).one()

    feed(env.dp, env.bot, cb_update(2, 111, "my"))
    shown = last(env.fake, EditMessageText)
    assert "ждёт предоплаты 450 ₽" in shown.text and f"pay:{b.id}" in buttons(shown)

    feed(env.dp, env.bot, cb_update(3, 111, f"pay:{b.id}"))
    assert len(calls_of(env.fake, SendInvoice)) == 2

    feed(env.dp, env.bot, cb_update(4, 222, f"pay:{b.id}"))  # чужая запись счёт не получит
    assert len(calls_of(env.fake, SendInvoice)) == 2
    assert "уже оплачена или отменена" in last(env.fake, AnswerCallbackQuery).text


def test_payment_after_hold_expired_restores_booking_if_slot_free(bot_env, session, monkeypatch):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    b = session.scalars(select(Booking)).one()
    monkeypatch.setattr(db, "now_local", lambda: NOW + timedelta(minutes=16))
    asyncio.run(bot_module.expire_unpaid_bookings(env.bot))

    feed(env.dp, env.bot, payment_update(2, 111, b.id, 450))
    session.expire_all()
    assert session.get(Booking, b.id).status == ACTIVE
    assert "восстановлена" in admin_messages(env.fake)[-1].text


def test_payment_after_hold_expired_needs_refund_if_slot_taken(bot_env, session, monkeypatch):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env)))
    first = session.scalars(select(Booking)).one()
    monkeypatch.setattr(db, "now_local", lambda: NOW + timedelta(minutes=16))
    asyncio.run(bot_module.expire_unpaid_bookings(env.bot))
    feed(env.dp, env.bot, cb_update(2, 222, ok_cb(env)))  # время заняли

    feed(env.dp, env.bot, payment_update(3, 111, first.id, 450, charge="ch-late"))
    session.expire_all()
    assert session.get(Booking, first.id).status == CANCELLED
    to_client = [c for c in calls_of(env.fake, SendMessage) if c.chat_id == 111][-1]
    assert "вернёт предоплату" in to_client.text
    admin_msg = admin_messages(env.fake)[-1]
    assert "Нужен возврат 450" in admin_msg.text and "ch-late" in admin_msg.text


def test_payment_without_known_booking_is_escalated(bot_env):
    env = bot_env(**PAYING)
    feed(env.dp, env.bot, payment_update(1, 111, 0, 450, charge="ch-x", payload="garbage"))
    assert "запись не найдена" in [c for c in calls_of(env.fake, SendMessage) if c.chat_id == 111][-1].text
    assert "ch-x" in admin_messages(env.fake)[-1].text


def test_create_booking_still_rejects_double_booking_with_prepayment(session, data):
    service, master = data
    make_pending(session, data)
    other = booking.get_or_create_client(session, 222, "Пётр")
    with pytest.raises(BookingError):
        booking.create_booking(session, other, master.id, service.id, START, NOW, prepay_amount=450)
