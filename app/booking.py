"""Бизнес-логика записи. Не зависит от Telegram и от веб-части."""

import math
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings
from app.models import (
    ACTIVE,
    BLOCKING,
    CANCELLED,
    DONE,
    NO_SHOW,
    OCCUPYING,
    PENDING,
    Booking,
    Client,
    Master,
    Service,
    WorkingHours,
)


class BookingError(Exception):
    """Ошибка бизнес-логики, текст можно показывать клиенту."""


# ---------- клиенты ----------


def get_or_create_client(
    session: Session, tg_id: int, name: str = "", username: str = ""
) -> Client:
    client = session.scalar(select(Client).where(Client.tg_id == tg_id))
    if client is None:
        client = Client(tg_id=tg_id, name=name, username=username or "")
        session.add(client)
        session.flush()
    else:
        if name and client.name != name:
            client.name = name
        if username and client.username != username:
            client.username = username
    return client


def normalize_phone(raw: str) -> str:
    """Оставляет «+» и цифры. Бросает BookingError, если на номер не похоже."""
    digits = re.sub(r"\D", "", raw or "")
    if not 10 <= len(digits) <= 15:
        raise BookingError("Не удалось распознать номер телефона.")
    if len(digits) == 11 and digits.startswith("8"):  # 8 999… → +7 999… (российский формат)
        digits = "7" + digits[1:]
    return "+" + digits


def set_client_phone(
    session: Session, tg_id: int, raw_phone: str, name: str = "", username: str = ""
) -> Client:
    phone = normalize_phone(raw_phone)
    client = get_or_create_client(session, tg_id, name, username)
    client.phone = phone
    session.flush()
    return client


# ---------- мастера и услуги ----------


def offers_service(master: Master, service_id: int) -> bool:
    """Мастер выполняет услугу, если она отмечена у него или если не отмечено ничего (тогда — все)."""
    return not master.services or any(s.id == service_id for s in master.services)


def masters_for_service(session: Session, service_id: int) -> list[Master]:
    masters = session.scalars(
        select(Master)
        .where(Master.active)
        .options(selectinload(Master.services))
        .order_by(Master.id)
    ).all()
    return [m for m in masters if offers_service(m, service_id)]


# ---------- слоты ----------


def _overlaps(
    session: Session,
    master_id: int,
    start: datetime,
    end: datetime,
    exclude_booking_id: int | None = None,
) -> bool:
    stmt = select(Booking.id).where(
        Booking.master_id == master_id,
        Booking.status.in_(OCCUPYING),
        Booking.start_at < end,
        Booking.end_at > start,
    )
    if exclude_booking_id is not None:
        stmt = stmt.where(Booking.id != exclude_booking_id)
    return session.scalar(stmt) is not None


def get_free_slots(
    session: Session,
    master_id: int,
    service_id: int,
    day: date,
    now: datetime,
    step_min: int | None = None,
    min_lead_min: int | None = None,
    exclude_booking_id: int | None = None,
) -> list[datetime]:
    """Свободные времена начала для услуги у мастера в указанный день.

    exclude_booking_id — не считать занятым время этой записи (нужно при её переносе,
    иначе нельзя сдвинуть визит на соседнее время)."""
    step = timedelta(minutes=step_min or settings.slot_step_min)
    lead = timedelta(
        minutes=settings.min_lead_min if min_lead_min is None else min_lead_min
    )
    service = session.get(Service, service_id)
    master = session.get(Master, master_id)
    if service is None or master is None or not service.active or not master.active:
        return []
    if not offers_service(master, service_id):
        return []
    duration = timedelta(minutes=service.duration_min)

    windows = session.scalars(
        select(WorkingHours).where(
            WorkingHours.master_id == master_id,
            WorkingHours.weekday == day.weekday(),
        )
    ).all()

    busy_stmt = select(Booking.start_at, Booking.end_at).where(
        Booking.master_id == master_id,
        Booking.status.in_(OCCUPYING),
        Booking.start_at < datetime.combine(day, datetime.max.time()),
        Booking.end_at > datetime.combine(day, datetime.min.time()),
    )
    if exclude_booking_id is not None:
        busy_stmt = busy_stmt.where(Booking.id != exclude_booking_id)
    busy = session.execute(busy_stmt).all()

    slots: list[datetime] = []
    for w in windows:
        cur = datetime.combine(day, w.start)
        window_end = datetime.combine(day, w.end)
        while cur + duration <= window_end:
            end = cur + duration
            if cur >= now + lead and not any(
                b_start < end and b_end > cur for b_start, b_end in busy
            ):
                slots.append(cur)
            cur += step
    return sorted(set(slots))


