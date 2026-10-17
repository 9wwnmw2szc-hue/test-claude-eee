"""Телефон клиента: нормализация, запрос после первой записи, защита от чужих контактов."""

import pytest
from aiogram.methods import SendMessage
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy import select

from app import booking
from app.booking import BookingError
from app.models import Client
from tests.bot_helpers import (
    ADMIN_ID,
    calls_of,
    cb_update,
    command_update,
    contact_update,
    feed,
    last,
    msg_update,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+7 (999) 123-45-67", "+79991234567"),
        ("8 999 123-45-67", "+79991234567"),  # российский формат с 8
        ("79991234567", "+79991234567"),
        ("+380 44 123 45 67", "+380441234567"),
    ],
)
def test_normalize_phone(raw, expected):
    assert booking.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "123", "не телефон", "+7 999"])
def test_normalize_phone_rejects_garbage(raw):
    with pytest.raises(BookingError):
        booking.normalize_phone(raw)


def test_set_client_phone_creates_and_updates(session):
    booking.set_client_phone(session, 5, "8 999 111-22-33", "Иван", "ivan")
    booking.set_client_phone(session, 5, "+7 999 444-55-66")
    client = session.scalars(select(Client)).one()
    assert (client.name, client.username, client.phone) == ("Иван", "ivan", "+79994445566")


def phone_prompts(fake, uid):
    return [
        c
        for c in calls_of(fake, SendMessage)
        if c.chat_id == uid and isinstance(c.reply_markup, ReplyKeyboardMarkup)
    ]


def ok_cb(env, hhmm):
    """Подтверждение записи на понедельник 19.10.2026 в hhmm (например, «1100»)."""
    return f"ok:{env.service.id}:{env.master.id}:20261019{hhmm}"


def test_phone_requested_once_after_first_booking(bot_env):
    env = bot_env()
    feed(env.dp, env.bot, cb_update(1, 111, ok_cb(env, "1100")))
    prompts = phone_prompts(env.fake, 111)
    assert len(prompts) == 1
    buttons_flat = [b for row in prompts[0].reply_markup.keyboard for b in row]
    assert any(b.request_contact for b in buttons_flat)

    # вторая запись того же клиента — больше не спрашиваем
    feed(env.dp, env.bot, cb_update(2, 111, ok_cb(env, "1300")))
    assert len(phone_prompts(env.fake, 111)) == 1


def test_own_contact_is_saved_and_keyboard_removed(bot_env, session):
    env = bot_env()
    feed(env.dp, env.bot, contact_update(1, 111, "+7 (999) 123-45-67"))
    reply = last(env.fake, SendMessage)
    assert "сохранён" in reply.text and isinstance(reply.reply_markup, ReplyKeyboardRemove)
    session.expire_all()
    assert session.scalars(select(Client)).one().phone == "+79991234567"


def test_foreign_contact_is_rejected(bot_env, session):
    env = bot_env()
    feed(env.dp, env.bot, contact_update(1, 111, "+7 999 000-00-00", contact_user_id=222))
    assert "собственный номер" in last(env.fake, SendMessage).text
    session.expire_all()
    assert session.scalars(select(Client)).all() == []


def test_skip_and_phone_command(bot_env):
    env = bot_env()
    feed(env.dp, env.bot, msg_update(1, 111, text="Пропустить"))
    assert isinstance(last(env.fake, SendMessage).reply_markup, ReplyKeyboardRemove)

    feed(env.dp, env.bot, command_update(2, 111, "/phone"))
    assert isinstance(last(env.fake, SendMessage).reply_markup, ReplyKeyboardMarkup)


def test_admin_notification_includes_known_phone(bot_env):
    env = bot_env()
    feed(env.dp, env.bot, contact_update(1, 111, "8 999 123-45-67"))
    feed(env.dp, env.bot, cb_update(2, 111, ok_cb(env, "1100")))
    admin_msgs = [c for c in calls_of(env.fake, SendMessage) if c.chat_id == ADMIN_ID]
    assert "+79991234567" in admin_msgs[-1].text
