"""Telegram-бот записи клиентов. Запуск: python -m app.bot"""

import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app import booking, db, keyboards as kb
from app.booking import BookingError
from app.config import settings
from app.models import Master, Service

log = logging.getLogger("booking-bot")
router = Router()


def esc(value: str) -> str:
    return html.escape(value or "")


def greeting() -> str:
    return (
        f"Здравствуйте! Это бот записи «{esc(settings.business_name)}».\n"
        "Выберите действие:"
    )


async def show(cb: CallbackQuery, text: str, markup) -> None:
    """Редактируем текущее сообщение; если текст не изменился — игнорируем ошибку."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await cb.answer()


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError as e:
            log.warning("Не удалось уведомить админа %s: %s", admin_id, e)


# ---------- меню ----------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(greeting(), reply_markup=kb.main_menu_kb())


@router.message(Command("my"))
async def cmd_my(message: Message) -> None:
    with db.session_scope() as s:
        items = booking.client_bookings(s, message.from_user.id, db.now_local())
        text, markup = _my_view(items)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery) -> None:
    await show(cb, greeting(), kb.main_menu_kb())


# ---------- запись ----------


@router.callback_query(F.data == "book")
async def cb_book(cb: CallbackQuery) -> None:
    with db.session_scope() as s:
        services = list(
            s.scalars(select(Service).where(Service.active).order_by(Service.id))
        )
    if not services:
        await show(cb, "Пока нет доступных услуг.", kb.main_menu_kb())
        return
    await show(cb, "Выберите услугу:", kb.services_kb(services))


async def _show_days(cb: CallbackQuery, service_id: int, master_id: int) -> None:
    with db.session_scope() as s:
        days = booking.available_days(s, master_id, service_id, db.now_local())
        master = s.get(Master, master_id)
        master_name = master.name if master else ""
    if not days:
        await show(
            cb,
            "У этого мастера нет свободных дат в ближайшее время.",
            kb.main_menu_kb(),
        )
        return
    await show(
        cb,
        f"Мастер: <b>{esc(master_name)}</b>\nВыберите дату:",
        kb.days_kb(service_id, master_id, days),
    )


@router.callback_query(F.data.startswith("s:"))
async def cb_service(cb: CallbackQuery) -> None:
    service_id = int(cb.data.split(":")[1])
    with db.session_scope() as s:
        masters = list(
            s.scalars(select(Master).where(Master.active).order_by(Master.id))
        )
    if not masters:
        await show(cb, "Пока нет доступных мастеров.", kb.main_menu_kb())
        return
    if len(masters) == 1:
        await _show_days(cb, service_id, masters[0].id)
        return
    await show(cb, "Выберите мастера:", kb.masters_kb(service_id, masters))


@router.callback_query(F.data.startswith("m:"))
async def cb_master(cb: CallbackQuery) -> None:
    _, service_id, master_id = cb.data.split(":")
    await _show_days(cb, int(service_id), int(master_id))


@router.callback_query(F.data.startswith("d:"))
async def cb_day(cb: CallbackQuery) -> None:
    _, service_id, master_id, day = cb.data.split(":")
    service_id, master_id = int(service_id), int(master_id)
    with db.session_scope() as s:
        slots = booking.get_free_slots(
            s, master_id, service_id, kb.parse_ds(day), db.now_local()
        )
    if not slots:
        await cb.answer("На эту дату свободного времени уже нет.", show_alert=True)
        await _show_days(cb, service_id, master_id)
        return
    await show(
        cb,
        f"Выберите время на <b>{kb.parse_ds(day):%d.%m.%Y}</b>:",
        kb.times_kb(service_id, master_id, slots),
    )


@router.callback_query(F.data.startswith("t:"))
async def cb_time(cb: CallbackQuery) -> None:
    _, service_id, master_id, ts = cb.data.split(":")
    service_id, master_id = int(service_id), int(master_id)
    start = kb.parse_ts(ts)
    with db.session_scope() as s:
        service = s.get(Service, service_id)
        master = s.get(Master, master_id)
        text = (
            "Проверьте запись:\n\n"
            f"Услуга: <b>{esc(service.name)}</b>\n"
            f"Мастер: <b>{esc(master.name)}</b>\n"
            f"Дата: <b>{start:%d.%m.%Y}</b> в <b>{start:%H:%M}</b>\n"
            f"Стоимость: <b>{service.price} ₽</b>"
        )
    await show(cb, text, kb.confirm_kb(service_id, master_id, start))


@router.callback_query(F.data.startswith("ok:"))
async def cb_confirm(cb: CallbackQuery, bot: Bot) -> None:
    _, service_id, master_id, ts = cb.data.split(":")
    service_id, master_id = int(service_id), int(master_id)
    start = kb.parse_ts(ts)
    user = cb.from_user
    try:
        with db.session_scope() as s:
            client = booking.get_or_create_client(
                s, user.id, user.full_name, user.username or ""
            )
            b = booking.create_booking(
                s, client, master_id, service_id, start, db.now_local()
            )
            summary = booking.format_booking(b)
            admin_text = (
                f"🆕 Новая запись #{b.id}\n{summary}\n"
                f"Клиент: {user.full_name}"
                + (f" (@{user.username})" if user.username else "")
            )
    except BookingError as e:
        await cb.answer(str(e), show_alert=True)
        await _show_days(cb, service_id, master_id)
        return
    await show(
        cb,
        f"✅ Вы записаны!\n\n{esc(summary)}\n\nЗа пару часов до визита пришлю напоминание.",
        kb.main_menu_kb(),
    )
    await notify_admins(bot, admin_text)


# ---------- мои записи ----------


def _my_view(items):
    if not items:
        return "У вас нет предстоящих записей.", kb.main_menu_kb()
    lines = [f"{i}. {esc(booking.format_booking(b))}" for i, b in enumerate(items, 1)]
    return "Ваши записи:\n\n" + "\n\n".join(lines), kb.my_bookings_kb(items)


@router.callback_query(F.data == "my")
async def cb_my(cb: CallbackQuery) -> None:
    with db.session_scope() as s:
        items = booking.client_bookings(s, cb.from_user.id, db.now_local())
        text, markup = _my_view(items)
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("x:"))
async def cb_cancel(cb: CallbackQuery, bot: Bot) -> None:
    booking_id = int(cb.data.split(":")[1])
    try:
        with db.session_scope() as s:
            b = booking.cancel_booking(s, booking_id, tg_id=cb.from_user.id)
            admin_text = (
                f"❌ Отмена записи #{b.id}\n{booking.format_booking(b)}\n"
                f"Клиент: {cb.from_user.full_name}"
            )
    except BookingError as e:
        await cb.answer(str(e), show_alert=True)
    else:
        await notify_admins(bot, admin_text)
        await cb.answer("Запись отменена.")
    with db.session_scope() as s:
        items = booking.client_bookings(s, cb.from_user.id, db.now_local())
        text, markup = _my_view(items)
    await show(cb, text, markup)


# ---------- напоминания ----------


async def send_due_reminders(bot: Bot) -> int:
    """Отправляет напоминания, возвращает число отправленных."""
    sent = 0
    with db.session_scope() as s:
        due = booking.due_reminders(s, db.now_local())
        for b in due:
            try:
                await bot.send_message(
                    b.client.tg_id,
                    f"⏰ Напоминание о записи:\n\n{esc(booking.format_booking(b))}",
                )
                sent += 1
            except TelegramAPIError as e:
                log.warning("Напоминание для %s не доставлено: %s", b.client.tg_id, e)
            b.reminder_sent = True  # не пытаемся повторно, даже если клиент заблокировал бота
    return sent


async def reminder_loop(bot: Bot, interval: int = 60) -> None:
    while True:
        try:
            await send_due_reminders(bot)
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(interval)


# ---------- запуск ----------


_dispatcher: Dispatcher | None = None


def build_dispatcher() -> Dispatcher:
    """Один диспетчер на процесс: роутер aiogram можно подключить только один раз."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
        _dispatcher.include_router(router)
    return _dispatcher


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not settings.bot_token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и заполните.")
    db.init_db()
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    task = asyncio.create_task(reminder_loop(bot))
    try:
        await build_dispatcher().start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