def available_days(
    session: Session,
    master_id: int,
    service_id: int,
    now: datetime,
    exclude_booking_id: int | None = None,
) -> list[date]:
    """Даты в горизонте записи, где у мастера есть хотя бы один свободный слот."""
    days = []
    for i in range(settings.booking_days_ahead + 1):
        d = now.date() + timedelta(days=i)
        if get_free_slots(
            session, master_id, service_id, d, now, exclude_booking_id=exclude_booking_id
        ):
            days.append(d)
    return days


# ---------- записи ----------


def prepayment_for(price: int, percent: int) -> int:
    """Сумма предоплаты в рублях, округлённая вверх до рубля."""
    if price <= 0 or percent <= 0:
        return 0
    return min(price, math.ceil(price * percent / 100))


def create_booking(
    session: Session,
    client: Client,
    master_id: int,
    service_id: int,
    start_at: datetime,
    now: datetime,
    prepay_amount: int = 0,
) -> Booking:
    """Создаёт запись. При prepay_amount > 0 время удерживается (PENDING) до оплаты."""
    service = session.get(Service, service_id)
    master = session.get(Master, master_id)
    if service is None or master is None:
        raise BookingError("Услуга или мастер не найдены.")
    if not offers_service(master, service_id):
        raise BookingError("Этот мастер не выполняет выбранную услугу.")
    free = get_free_slots(session, master_id, service_id, start_at.date(), now)
    if start_at not in free:
        raise BookingError("К сожалению, это время уже занято. Выберите другое.")
    end_at = start_at + timedelta(minutes=service.duration_min)
    if _overlaps(session, master_id, start_at, end_at):
        raise BookingError("К сожалению, это время уже занято. Выберите другое.")
    booking = Booking(
        client_id=client.id,
        master_id=master_id,
        service_id=service_id,
        start_at=start_at,
        end_at=end_at,
        status=PENDING if prepay_amount > 0 else ACTIVE,
        prepay_amount=max(prepay_amount, 0),
        price=service.price,
        created_at=now,
    )
    session.add(booking)
    session.flush()
    return booking


def cancel_booking(
    session: Session, booking_id: int, tg_id: int | None = None
) -> Booking:
    """Отмена записи. Если передан tg_id, отменить можно только свою запись."""
    booking = session.get(
        Booking, booking_id, options=[joinedload(Booking.client)]
    )
    if booking is None:
        raise BookingError("Запись не найдена.")
    if tg_id is not None and booking.client.tg_id != tg_id:
        raise BookingError("Это не ваша запись.")
    if booking.status not in BLOCKING:
        raise BookingError("Запись уже отменена.")
    booking.status = CANCELLED
    session.flush()
    return booking


