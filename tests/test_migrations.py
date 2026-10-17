"""Обновление уже существующей базы: новые колонки добавляются, данные остаются."""

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import db
from app.models import Booking, Client

# Схема «до обновления»: без phone, предоплаты и таблицы master_services
OLD_SCHEMA = """
CREATE TABLE clients (
    id INTEGER PRIMARY KEY, tg_id BIGINT NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL, username VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL
);
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, master_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL, start_at DATETIME NOT NULL, end_at DATETIME NOT NULL,
    status VARCHAR(16) NOT NULL, reminder_sent BOOLEAN NOT NULL, created_at DATETIME NOT NULL
);
INSERT INTO clients VALUES (1, 111, 'Иван', 'ivan', '2026-10-01 12:00:00');
INSERT INTO bookings VALUES (1, 1, 1, 1, '2026-10-20 11:00:00', '2026-10-20 12:00:00',
                             'active', 0, '2026-10-01 12:00:00');
"""


def old_engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in OLD_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(text(statement))
    return engine


def test_init_db_upgrades_old_database_without_losing_data():
    engine = old_engine()
    db.init_db(engine)

    columns = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in ("clients", "bookings")}
    assert "phone" in columns["clients"]
    new_booking_columns = {
        "prepay_amount", "paid_amount", "payment_charge_id", "price", "rating", "review_sent",
    }
    assert new_booking_columns <= columns["bookings"]
    assert "master_services" in inspect(engine).get_table_names()

    with Session(engine) as s:
        client = s.scalars(select(Client)).one()
        old_booking = s.scalars(select(Booking)).one()
    assert (client.name, client.phone) == ("Иван", "")  # данные целы, у новых колонок значения по умолчанию
    assert (old_booking.status, old_booking.paid_amount, old_booking.prepay_amount) == ("active", 0, 0)
    assert (old_booking.price, old_booking.rating, old_booking.review_sent) == (0, 0, False)


def test_second_run_changes_nothing():
    engine = old_engine()
    db.init_db(engine)
    assert db.ensure_columns(engine) == []


def test_fresh_database_needs_no_upgrade():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    db.init_db(engine)
    assert db.ensure_columns(engine) == []
