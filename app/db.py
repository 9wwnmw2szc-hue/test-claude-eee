"""Подключение к БД. Движок и фабрика сессий — на уровне модуля (тесты их подменяют)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import now_in_tz, settings
from app.models import Base


def _make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # бот и админка работают из разных потоков
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Сессия с коммитом при успехе и откатом при ошибке."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def now_local() -> datetime:
    """Текущее время бизнеса (без tzinfo). Тесты подменяют эту функцию."""
    return now_in_tz(settings.timezone)
