"""Telegram-бот записи клиентов. Запуск: python -m app.bot"""

import asyncio
import html
import logging
from datetime import timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
)
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app import booking, db, keyboards as kb
from app.booking import BookingError
from app.config import settings
from app.models import ACTIVE, PENDING, Booking, Master, Service

log = logging.getLogger("booking-bot")
router = Router()


def esc(value: str) -> str:
    return html.escape(value or "")


def greeting() -> str:
    return (
        f"Здравствуйте! Это бот записи «{esc(settings.business_name)}».\n"
        "Выберите действие:"
    )


def prepay_for(price: int) -> int:
    """Сколько предоплаты нужно за услугу с такой ценой (0 — предоплата не требуется)."""
    if not settings.prepayment_enabled:
        return 0
    return booking.prepayment_for(price, settings.prepayment_percent)


def client_line(full_name: str, username: str, phone: str) -> str:
    line = f"Клиент: {esc(full_name)}"
    if username:
        line += f" (@{esc(username)})"
    if phone:
        line += f", тел. {esc(phone)}"
    return line


def invoice_payload(booking_id: int) -> str:
    return f"b:{booking_id}"


def booking_id_from_payload(payload: str) -> int | None:
    prefix, _, raw = (payload or "").partition(":")
    if prefix == "b" and raw.isdigit():
        return int(raw)
    return None


