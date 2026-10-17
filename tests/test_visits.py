"""Итоги визитов, оценка, перенос записи и статистика (логика без Telegram и веба)."""

from datetime import datetime, time, timedelta

import pytest

from app import booking, stats
from app.booking import BookingError
from app.models import ACTIVE, DONE, NO_SHOW, PENDING, Master, Service, WorkingHours

CREATED = datetime(2026, 10, 1, 8, 0)  # когда делались записи
NOW = datetime(2026, 10, 19, 8, 0)  # понедельник, «сейчас» для статистики


def book(session, data, tg_id, start, service=None, master=None, now=CREATED, name="Клиент"):
    default_service, default_master = data
    client = booking.get_or_create_client(session, tg_id, f"{name} {tg_id}")
    return booking.create_booking(
        session, client, (master or default_master).id, (service or default_service).id, start, now
    )


# ---------- итог визита ----------


def test_mark_done_only_after_visit_started(session, data):
    b = book(session, data, 1, datetime(2026, 10, 19, 11, 0))
    with pytest.raises(BookingError, match="ещё не начался"):
        booking.mark_done(session, b.id, datetime(2026, 10, 19, 10, 59))
    assert booking.mark_done(session, b.id, datetime(2026, 10, 19, 11, 0)).status == DONE
    with pytest.raises(BookingError):  # повторно отметить нельзя
        booking.mark_done(session, b.id, NOW)


def test_pending_booking_cannot_be_closed(session, data):
    service, master = data
    client = booking.get_or_create_client(session, 1, "Иван")
    b = booking.create_booking(
        session, client, master.id, service.id, datetime(2026, 10, 19, 11, 0), CREATED, prepay_amount=450
    )
    assert b.status == PENDING
    with pytest.raises(BookingError):
        booking.mark_done(session, b.id, NOW + timedelta(days=1))


def test_no_show_frees_time_but_done_keeps_it(session, data):
    service, master = data
    start = datetime(2026, 10, 19, 11, 0)
    missed = book(session, data, 1, start)
    booking.mark_no_show(session, missed.id, start)
    assert missed.status == NO_SHOW
    assert start in booking.get_free_slots(session, master.id, service.id, start.date(), start - timedelta(hours=3))

    other = datetime(2026, 10, 19, 13, 0)
    came = book(session, data, 2, other)
    booking.mark_done(session, came.id, other)
    assert other not in booking.get_free_slots(session, master.id, service.id, other.date(), other - timedelta(hours=3))


def test_done_visit_cannot_be_cancelled_or_listed_as_upcoming(session, data):
    start = datetime(2026, 10, 19, 11, 0)
    b = book(session, data, 1, start)
    booking.mark_done(session, b.id, start)
    with pytest.raises(BookingError):
        booking.cancel_booking(session, b.id, tg_id=1)
    assert booking.client_bookings(session, 1, start - timedelta(days=1)) == []


def test_close_lists(session, data):
    old = book(session, data, 1, datetime(2026, 10, 5, 11, 0))
    fresh = book(session, data, 2, datetime(2026, 10, 19, 11, 0))
    future = book(session, data, 3, datetime(2026, 10, 26, 11, 0))
    now = datetime(2026, 10, 19, 13, 0)
    assert [b.id for b in booking.bookings_to_close(session, now)] == [fresh.id, old.id]  # новые сверху
    booking.mark_done(session, fresh.id, now)
    booking.mark_no_show(session, old.id, now)
    assert booking.bookings_to_close(session, now) == []
    assert [b.id for b in booking.recently_closed(session, now)] == [fresh.id]  # old — старше 14 дней
    assert future.status == ACTIVE


# ---------- оценка ----------


def test_review_requests_are_due_once(session, data):
    start = datetime(2026, 10, 19, 11, 0)
    done = book(session, data, 1, start)
    missed = book(session, data, 2, datetime(2026, 10, 19, 13, 0))
    booking.mark_done(session, done.id, start)
    booking.mark_no_show(session, missed.id, datetime(2026, 10, 19, 13, 0))

    now = datetime(2026, 10, 19, 14, 0)
    assert [b.id for b in booking.due_review_requests(session, now)] == [done.id]
    done.review_sent = True
    assert booking.due_review_requests(session, now) == []


