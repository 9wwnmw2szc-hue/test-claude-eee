"""Бизнес-логика записи. Не зависит от Telegram и от веб-части."""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import ACTIVE, CANCELLED, Booking, Client, Master, Service, WorkingHours


class BookingError(Exception):
    """Ошибка бизнес-логики, текст можно показывать клиенту."""


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


def _overlaps(session: Session, master_id: int, start: datetime, end: datetime) -> bool:
    stmt = select(Booking.id).where(
        Booking.master_id == master_id,
        Booking.status == ACTIVE,
        Booking.start_at < end,
        Booking.end_at > start,
    )
    return session.scalar(stmt) is not None


def get_free_slots(
    session: Session,
    master_id: int,
    service_id: int,
    day: date,
    now: datetime,
    step_min: int | None = None,
    min_lead_min: int | None = None,
) -> list[datetime]:
    """Свободные времена начала для услуги у мастера в указанный день."""
    step = timedelta(minutes=step_min or settings.slot_step_min)
    lead = timedelta(
        minutes=settings.min_lead_min if min_lead_min is None else min_lead_min
    )
    service = session.get(Service, service_id)
    master = session.get(Master, master_id)
    if service is None or master is None or not service.active or not master.active:
        return []
    duration = timedelta(minutes=service.duration_min)

    windows = session.scalars(
        select(WorkingHours).where(
            WorkingHours.master_id == master_id,
            WorkingHours.weekday == day.weekday(),
        )
    ).all()

    busy = session.execute(
        select(Booking.start_at, Booking.end_at).where(
            Booking.master_id == master_id,
            Booking.status == ACTIVE,
            Booking.start_at < datetime.combine(day, datetime.max.time()),
            Booking.end_at > datetime.combine(day, datetime.min.time()),
        )
    ).all()

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
    session: Session, master_id: int, service_id: int, now: datetime
) -> list[date]:
    """Даты в горизонте записи, где у мастера есть хотя бы один свободный слот."""
    days = []
    for i in range(settings.booking_days_ahead + 1):
        d = now.date() + timedelta(days=i)
        if get_free_slots(session, master_id, service_id, d, now):
            days.append(d)
    return days


def create_booking(
    session: Session,
    client: Client,
    master_id: int,
    service_id: int,
    start_at: datetime,
    now: datetime,
) -> Booking:
    service = session.get(Service, service_id)
    master = session.get(Master, master_id)
    if service is None or master is None:
        raise BookingError("Услуга или мастер не найдены.")
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
        status=ACTIVE,
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
    if booking.status != ACTIVE:
        raise BookingError("Запись уже отменена.")
    booking.status = CANCELLED
    session.flush()
    return booking


def client_bookings(session: Session, tg_id: int, now: datetime) -> list[Booking]:
    stmt = (
        select(Booking)
        .join(Client)
        .where(
            Client.tg_id == tg_id,
            Booking.status == ACTIVE,
            Booking.end_at > now,
        )
        .options(joinedload(Booking.service), joinedload(Booking.master))
        .order_by(Booking.start_at)
    )
    return list(session.scalars(stmt).unique())


def upcoming_bookings(session: Session, now: datetime, limit: int = 200) -> list[Booking]:
    stmt = (
        select(Booking)
        .where(Booking.status == ACTIVE, Booking.end_at > now)
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
    """Записи, о которых пора напомнить (и которые ещё не напоминали)."""
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
