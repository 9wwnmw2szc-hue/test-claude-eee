"""Демо-данные: python -m app.seed"""

from datetime import time

from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import Master, Service, WorkingHours


def seed(session) -> None:
    if session.scalar(select(Service.id)) is not None:
        print("В базе уже есть данные, пропускаю.")
        return
    session.add_all(
        [
            Service(name="Стрижка", duration_min=60, price=1500),
            Service(name="Окрашивание", duration_min=120, price=4000),
            Service(name="Маникюр", duration_min=90, price=2000),
        ]
    )
    for name in ("Анна", "Мария"):
        master = Master(name=name)
        master.hours = [
            WorkingHours(weekday=d, start=time(10, 0), end=time(19, 0))
            for d in range(0, 6)  # Пн-Сб
        ]
        session.add(master)
    print("Демо-данные добавлены: 3 услуги, 2 мастера (Пн-Сб 10:00-19:00).")


if __name__ == "__main__":
    init_db()
    with session_scope() as s:
        seed(s)