def reschedule_booking(
    session: Session,
    booking_id: int,
    new_start: datetime,
    now: datetime,
    tg_id: int | None = None,
    min_before_min: int = 0,
) -> tuple[Booking, datetime]:
    """Переносит подтверждённую запись на другое время у того же мастера.

    Возвращает (запись, прежнее время начала). Если передан tg_id, переносить можно только свою
    запись. Перенос возможен не позднее чем за min_before_min минут до визита."""
    booking = session.get(
        Booking,
        booking_id,
        options=[
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        ],
    )
    if booking is None:
        raise BookingError("Запись не найдена.")
    if tg_id is not None and booking.client.tg_id != tg_id:
        raise BookingError("Это не ваша запись.")
    if booking.status != ACTIVE:
        raise BookingError("Перенести можно только подтверждённую запись.")
    if booking.start_at - now < timedelta(minutes=min_before_min):
        raise BookingError(
            f"Перенос возможен не позднее чем за {min_before_min} мин до визита. "
            "Свяжитесь с администратором."
        )
    free = get_free_slots(
        session,
        booking.master_id,
        booking.service_id,
        new_start.date(),
        now,
        exclude_booking_id=booking.id,
    )
    if new_start not in free:
        raise BookingError("К сожалению, это время уже занято. Выберите другое.")
    old_start = booking.start_at
    booking.start_at = new_start
    booking.end_at = new_start + timedelta(minutes=booking.service.duration_min)
    booking.reminder_sent = False  # напоминание должно прийти уже про новое время
    session.flush()
    return booking, old_start


# ---------- итог визита и оценка ----------


def _close_visit(session: Session, booking_id: int, now: datetime, status: str) -> Booking:
    booking = session.get(
        Booking,
        booking_id,
        options=[
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        ],
    )
    if booking is None:
        raise BookingError("Запись не найдена.")
    if booking.status != ACTIVE:
        raise BookingError("Отметить можно только подтверждённую запись.")
    if booking.start_at > now:
        raise BookingError("Визит ещё не начался.")
    booking.status = status
    session.flush()
    return booking


def mark_done(session: Session, booking_id: int, now: datetime) -> Booking:
    """Визит состоялся: идёт в выручку, клиенту уйдёт просьба оценить визит."""
    return _close_visit(session, booking_id, now, DONE)


def mark_no_show(session: Session, booking_id: int, now: datetime) -> Booking:
    """Клиент не пришёл: время освобождено, в выручку не входит."""
    return _close_visit(session, booking_id, now, NO_SHOW)


def auto_close_visits(session: Session, now: datetime, after_hours: int) -> list[Booking]:
    """Неотмеченные подтверждённые визиты, закончившиеся более after_hours часов назад, считаем состоявшимися.

    Не пришедшего клиента система угадать не может, поэтому «не пришёл» остаётся ручной отметкой
    администратора; автоотметка лишь не даёт забытым визитам выпасть из выручки и оценок.
    """
    if after_hours <= 0:
        return []
    stmt = select(Booking).where(
        Booking.status == ACTIVE, Booking.end_at <= now - timedelta(hours=after_hours)
    )
    closed = list(session.scalars(stmt))
    for b in closed:
        b.status = DONE
    session.flush()
    return closed


def due_review_requests(
    session: Session, now: datetime, within_days: int = 3
) -> list[Booking]:
    """Состоявшиеся визиты, по которым ещё не просили оценку (не старше within_days дней)."""
    stmt = (
        select(Booking)
        .where(
            Booking.status == DONE,
            Booking.review_sent.is_(False),
            Booking.rating == 0,
            Booking.end_at >= now - timedelta(days=within_days),
        )
        .options(
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        )
    )
    return list(session.scalars(stmt).unique())


def set_rating(session: Session, booking_id: int, tg_id: int, rating: int) -> Booking:
    if not 1 <= rating <= 5:
        raise BookingError("Оценка должна быть от 1 до 5.")
    booking = session.get(
        Booking,
        booking_id,
        options=[
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        ],
    )
    if booking is None or booking.client.tg_id != tg_id or booking.status != DONE:
        raise BookingError("Оценить можно только свой состоявшийся визит.")
    if booking.rating:
        raise BookingError("Спасибо, вы уже оценили этот визит.")
    booking.rating = rating
    session.flush()
    return booking