async def show(cb: CallbackQuery, text: str, markup, answer: bool = True) -> None:
    """Редактируем текущее сообщение; если текст не изменился — игнорируем ошибку.

    answer=False — не отвечать на callback (его уже закрыли всплывающим уведомлением)."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    if answer:
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


# ---------- телефон ----------

PHONE_PROMPT = "Поделитесь номером телефона, чтобы мастер мог с вами связаться:"


@router.message(Command("phone"))
async def cmd_phone(message: Message) -> None:
    await message.answer(PHONE_PROMPT, reply_markup=kb.phone_kb())


@router.message(F.contact)
async def on_contact(message: Message) -> None:
    contact = message.contact
    # Принимаем только собственный контакт, чужой номер в карточку клиента не пишем
    if contact.user_id != message.from_user.id:
        await message.answer(
            "Нужен ваш собственный номер — нажмите кнопку «Поделиться номером».",
            reply_markup=kb.phone_kb(),
        )
        return
    user = message.from_user
    try:
        with db.session_scope() as s:
            booking.set_client_phone(
                s, user.id, contact.phone_number, user.full_name, user.username or ""
            )
    except BookingError as e:
        await message.answer(str(e), reply_markup=ReplyKeyboardRemove())
        return
    await message.answer("Спасибо, номер сохранён ✅", reply_markup=ReplyKeyboardRemove())


@router.message(F.text == kb.SKIP_PHONE_TEXT)
async def on_skip_phone(message: Message) -> None:
    await message.answer(
        "Хорошо, без номера. Добавить его можно позже командой /phone.",
        reply_markup=ReplyKeyboardRemove(),
    )


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
        masters = booking.masters_for_service(s, service_id)
    if not masters:
        await show(cb, "Для этой услуги пока нет мастеров.", kb.main_menu_kb())
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
        prepay = prepay_for(service.price)
    if prepay:
        text += (
            f"\nПредоплата: <b>{prepay} ₽</b> — на оплату даётся "
            f"{settings.payment_hold_min} мин, затем время освободится."
        )
    await show(cb, text, kb.confirm_kb(service_id, master_id, start))


async def send_prepay_invoice(bot: Bot, chat_id: int, booking_id: int) -> bool:
    """Отправляет счёт на предоплату. False — если Telegram счёт не принял."""
    with db.session_scope() as s:
        b = s.get(
            Booking,
            booking_id,
            options=[joinedload(Booking.service), joinedload(Booking.master)],
        )
        amount = b.prepay_amount
        title = f"Предоплата: {b.service.name}"[:32]
        description = f"{b.service.name}, {b.master.name}, {b.start_at:%d.%m.%Y в %H:%M}"[:255]
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=invoice_payload(booking_id),
            provider_token=settings.payment_provider_token,
            currency=settings.payment_currency,
            prices=[LabeledPrice(label="Предоплата", amount=amount * 100)],  # в копейках
        )
    except TelegramAPIError as e:
        log.error("Не удалось выставить счёт по записи %s: %s", booking_id, e)
        return False
    return True


@router.callback_query(F.data.startswith("ok:"))
async def cb_confirm(cb: CallbackQuery, bot: Bot) -> None:
    _, service_id, master_id, ts = cb.data.split(":")
    service_id, master_id = int(service_id), int(master_id)
    start = kb.parse_ts(ts)
    user = cb.from_user
    try:
        with db.session_scope() as s:
            service = s.get(Service, service_id)
            prepay = prepay_for(service.price) if service else 0
            client = booking.get_or_create_client(
                s, user.id, user.full_name, user.username or ""
            )
            b = booking.create_booking(
                s, client, master_id, service_id, start, db.now_local(), prepay_amount=prepay
            )
            booking_id = b.id
            awaiting_payment = b.status == PENDING
            summary = booking.format_booking(b)
            first_booking = (
                s.scalar(select(func.count(Booking.id)).where(Booking.client_id == client.id)) == 1
            )
            needs_phone = first_booking and not client.phone
            admin_text = (
                f"🆕 Новая запись #{b.id}\n{esc(summary)}\n"
                + client_line(user.full_name, user.username or "", client.phone)
            )
    except BookingError as e:
        await cb.answer(str(e), show_alert=True)
        await _show_days(cb, service_id, master_id)
        return

    if awaiting_payment:
        # Время удержано, подтверждение и уведомление админам — после оплаты
        if not await send_prepay_invoice(bot, user.id, booking_id):
            with db.session_scope() as s:
                try:
                    booking.cancel_booking(s, booking_id)
                except BookingError:
                    pass
            await notify_admins(
                bot,
                f"⚠️ Не удалось выставить счёт на предоплату по записи #{booking_id}. "
                "Проверьте PAYMENT_PROVIDER_TOKEN и сумму предоплаты.",
            )
            await show(
                cb,
                "Не получилось выставить счёт на предоплату, запись не создана. "
                "Попробуйте чуть позже.",
                kb.main_menu_kb(),
            )
            return
        await show(
            cb,
            f"⏳ Время зарезервировано на {settings.payment_hold_min} мин.\n\n{esc(summary)}\n\n"
            f"Оплатите предоплату <b>{prepay} ₽</b> по счёту ниже, и запись будет подтверждена.",
            kb.pay_kb(booking_id),
        )
        return

    await show(
        cb,
        f"✅ Вы записаны!\n\n{esc(summary)}\n\nЗа пару часов до визита пришлю напоминание.",
        kb.main_menu_kb(),
    )
    if needs_phone:
        await bot.send_message(user.id, PHONE_PROMPT, reply_markup=kb.phone_kb())
    await notify_admins(bot, admin_text)


# ---------- предоплата ----------


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery, bot: Bot) -> None:
    booking_id = int(cb.data.split(":")[1])
    with db.session_scope() as s:
        b = s.get(Booking, booking_id, options=[joinedload(Booking.client)])
        valid = b is not None and b.client.tg_id == cb.from_user.id and b.status == PENDING
    if not valid:
        await cb.answer("Эта запись уже оплачена или отменена.", show_alert=True)
        return
    if await send_prepay_invoice(bot, cb.from_user.id, booking_id):
        await cb.answer("Счёт отправлен ниже.")
    else:
        await cb.answer("Не удалось выставить счёт. Попробуйте позже.", show_alert=True)


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Telegram спрашивает, можно ли принимать платёж: отвечаем за 10 секунд, поэтому только проверки."""
    booking_id = booking_id_from_payload(query.invoice_payload)
    error = None
    with db.session_scope() as s:
        b = (
            s.get(Booking, booking_id, options=[joinedload(Booking.client)])
            if booking_id is not None
            else None
        )
        if b is None or b.client.tg_id != query.from_user.id:
            error = "Запись не найдена."
        elif b.status != PENDING or b.created_at <= db.now_local() - timedelta(
            minutes=settings.payment_hold_min
        ):
            error = "Время брони истекло или запись отменена. Запишитесь заново."
        elif (
            query.total_amount != b.prepay_amount * 100
            or query.currency != settings.payment_currency
        ):
            error = "Сумма счёта не совпадает с записью. Запишитесь заново."
    if error:
        await query.answer(ok=False, error_message=error)
    else:
        await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    user = message.from_user
    amount = sp.total_amount // 100
    charge = sp.provider_payment_charge_id or sp.telegram_payment_charge_id
    booking_id = booking_id_from_payload(sp.invoice_payload)
    log.warning(
        "Получена оплата: запись=%s сумма=%s %s платёж=%s", booking_id, amount, sp.currency, charge
    )

    b = None
    outcome = "unknown"
    if booking_id is not None:
        with db.session_scope() as s:
            b, outcome = booking.confirm_payment(s, booking_id, amount, charge, db.now_local())
            if b is not None:
                summary = esc(booking.format_booking(b))
                who = client_line(b.client.name, b.client.username, b.client.phone)

    if outcome in ("confirmed", "reactivated"):
        await message.answer(
            f"✅ Предоплата {amount} ₽ получена, вы записаны!\n\n{summary}\n\n"
            "За пару часов до визита пришлю напоминание.",
            reply_markup=kb.main_menu_kb(),
        )
        note = " (бронь успела истечь, но время было свободно — запись восстановлена)" if outcome == "reactivated" else ""
        await notify_admins(
            bot, f"💳 Предоплата {amount} ₽ получена. Новая запись #{booking_id}{note}\n{summary}\n{who}"
        )
    elif outcome == "duplicate":
        log.info("Повторное уведомление об оплате записи %s, пропускаю", booking_id)
    elif outcome == "refund_needed":
        await message.answer(
            "Оплата получена, но время уже занято, а бронь была снята. "
            "Администратор свяжется с вами и вернёт предоплату."
        )
        await notify_admins(
            bot,
            f"⚠️ Нужен возврат {amount} ₽: оплата по записи #{booking_id} пришла после снятия брони, "
            f"время недоступно.\nПлатёж: {esc(charge)}\n{summary}\n{who}",
        )
    else:
        await message.answer(
            "Оплата получена, но запись не найдена. Администратор свяжется с вами."
        )
        await notify_admins(
            bot,
            f"⚠️ Оплата {amount} ₽ без записи (payload {esc(sp.invoice_payload)}). "
            f"Платёж: {esc(charge)}. Клиент: {esc(user.full_name)}",
        )


