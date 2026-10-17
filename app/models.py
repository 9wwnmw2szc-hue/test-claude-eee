"""ORM-модели. Время записей хранится «настенное», в часовом поясе бизнеса (TIMEZONE)."""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Time
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import now_in_tz, settings

# Статусы записи
ACTIVE = "active"
CANCELLED = "cancelled"

# Индекс = datetime.weekday()
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _now() -> datetime:
    return now_in_tz(settings.timezone)


class Base(DeclarativeBase):
    pass


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    duration_min: Mapped[int]
    price: Mapped[int] = mapped_column(default=0)  # в рублях
    active: Mapped[bool] = mapped_column(default=True)


class Master(Base):
    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(default=True)

    hours: Mapped[list[WorkingHours]] = relationship(
        cascade="all, delete-orphan",
        order_by="WorkingHours.weekday, WorkingHours.start",
    )


class WorkingHours(Base):
    """Рабочее окно мастера в день недели (0 = понедельник). Окон в день может быть несколько."""

    __tablename__ = "working_hours"

    id: Mapped[int] = mapped_column(primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"))
    weekday: Mapped[int]
    start: Mapped[time] = mapped_column(Time)
    end: Mapped[time] = mapped_column(Time)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    username: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (Index("ix_bookings_master_start", "master_id", "start_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=ACTIVE, index=True)
    reminder_sent: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    client: Mapped[Client] = relationship()
    master: Mapped[Master] = relationship()
    service: Mapped[Service] = relationship()