def bookings_to_close(session: Session, now: datetime, limit: int = 200) -> list[Booking]:
    """Подтверждённые записи, время которых уже наступило: администратор отмечает итог."""
    stmt = (
        select(Booking)
        .where(Booking.status == ACTIVE, Booking.start_at <= now)
        .options(
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        )
        .order_by(Booking.start_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


def recently_closed(
    session: Session, now: datetime, days: int = 14, limit: int = 200
) -> list[Booking]:
    stmt = (
        select(Booking)
        .where(
            Booking.status.in_((DONE, NO_SHOW)),
            Booking.start_at >= now - timedelta(days=days),
        )
        .options(
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        )
        .order_by(Booking.start_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


# ---------- предоплата ----------


def confirm_payment(
    session: Session, booking_id: int, amount: int, charge_id: str, now: datetime
) -> tuple[Booking | None, str]:
    """Фиксирует поступившую предоплату. Возвращает (запись, исход):

    confirmed     — запись ждала оплаты и подтверждена;
    duplicate     — запись уже подтверждена (повторное уведомление об оплате);
    reactivated   — бронь успела истечь, но время ещё свободно — запись восстановлена;
    refund_needed — бронь истекла и время уже недоступно: деньги получены, нужен возврат;
    unknown       — такой записи нет.
    """
    booking = session.get(
        Booking,
        booking_id,
        options=[
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        ],
    )
    if booking is None:
        return None, "unknown"
    if booking.status == ACTIVE:
        return booking, "duplicate"

    booking.paid_amount = amount
    booking.payment_charge_id = charge_id
    if booking.status == PENDING:
        booking.status = ACTIVE
        outcome = "confirmed"
    elif booking.start_at > now and not _overlaps(
        session, booking.master_id, booking.start_at, booking.end_at
    ):
        booking.status = ACTIVE
        outcome = "reactivated"
    else:
        outcome = "refund_needed"
    session.flush()
    return booking, outcome


def expire_unpaid(session: Session, now: datetime, hold_min: int) -> list[Booking]:
    """Отменяет брони, которые не оплатили за hold_min минут, и освобождает время."""
    stmt = (
        select(Booking)
        .where(
            Booking.status == PENDING,
            Booking.created_at <= now - timedelta(minutes=hold_min),
        )
        .options(
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        )
    )
    expired = list(session.scalars(stmt).unique())
    for b in expired:
        b.status = CANCELLED
    session.flush()
    return expired


# ---------- выборки ----------


def client_bookings(session: Session, tg_id: int, now: datetime) -> list[Booking]:
    stmt = (
        select(Booking)
        .join(Client)
        .where(
            Client.tg_id == tg_id,
            Booking.status.in_(BLOCKING),
            Booking.end_at > now,
        )
        .options(joinedload(Booking.service), joinedload(Booking.master))
        .order_by(Booking.start_at)
    )
    return list(session.scalars(stmt).unique())


def upcoming_bookings(session: Session, now: datetime, limit: int = 200) -> list[Booking]:
    stmt = (
        select(Booking)
        .where(Booking.status.in_(BLOCKING), Booking.end_at > now)
        .options(
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        )
        .order_by(Booking.start_at)
        .limit(limit)
    )
    return list(session.scalars(stmt).unique())


def due_reminders(
    session: Session, now: datetime, before_min: int | None = None
) -> list[Booking]:
    """Записи, о которых пора напомнить (и которые ещё не напоминали). Неоплаченные брони не в счёт."""
    before = timedelta(minutes=before_min or settings.reminder_before_min)
    stmt = (
        select(Booking)
        .where(
            Booking.status == ACTIVE,
            Booking.reminder_sent.is_(False),
            Booking.start_at > now,
            Booking.start_at <= now + before,
        )
        .options(
            joinedload(Booking.client),
            joinedload(Booking.service),
            joinedload(Booking.master),
        )
    )
    return list(session.scalars(stmt).unique())


def format_booking(b: Booking) -> str:
    return (
        f"{b.service.name} — {b.master.name}\n"
        f"{b.start_at:%d.%m.%Y} в {b.start_at:%H:%M}"
    )
