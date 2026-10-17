from datetime import date, datetime, timedelta

import pytest

from app import booking
from app.booking import BookingError

# Понедельник, 08:00 — задолго до начала работы
MONDAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 8, 0)


def hhmm(slots):
    return [s.strftime("%H:%M") for s in slots]


def test_free_slots_basic(session, data):
    service, master = data
    slots = booking.get_free_slots(session, master.id, service.id, MONDAY, NOW)
    # окно 10-14, услуга 60 мин, шаг 30 мин -> последний старт 13:00
    assert hhmm(slots) == ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00"]


def test_no_slots_on_weekend(session, data):
    service, master = data
    saturday = date(2026, 10, 24)
    assert booking.get_free_slots(session, master.id, service.id, saturday, NOW) == []


def test_min_lead_time_hides_close_slots(session, data):
    service, master = data
    now = datetime(2026, 10, 19, 10, 15)  # ближайший слот не раньше 11:15 -> 11:30
    slots = booking.get_free_slots(session, master.id, service.id, MONDAY, now)
    assert hhmm(slots)[0] == "11:30"


def test_booking_blocks_overlapping_slots(session, data):
    service, master = data
    client = booking.get_or_create_client(session, 111, "Иван")
    booking.create_booking(
        session, client, master.id, service.id, datetime(2026, 10, 19, 11, 0), NOW
    )
    slots = hhmm(booking.get_free_slots(session, master.id, service.id, MONDAY, NOW))
    # 11:00-12:00 занято: недоступны старты, пересекающиеся с этим окном
    assert "10:30" not in slots
    assert "11:00" not in slots
    assert "11:30" not in slots
    assert "10:00" in slots
    assert "12:00" in slots


def test_double_booking_is_rejected(session, data):
    service, master = data
    c1 = booking.get_or_create_client(session, 111, "Иван")
    c2 = booking.get_or_create_client(session, 222, "Пётр")
    start = datetime(2026, 10, 19, 12, 0)
    booking.create_booking(session, c1, master.id, service.id, start, NOW)
    with pytest.raises(BookingError):
        booking.create_booking(session, c2, master.id, service.id, start, NOW)


def test_cannot_book_outside_working_hours(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 111, "Иван")
    with pytest.raises(BookingError):
        booking.create_booking(
            session, c, master.id, service.id, datetime(2026, 10, 19, 9, 0), NOW
        )


def test_cancel_frees_slot(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 111, "Иван")
    start = datetime(2026, 10, 19, 12, 0)
    b = booking.create_booking(session, c, master.id, service.id, start, NOW)
    booking.cancel_booking(session, b.id, tg_id=111)
    slots = booking.get_free_slots(session, master.id, service.id, MONDAY, NOW)
    assert start in slots


def test_cancel_foreign_booking_forbidden(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 111, "Иван")
    b = booking.create_booking(
        session, c, master.id, service.id, datetime(2026, 10, 19, 12, 0), NOW
    )
    with pytest.raises(BookingError):
        booking.cancel_booking(session, b.id, tg_id=999)


def test_cancel_twice_raises(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 111, "Иван")
    b = booking.create_booking(
        session, c, master.id, service.id, datetime(2026, 10, 19, 12, 0), NOW
    )
    booking.cancel_booking(session, b.id)
    with pytest.raises(BookingError):
        booking.cancel_booking(session, b.id)


def test_client_bookings_only_active_and_future(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 111, "Иван")
    b1 = booking.create_booking(
        session, c, master.id, service.id, datetime(2026, 10, 19, 11, 0), NOW
    )
    b2 = booking.create_booking(
        session, c, master.id, service.id, datetime(2026, 10, 19, 13, 0), NOW
    )
    booking.cancel_booking(session, b2.id)
    result = booking.client_bookings(session, 111, NOW)
    assert [b.id for b in result] == [b1.id]


def test_due_reminders(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 111, "Иван")
    b = booking.create_booking(
        session, c, master.id, service.id, datetime(2026, 10, 19, 12, 0), NOW
    )
    # за 3 часа до визита — рано (окно 120 мин)
    assert booking.due_reminders(session, datetime(2026, 10, 19, 9, 0), 120) == []
    # за 90 минут — пора
    due = booking.due_reminders(session, datetime(2026, 10, 19, 10, 30), 120)
    assert [x.id for x in due] == [b.id]
    b.reminder_sent = True
    session.flush()
    assert booking.due_reminders(session, datetime(2026, 10, 19, 10, 30), 120) == []


def test_available_days_skips_days_without_slots(session, data):
    service, master = data
    days = booking.available_days(session, master.id, service.id, NOW)
    assert MONDAY in days
    assert date(2026, 10, 24) not in days  # суббота
    assert date(2026, 10, 25) not in days  # воскресенье
    assert all(d <= NOW.date() + timedelta(days=booking.settings.booking_days_ahead) for d in days)


def test_get_or_create_client_updates_name(session):
    a = booking.get_or_create_client(session, 5, "Старое", "old")
    b = booking.get_or_create_client(session, 5, "Новое", "new")
    assert a.id == b.id
    assert b.name == "Новое" and b.username == "new"