# ---------- мои записи ----------


def _my_view(items):
    if not items:
        return "У вас нет предстоящих записей.", kb.main_menu_kb()
    lines = []
    for i, b in enumerate(items, 1):
        mark = f"\n⏳ ждёт предоплаты {b.prepay_amount} ₽" if b.status == PENDING else ""
        lines.append(f"{i}. {esc(booking.format_booking(b))}{mark}")
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
            paid = b.paid_amount
            admin_text = (
                f"❌ Отмена записи #{b.id}\n{esc(booking.format_booking(b))}\n"
                f"Клиент: {esc(cb.from_user.full_name)}"
            )
            if paid:
                admin_text += (
                    f"\n⚠️ Клиент внёс предоплату {paid} ₽ (платёж {esc(b.payment_charge_id)}) — "
                    "вопрос возврата решите с клиентом."
                )
    except BookingError as e:
        await cb.answer(str(e), show_alert=True)
    else:
        await notify_admins(bot, admin_text)
        await cb.answer(
            "Запись отменена. Вопрос с предоплатой решит администратор." if paid else "Запись отменена."
        )
    with db.session_scope() as s:
        items = booking.client_bookings(s, cb.from_user.id, db.now_local())
        text, markup = _my_view(items)
    await show(cb, text, markup)