def test_review_request_not_sent_for_old_visits(session, data):
    start = datetime(2026, 10, 5, 11, 0)
    b = book(session, data, 1, start)
    booking.mark_done(session, b.id, start)
    assert booking.due_review_requests(session, start + timedelta(days=2)) != []
    assert booking.due_review_requests(session, start + timedelta(days=5)) == []


def test_set_rating_rules(session, data):
    start = datetime(2026, 10, 19, 11, 0)
    b = book(session, data, 1, start)
    with pytest.raises(BookingError):  # визит ещё не отмечен как состоявшийся
        booking.set_rating(session, b.id, 1, 5)
    booking.mark_done(session, b.id, start)
    for bad in (0, 6, -1):
        with pytest.raises(BookingError, match="от 1 до 5"):
            booking.set_rating(session, b.id, 1, bad)
    with pytest.raises(BookingError):  # чужой визит
        booking.set_rating(session, b.id, 999, 5)
    assert booking.set_rating(session, b.id, 1, 4).rating == 4
    with pytest.raises(BookingError, match="уже оценили"):
        booking.set_rating(session, b.id, 1, 5)


# ---------- перенос ----------


def test_reschedule_moves_visit_and_frees_old_time(session, data):
    service, master = data
    b = book(session, data, 1, datetime(2026, 10, 19, 11, 0))
    b.reminder_sent = True
    new_start = datetime(2026, 10, 19, 13, 0)
    moved, old_start = booking.reschedule_booking(session, b.id, new_start, CREATED, tg_id=1)
    assert old_start == datetime(2026, 10, 19, 11, 0)
    assert (moved.start_at, moved.end_at) == (new_start, new_start + timedelta(minutes=60))
    assert moved.reminder_sent is False  # напоминание придёт про новое время
    free = booking.get_free_slots(session, master.id, service.id, new_start.date(), CREATED)
    assert datetime(2026, 10, 19, 11, 0) in free and new_start not in free


def test_reschedule_to_overlapping_neighbour_time(session, data):
    """Сдвиг на полчаса: новое время пересекается со старым временем этой же записи — это допустимо."""
    b = book(session, data, 1, datetime(2026, 10, 19, 11, 0))
    moved, _ = booking.reschedule_booking(session, b.id, datetime(2026, 10, 19, 11, 30), CREATED, tg_id=1)
    assert moved.start_at == datetime(2026, 10, 19, 11, 30)


def test_reschedule_rejects_taken_time(session, data):
    b = book(session, data, 1, datetime(2026, 10, 19, 11, 0))
    book(session, data, 2, datetime(2026, 10, 19, 13, 0))
    with pytest.raises(BookingError, match="занято"):
        booking.reschedule_booking(session, b.id, datetime(2026, 10, 19, 13, 0), CREATED, tg_id=1)
    with pytest.raises(BookingError, match="занято"):  # перекрытие с чужой записью
        booking.reschedule_booking(session, b.id, datetime(2026, 10, 19, 12, 30), CREATED, tg_id=1)
    assert b.start_at == datetime(2026, 10, 19, 11, 0)


def test_reschedule_rejects_outsiders_and_wrong_status(session, data):
    b = book(session, data, 1, datetime(2026, 10, 19, 11, 0))
    target = datetime(2026, 10, 19, 13, 0)
    with pytest.raises(BookingError, match="не ваша"):
        booking.reschedule_booking(session, b.id, target, CREATED, tg_id=2)
    with pytest.raises(BookingError, match="не найдена"):
        booking.reschedule_booking(session, 9999, target, CREATED, tg_id=1)
    booking.cancel_booking(session, b.id)
    with pytest.raises(BookingError, match="подтверждённую"):
        booking.reschedule_booking(session, b.id, target, CREATED, tg_id=1)


def test_reschedule_deadline(session, data):
    b = book(session, data, 1, datetime(2026, 10, 19, 11, 0))
    target = datetime(2026, 10, 19, 13, 0)
    too_late = datetime(2026, 10, 19, 10, 30)  # до визита 30 минут
    with pytest.raises(BookingError, match="не позднее чем за 60 мин"):
        booking.reschedule_booking(session, b.id, target, too_late, tg_id=1, min_before_min=60)
    on_time = datetime(2026, 10, 19, 10, 0)  # ровно за час — ещё можно
    assert booking.reschedule_booking(session, b.id, target, on_time, tg_id=1, min_before_min=60)[0].start_at == target


