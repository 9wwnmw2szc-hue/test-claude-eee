"""Настройки из переменных окружения и файла .env (см. .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _text(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Переменная {name} должна быть целым числом, сейчас: {raw!r}")


def _int_list(name: str) -> list[int]:
    ids: list[int] = []
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise SystemExit(f"{name}: ожидаются числа через запятую, а не {part!r}")
    return ids


@dataclass(frozen=True)
class Settings:
    # Бот
    bot_token: str = field(default_factory=lambda: _text("BOT_TOKEN", ""))
    business_name: str = field(default_factory=lambda: _text("BUSINESS_NAME", "Мой салон"))
    admin_ids: list[int] = field(default_factory=lambda: _int_list("ADMIN_IDS"))

    # Запись
    slot_step_min: int = field(default_factory=lambda: _int("SLOT_STEP_MIN", 30))
    min_lead_min: int = field(default_factory=lambda: _int("MIN_LEAD_MIN", 60))
    booking_days_ahead: int = field(default_factory=lambda: _int("BOOKING_DAYS_AHEAD", 14))
    reminder_before_min: int = field(default_factory=lambda: _int("REMINDER_BEFORE_MIN", 120))

    # Хранилище и время
    database_url: str = field(default_factory=lambda: _text("DATABASE_URL", "sqlite:///./booking.db"))
    timezone: str = field(default_factory=lambda: _text("TIMEZONE", "Europe/Moscow"))

    # Админ-панель
    admin_user: str = field(default_factory=lambda: _text("ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: _text("ADMIN_PASSWORD", "change-me"))


def now_in_tz(timezone: str) -> datetime:
    """Текущее «настенное» время в часовом поясе бизнеса, без tzinfo."""
    return datetime.now(ZoneInfo(timezone)).replace(tzinfo=None)


settings = Settings()