# ---------- перенос записи ----------


def _own_active_booking(s, booking_id: int, tg_id: int) -> Booking | None:
    b = s.get(
        Booking,
        booking_id,
        options=[
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        ],
    )
    if b is None or b.client.tg_id != tg_id or b.status != ACTIVE:
        return None
    return b


async def _show_reschedule_days(cb: CallbackQuery, booking_id: int, answer: bool = True) -> None:
    now = db.now_local()
    with db.session_scope() as s:
        b = _own_active_booking(s, booking_id, cb.from_user.id)
        if b is not None:
            header = esc(booking.format_booking(b))
            too_late = b.start_at - now < timedelta(minutes=settings.reschedule_before_min)
            days = (
                []
                if too_late
                else booking.available_days(
                    s, b.master_id, b.service_id, now, exclude_booking_id=b.id
                )
            )
    if b is None:
        await cb.answer("Эту запись уже нельзя перенести.", show_alert=True)
        return
    if too_late:
        await cb.answer(
            f"Перенос возможен не позднее чем за {settings.reschedule_before_min} мин до визита. "
            "Свяжитесь с администратором.",
            show_alert=True,
        )
        return
    if not days:
        await show(
            cb,
            f"Для переноса пока нет свободных дат.\n\n{header}",
            kb.main_menu_kb(),
            answer=answer,
        )
        return
    await show(
        cb,
        f"Перенос записи:\n\n{header}\n\nВыберите новую дату:",
        kb.reschedule_days_kb(booking_id, days),
        answer=answer,
    )


@router.callback_query(F.data.startswith("rs:"))
async def cb_reschedule(cb: CallbackQuery) -> None:
    await _show_reschedule_days(cb, int(cb.data.split(":")[1]))


@router.callback_query(F.data.startswith("rd:"))
async def cb_reschedule_day(cb: CallbackQuery) -> None:
    _, booking_id, day = cb.data.split(":")
    booking_id = int(booking_id)
    with db.session_scope() as s:
        b = _own_active_booking(s, booking_id, cb.from_user.id)
        slots = (
            booking.get_free_slots(
                s, b.master_id, b.service_id, kb.parse_ds(day), db.now_local(),
                exclude_booking_id=b.id,
            )
            if b is not None
            else []
        )
    if b is None:
        await cb.answer("Эту запись уже нельзя перенести.", show_alert=True)
        return
    if not slots:
        await cb.answer("На эту дату свободного времени уже нет.", show_alert=True)
        await _show_reschedule_days(cb, booking_id, answer=False)
        return
    await show(
        cb,
        f"Выберите новое время на <b>{kb.parse_ds(day):%d.%m.%Y}</b>:",
        kb.reschedule_times_kb(booking_id, slots),
    )


@router.callback_query(F.data.startswith("rt:"))
async def cb_reschedule_time(cb: CallbackQuery) -> None:
    _, booking_id, ts = cb.data.split(":")
    booking_id, start = int(booking_id), kb.parse_ts(ts)
    with db.session_scope() as s:
        b = _own_active_booking(s, booking_id, cb.from_user.id)
        header = esc(booking.format_booking(b)) if b is not None else ""
    if b is None:
        await cb.answer("Эту запись уже нельзя перенести.", show_alert=True)
        return
    await show(
        cb,
        f"Перенести запись:\n\n{header}\n\nна <b>{start:%d.%m.%Y}</b> в <b>{start:%H:%M}</b>?",
        kb.reschedule_confirm_kb(booking_id, start),
    )


