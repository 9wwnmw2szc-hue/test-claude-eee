"""Привязка услуг к мастерам: что видит клиент и что разрешает логика записи."""

from datetime import date, datetime, time

import pytest

from app import booking
from app.booking import BookingError
from app.models import Master, Service, WorkingHours
from tests.bot_helpers import NOW, buttons, cb_update, feed, last
from aiogram.methods import EditMessageText

MONDAY = date(2026, 10, 19)


@pytest.fixture()
def team(session, data):
    """Анна (ничего не отмечено — делает всё) и Мария (только стрижка); вторая услуга — маникюр."""
    haircut, anna = data
    manicure = Service(name="Маникюр", duration_min=90, price=2000)
    maria = Master(name="Мария")
    maria.services = [haircut]
    maria.hours = [WorkingHours(weekday=d, start=time(10, 0), end=time(14, 0)) for d in range(5)]
    session.add_all([manicure, maria])
    session.commit()
    return haircut, manicure, anna, maria


def test_master_without_links_does_all_services(session, team):
    haircut, manicure, anna, maria = team
    assert [m.id for m in booking.masters_for_service(session, haircut.id)] == [anna.id, maria.id]
    assert [m.id for m in booking.masters_for_service(session, manicure.id)] == [anna.id]


def test_inactive_master_is_not_offered(session, team):
    haircut, _, anna, maria = team
    maria.active = False
    session.commit()
    assert [m.id for m in booking.masters_for_service(session, haircut.id)] == [anna.id]


def test_no_slots_for_service_master_does_not_do(session, team):
    _, manicure, _, maria = team
    assert booking.get_free_slots(session, maria.id, manicure.id, MONDAY, NOW) == []


def test_cannot_book_service_master_does_not_do(session, team):
    _, manicure, _, maria = team
    client = booking.get_or_create_client(session, 111, "Иван")
    with pytest.raises(BookingError, match="не выполняет"):
        booking.create_booking(
            session, client, maria.id, manicure.id, datetime(2026, 10, 19, 11, 0), NOW
        )


def test_bot_offers_only_masters_who_do_the_service(bot_env, team):
    haircut, manicure, anna, maria = team
    env = bot_env()

    # стрижку делают обе — бот предлагает выбрать мастера
    feed(env.dp, env.bot, cb_update(1, 111, f"s:{haircut.id}"))
    shown = last(env.fake, EditMessageText)
    assert shown.text == "Выберите мастера:"
    assert f"m:{haircut.id}:{anna.id}" in buttons(shown)
    assert f"m:{haircut.id}:{maria.id}" in buttons(shown)

    # маникюр делает только Анна — выбор пропускается, сразу даты
    feed(env.dp, env.bot, cb_update(2, 111, f"s:{manicure.id}"))
    shown = last(env.fake, EditMessageText)
    assert "Выберите дату" in shown.text
    day_buttons = [cb.split(":") for cb in buttons(shown) if cb.startswith("d:")]
    assert day_buttons and all(parts[2] == str(anna.id) for parts in day_buttons)  # d:услуга:мастер:дата


def test_bot_says_so_when_nobody_does_the_service(bot_env, session, team):
    _, manicure, anna, maria = team
    anna.active = False  # единственный, кто делал маникюр
    session.commit()
    env = bot_env()
    feed(env.dp, env.bot, cb_update(1, 111, f"s:{manicure.id}"))
    assert "нет мастеров" in last(env.fake, EditMessageText).text
