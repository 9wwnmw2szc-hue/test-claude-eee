"""Inline-клавиатуры. Состояние записи кодируется в callback_data (лимит Telegram — 64 байта),
поэтому боту не нужны FSM и хранилище состояний."""

from datetime import date, datetime

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.models import PENDING, WEEKDAYS, Booking, Master, Service

TS = "%Y%m%d%H%M"
DS = "%Y%m%d"

SKIP_PHONE_TEXT = "Пропустить"


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS)


def parse_ds(value: str) -> date:
    return datetime.strptime(value, DS).date()


def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📅 Записаться", callback_data="book")
    b.button(text="📋 Мои записи", callback_data="my")
    b.adjust(1)
    return b.as_markup()


def services_kb(services: list[Service]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in services:
        price = f" — {s.price} ₽" if s.price else ""
        b.button(text=f"{s.name} ({s.duration_min} мин){price}", callback_data=f"s:{s.id}")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def masters_kb(service_id: int, masters: list[Master]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in masters:
        b.button(text=m.name, callback_data=f"m:{service_id}:{m.id}")
    b.button(text="⬅️ Назад", callback_data="book")
    b.adjust(1)
    return b.as_markup()


def days_kb(service_id: int, master_id: int, days: list[date]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for d in days:
        b.button(
            text=f"{WEEKDAYS[d.weekday()]} {d:%d.%m}",
            callback_data=f"d:{service_id}:{master_id}:{d:{DS}}",
        )
    b.adjust(3)
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"s:{service_id}"))
    return b.as_markup()


def times_kb(
    service_id: int, master_id: int, slots: list[datetime]
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in slots:
        b.button(text=f"{t:%H:%M}", callback_data=f"t:{service_id}:{master_id}:{t:{TS}}")
    b.adjust(4)
    b.row(
        InlineKeyboardButton(
            text="⬅️ Другой день", callback_data=f"m:{service_id}:{master_id}"
        )
    )
    return b.as_markup()


def confirm_kb(service_id: int, master_id: int, start: datetime) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить", callback_data=f"ok:{service_id}:{master_id}:{start:{TS}}")
    b.button(text="⬅️ Другое время", callback_data=f"d:{service_id}:{master_id}:{start:{DS}}")
    b.adjust(1)
    return b.as_markup()


def my_bookings_kb(bookings: list[Booking]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bk in bookings:
        when = f"{bk.start_at:%d.%m %H:%M}"
        if bk.status == PENDING:
            b.button(text=f"💳 Оплатить {when}", callback_data=f"pay:{bk.id}")
        b.button(text=f"❌ Отменить {when}", callback_data=f"x:{bk.id}")
    b.button(text="📅 Записаться ещё", callback_data="book")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def pay_kb(booking_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Оплатить", callback_data=f"pay:{booking_id}")
    b.button(text="❌ Отменить запись", callback_data=f"x:{booking_id}")
    b.adjust(1)
    return b.as_markup()


def phone_kb() -> ReplyKeyboardMarkup:
    """Кнопка «поделиться контактом» отправляет номер самого пользователя, вводить вручную не нужно."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text=SKIP_PHONE_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