@router.callback_query(F.data.startswith("rok:"))
async def cb_reschedule_confirm(cb: CallbackQuery, bot: Bot) -> None:
    _, booking_id, ts = cb.data.split(":")
    booking_id, start = int(booking_id), kb.parse_ts(ts)
    user = cb.from_user
    try:
        with db.session_scope() as s:
            b, old_start = booking.reschedule_booking(
                s,
                booking_id,
                start,
                db.now_local(),
                tg_id=user.id,
                min_before_min=settings.reschedule_before_min,
            )
            summary = booking.format_booking(b)
            admin_text = (
                f"🔄 Перенос записи #{b.id}\n"
                f"было: {old_start:%d.%m.%Y %H:%M}\nстало: {start:%d.%m.%Y %H:%M}\n"
                f"{esc(b.service.name)} — {esc(b.master.name)}\n"
                + client_line(user.full_name, user.username or "", b.client.phone)
            )
    except BookingError as e:
        await cb.answer(str(e), show_alert=True)
        await _show_reschedule_days(cb, booking_id, answer=False)
        return
    await show(
        cb,
        f"✅ Запись перенесена:\n\n{esc(summary)}\n\nНапоминание придёт ближе к новому времени.",
        kb.main_menu_kb(),
    )
    await notify_admins(bot, admin_text)


# ---------- оценка визита ----------


@router.callback_query(F.data.startswith("rate:"))
async def cb_rate(cb: CallbackQuery, bot: Bot) -> None:
    _, booking_id, value = cb.data.split(":")
    user = cb.from_user
    try:
        with db.session_scope() as s:
            b = booking.set_rating(s, int(booking_id), user.id, int(value))
            rating = b.rating
            admin_text = (
                f"⚠️ Низкая оценка {rating}/5 по записи #{b.id}\n{esc(booking.format_booking(b))}\n"
                + client_line(user.full_name, user.username or "", b.client.phone)
            )
    except BookingError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await show(cb, "Спасибо за оценку! Будем рады видеть вас снова 💛", kb.main_menu_kb())
    if rating <= 3:
        await notify_admins(bot, admin_text)


# ---------- фоновые задачи ----------


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


async def expire_unpaid_bookings(bot: Bot) -> int:
    """Снимает брони, за которые не внесли предоплату вовремя, и освобождает время."""
    notes: list[tuple[int, str]] = []
    with db.session_scope() as s:
        for b in booking.expire_unpaid(s, db.now_local(), settings.payment_hold_min):
            notes.append((b.client.tg_id, esc(booking.format_booking(b))))
    for tg_id, summary in notes:
        try:
            await bot.send_message(
                tg_id,
                f"⌛ Предоплата не поступила вовремя, бронь снята:\n\n{summary}\n\n"
                "Чтобы записаться снова, нажмите /start.",
            )
        except TelegramAPIError as e:
            log.warning("Не удалось сообщить %s о снятии брони: %s", tg_id, e)
    return len(notes)


async def send_review_requests(bot: Bot) -> int:
    """После отметки «визит состоялся» просит клиента оценить визит. Возвращает число отправленных."""
    sent = 0
    with db.session_scope() as s:
        for b in booking.due_review_requests(s, db.now_local()):
            try:
                await bot.send_message(
                    b.client.tg_id,
                    f"Спасибо, что были у нас!\n\n{esc(booking.format_booking(b))}\n\n"
                    "Как всё прошло? Оцените визит:",
                    reply_markup=kb.rating_kb(b.id),
                )
                sent += 1
            except TelegramAPIError as e:
                log.warning("Просьба об оценке для %s не доставлена: %s", b.client.tg_id, e)
            b.review_sent = True  # одну просьбу на визит, даже если клиент заблокировал бота
    return sent


async def reminder_loop(bot: Bot, interval: int = 60) -> None:
    """Раз в минуту: напоминания о визитах, снятие неоплаченных броней, просьбы об оценке."""
    while True:
        for job, what in (
            (send_due_reminders, "напоминаний"),
            (expire_unpaid_bookings, "снятия неоплаченных броней"),
            (send_review_requests, "запросов оценки"),
        ):
            try:
                await job(bot)
            except Exception:
                log.exception("Ошибка в цикле %s", what)
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