# ---------- цена и статистика ----------


def test_price_snapshot_survives_price_change(session, data):
    service, _ = data
    b = book(session, data, 1, datetime(2026, 10, 19, 11, 0))
    service.price = 2500
    session.flush()
    assert b.price == 1500


@pytest.fixture()
def busy_month(session, data):
    """Набор визитов с известными итогами: 3 состоялись (7000 ₽), 1 не пришёл, 1 отменён, 1 впереди."""
    haircut, anna = data
    coloring = Service(name="Окрашивание", duration_min=120, price=4000)
    maria = Master(name="Мария")
    maria.hours = [WorkingHours(weekday=d, start=time(10, 0), end=time(14, 0)) for d in range(5)]
    session.add_all([coloring, maria])
    session.flush()

    def visit(tg, day, hhmm, service, master, outcome):
        start = datetime(2026, 10, day, *hhmm)
        b = book(session, data, tg, start, service=service, master=master)
        if outcome == "done":
            booking.mark_done(session, b.id, start)
        elif outcome == "no_show":
            booking.mark_no_show(session, b.id, start)
        elif outcome == "cancelled":
            booking.cancel_booking(session, b.id)
        return b

    a1 = visit(1, 5, (11, 0), haircut, anna, "done")
    a2 = visit(1, 6, (10, 0), coloring, anna, "done")
    visit(2, 7, (11, 0), haircut, maria, "done")
    visit(2, 8, (11, 0), haircut, anna, "no_show")
    visit(3, 9, (11, 0), haircut, anna, "cancelled")
    visit(3, 20, (11, 0), haircut, anna, "future")
    old = visit(4, 1, (11, 0), haircut, anna, "done")  # октябрь, но до начала окна ниже
    booking.set_rating(session, a1.id, 1, 5)
    booking.set_rating(session, a2.id, 1, 3)
    return old


def test_period_stats_totals(session, busy_month):
    # окно с 3 по 19 октября: визит 1 октября в него не попадает
    result = stats.period_stats(session, datetime(2026, 10, 3), NOW, NOW)
    assert (result["done"], result["revenue"], result["avg_check"]) == (3, 7000, 2333)
    assert (result["no_show"], result["cancelled"], result["upcoming"]) == (1, 1, 1)
    assert result["attendance"] == 75  # пришли 3 из 4 отмеченных
    assert (result["avg_rating"], result["rated_count"]) == (4.0, 2)


def test_period_stats_breakdowns(session, busy_month):
    result = stats.period_stats(session, datetime(2026, 10, 3), NOW, NOW)
    assert result["by_service"] == [
        {"name": "Окрашивание", "visits": 1, "revenue": 4000},
        {"name": "Стрижка", "visits": 2, "revenue": 3000},
    ]
    assert {m["name"]: (m["visits"], m["revenue"]) for m in result["by_master"]} == {
        "Анна": (2, 5500),
        "Мария": (1, 1500),
    }
    assert [(c["name"], c["visits"], c["revenue"]) for c in result["top_clients"]] == [
        ("Клиент 1", 2, 5500),
        ("Клиент 2", 1, 1500),
    ]


def test_period_stats_respects_window(session, busy_month):
    wide = stats.period_stats(session, datetime(2026, 10, 1), NOW, NOW)
    assert wide["done"] == 4 and wide["revenue"] == 8500
    assert stats.period_stats(session, datetime(2026, 11, 1), datetime(2026, 12, 1), NOW)["done"] == 0


def test_stats_use_current_price_for_old_bookings_without_snapshot(session, data):
    service, _ = data
    start = datetime(2026, 10, 19, 11, 0)
    b = book(session, data, 1, start)
    booking.mark_done(session, b.id, start)
    b.price = 0  # запись из времён до появления поля price
    service.price = 1800
    session.flush()
    assert stats.period_stats(session, datetime(2026, 10, 1), NOW + timedelta(days=1), NOW)["revenue"] == 1800


def test_stats_on_empty_database(session):
    result = stats.period_stats(session, datetime(2026, 10, 1), NOW, NOW)
    assert result["done"] == 0 and result["revenue"] == 0 and result["avg_check"] == 0
    assert result["attendance"] is None and result["avg_rating"] is None
    assert result["by_service"] == result["by_master"] == result["top_clients"] == []
