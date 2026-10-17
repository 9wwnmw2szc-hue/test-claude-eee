from dataclasses import replace
from datetime import time
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import bot as bot_module
from app import db
from app.models import Base, Master, Service, WorkingHours
from tests.bot_helpers import ADMIN_ID, NOW, FakeSession


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


@pytest.fixture()
def bot_env(engine, data, monkeypatch):
    """Фабрика окружения бота: база в памяти, фиксированное «сейчас», фейковый Telegram.

    Вызов bot_env(payment_provider_token="X", prepayment_percent=30) переопределяет настройки бота.
    """

    def make(now=NOW, **settings_overrides):
        monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
        monkeypatch.setattr(db, "now_local", lambda: now)
        monkeypatch.setattr(
            bot_module,
            "settings",
            replace(bot_module.settings, admin_ids=[ADMIN_ID], **settings_overrides),
        )
        fake = FakeSession()
        bot = Bot(
            "123456789:" + "A" * 35,
            session=fake,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        service, master = data
        return SimpleNamespace(
            dp=bot_module.build_dispatcher(), bot=bot, fake=fake, service=service, master=master
        )

    return make
