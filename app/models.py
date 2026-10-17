"""ORM-модели. Время записей хранится «настенное», в часовом поясе бизнеса (TIMEZONE)."""

from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, String, Table, Time
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import now_in_tz, settings

# Статусы записи
ACTIVE = "active"  # подтверждена
PENDING = "pending_payment"  # время удержано, ждём предоплату
CANCELLED = "cancelled"
DONE = "done"  # визит состоялся (отмечает администратор)
NO_SHOW = "no_show"  # клиент не пришёл (отмечает администратор)
BLOCKING = (ACTIVE, PENDING)  # «открытые» записи: их можно отменить и переносить, они в «Моих записях»
OCCUPYING = (ACTIVE, PENDING, DONE)  # статусы, которые занимают время мастера

# Индекс = datetime.weekday()
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _now() -> datetime:
    return now_in_tz(settings.timezone)


class Base(DeclarativeBase):
    pass


# Какие услуги выполняет мастер. Если у мастера не отмечено ни одной — он выполняет все.
master_services = Table(
    "master_services",
    Base.metadata,
    Column("master_id", ForeignKey("masters.id", ondelete="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
)


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
    services: Mapped[list[Service]] = relationship(secondary=master_services, order_by=Service.id)


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
    phone: Mapped[str] = mapped_column(String(32), default="")
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

    # Предоплата, в рублях. prepay_amount — сколько нужно внести, paid_amount — сколько внесено.
    prepay_amount: Mapped[int] = mapped_column(default=0)
    paid_amount: Mapped[int] = mapped_column(default=0)
    payment_charge_id: Mapped[str] = mapped_column(String(128), default="")

    # Цена услуги на момент записи: смена прайса не меняет историю и выручку.
    # У записей, созданных до появления поля, здесь 0 — тогда берётся текущая цена услуги.
    price: Mapped[int] = mapped_column(default=0)

    # Оценка визита клиентом (1–5, 0 — нет) и признак того, что просьбу оценить уже отправили
    rating: Mapped[int] = mapped_column(default=0)
    review_sent: Mapped[bool] = mapped_column(default=False)

    client: Mapped[Client] = relationship()
    master: Mapped[Master] = relationship()
    service: Mapped[Service] = relationship()
