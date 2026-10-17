"""Демо-данные: python -m app.seed"""

from datetime import time

from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import Master, Service, WorkingHours


def seed(session) -> None:
    if session.scalar(select(Service.id)) is not None:
        print("В базе уже есть данные, пропускаю.")
        return
    haircut = Service(name="Стрижка", duration_min=60, price=1500)
    coloring = Service(name="Окрашивание", duration_min=120, price=4000)
    manicure = Service(name="Маникюр", duration_min=90, price=2000)
    session.add_all([haircut, coloring, manicure])

    for name, services in (
        ("Анна", []),  # не отмечено ни одной услуги — выполняет все
        ("Мария", [haircut, manicure]),
    ):
        master = Master(name=name)
        master.services = services
        master.hours = [
            WorkingHours(weekday=d, start=time(10, 0), end=time(19, 0))
            for d in range(0, 6)  # Пн-Сб
        ]
        session.add(master)
    print(
        "Демо-данные добавлены: 3 услуги, 2 мастера (Пн-Сб 10:00-19:00). "
        "Анна выполняет все услуги, Мария — только стрижку и маникюр."
    )


if __name__ == "__main__":
    init_db()
    with session_scope() as s:
        seed(s)
