"""Статистика для админки: выручка, визиты, оценки. Считается в базе, без выгрузки всех записей."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ACTIVE, CANCELLED, DONE, NO_SHOW, PENDING, Booking, Client, Master, Service

# Цена визита: снимок на момент записи, а у старых записей (там 0) — текущая цена услуги
_price = func.coalesce(func.nullif(Booking.price, 0), Service.price)


def _in_period(start: datetime, end: datetime):
    return (Booking.start_at >= start, Booking.start_at < end)


def _grouped(session: Session, label, group_id, start: datetime, end: datetime, limit: int | None = None):
    """Состоявшиеся визиты и выручка в разрезе label (услуга, мастер, клиент)."""
    revenue = func.coalesce(func.sum(_price), 0)
    stmt = (
        select(label, func.count(Booking.id), revenue)
        .select_from(Booking)
        .join(Service, Service.id == Booking.service_id)
        .where(Booking.status == DONE, *_in_period(start, end))
        .group_by(group_id, label)
        .order_by(revenue.desc(), func.count(Booking.id).desc())
    )
    return stmt.limit(limit) if limit else stmt


def period_stats(session: Session, start: datetime, end: datetime, now: datetime) -> dict:
    """Итоги за период [start, end) по времени визита. now нужен для счётчика будущих записей."""
    by_status = dict(
        session.execute(
            select(Booking.status, func.count(Booking.id))
            .where(*_in_period(start, end))
            .group_by(Booking.status)
        ).all()
    )

    done_count, revenue = session.execute(
        select(func.count(Booking.id), func.coalesce(func.sum(_price), 0))
        .select_from(Booking)
        .join(Service, Service.id == Booking.service_id)
        .where(Booking.status == DONE, *_in_period(start, end))
    ).one()

    avg_rating, rated_count = session.execute(
        select(func.avg(Booking.rating), func.count(Booking.id)).where(
            Booking.status == DONE, Booking.rating > 0, *_in_period(start, end)
        )
    ).one()

    finished = done_count + by_status.get(NO_SHOW, 0)
    upcoming = session.scalar(
        select(func.count(Booking.id)).where(
            Booking.status.in_((ACTIVE, PENDING)), Booking.start_at > now
        )
    )

    return {
        "done": done_count,
        "no_show": by_status.get(NO_SHOW, 0),
        "cancelled": by_status.get(CANCELLED, 0),
        "upcoming": upcoming or 0,
        "revenue": int(revenue),
        "avg_check": int(revenue / done_count) if done_count else 0,
        # доля пришедших среди тех, чей визит уже должен был пройти
        "attendance": round(100 * done_count / finished) if finished else None,
        "avg_rating": round(float(avg_rating), 1) if avg_rating is not None else None,
        "rated_count": rated_count,
        "by_service": [
            {"name": n, "visits": c, "revenue": int(r)}
            for n, c, r in session.execute(_grouped(session, Service.name, Service.id, start, end))
        ],
        "by_master": [
            {"name": n, "visits": c, "revenue": int(r)}
            for n, c, r in session.execute(
                _grouped(session, Master.name, Master.id, start, end).join(
                    Master, Master.id == Booking.master_id
                )
            )
        ],
        "top_clients": [
            {"name": n or "—", "visits": c, "revenue": int(r)}
            for n, c, r in session.execute(
                _grouped(session, Client.name, Client.id, start, end, limit=5).join(
                    Client, Client.id == Booking.client_id
                )
            )
        ],
    }
