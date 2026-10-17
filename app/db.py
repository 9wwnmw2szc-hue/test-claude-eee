"""Подключение к БД. Движок и фабрика сессий — на уровне модуля (тесты их подменяют)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import now_in_tz, settings
from app.models import Base


def normalize_url(url: str) -> str:
    """Railway и Heroku отдают postgres:// или postgresql://; SQLAlchemy нужен явный драйвер psycopg."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _make_engine(url: str) -> Engine:
    url = normalize_url(url)
    kwargs: dict = {}
    if not url.startswith("sqlite"):
        kwargs["pool_pre_ping"] = True  # переживаем обрыв соединения после простоя
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


def _sql_literal(value: object, dialect: str = "sqlite") -> str:
    if isinstance(value, bool):
        if dialect == "sqlite":
            return "1" if value else "0"
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def ensure_columns(bind: Engine) -> list[str]:
    """Добавляет в уже существующие таблицы колонки, появившиеся в моделях позже.

    Это минимальная «миграция» для обновления кода без потери данных: только добавление
    колонок со скалярным значением по умолчанию (или допускающих NULL). Всё остальное —
    переименования, удаление, смена типов — требует Alembic. Возвращает список добавленных колонок.
    """
    from sqlalchemy import inspect

    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    preparer = bind.dialect.identifier_preparer
    added: list[str] = []

    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                parts = [
                    "ALTER TABLE",
                    preparer.quote(table.name),
                    "ADD COLUMN",
                    preparer.quote(col.name),
                    col.type.compile(dialect=bind.dialect),
                ]
                if col.default is not None and col.default.is_scalar:
                    parts += ["DEFAULT", _sql_literal(col.default.arg, bind.dialect.name)]
                elif not col.nullable:
                    raise RuntimeError(
                        f"Колонку {table.name}.{col.name} нельзя добавить автоматически "
                        "(NOT NULL без значения по умолчанию) — нужна ручная миграция."
                    )
                conn.exec_driver_sql(" ".join(parts))
                added.append(f"{table.name}.{col.name}")
    return added


def init_db(bind: Engine | None = None) -> None:
    """Создаёт недостающие таблицы и добавляет новые колонки в существующие."""
    target = bind or engine
    Base.metadata.create_all(target)
    ensure_columns(target)


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
