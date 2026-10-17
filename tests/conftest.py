from datetime import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Master, Service, WorkingHours


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def data(session):
    """Одна услуга на 60 мин, один мастер, работает Пн-Пт 10:00-14:00."""
    service = Service(name="Стрижка", duration_min=60, price=1500)
    master = Master(name="Анна")
    master.hours = [
        WorkingHours(weekday=d, start=time(10, 0), end=time(14, 0)) for d in range(5)
    ]
    session.add_all([service, master])
    session.commit()
    return service, master
