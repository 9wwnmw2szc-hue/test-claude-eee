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
    # Клиент может сам перенести запись не позднее чем за столько минут до визита
    reschedule_before_min: int = field(default_factory=lambda: _int("RESCHEDULE_BEFORE_MIN", 60))
    # через сколько часов после конца визита неотмеченная запись сама станет «состоялась» (0 — выключено)
    auto_done_after_hours: int = field(default_factory=lambda: _int("AUTO_DONE_AFTER_HOURS", 24))

    # Хранилище и время
    database_url: str = field(default_factory=lambda: _text("DATABASE_URL", "sqlite:///./booking.db"))
    timezone: str = field(default_factory=lambda: _text("TIMEZONE", "Europe/Moscow"))

    # Админ-панель
    admin_user: str = field(default_factory=lambda: _text("ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: _text("ADMIN_PASSWORD", "change-me"))

    # Предоплата через Telegram Payments (токен платёжного провайдера выдаёт @BotFather → Payments)
    payment_provider_token: str = field(default_factory=lambda: _text("PAYMENT_PROVIDER_TOKEN", ""))
    prepayment_percent: int = field(default_factory=lambda: _int("PREPAYMENT_PERCENT", 0))
    payment_currency: str = field(default_factory=lambda: _text("PAYMENT_CURRENCY", "RUB").upper())
    payment_hold_min: int = field(default_factory=lambda: _int("PAYMENT_HOLD_MIN", 15))

    def __post_init__(self) -> None:
        if not 0 <= self.prepayment_percent <= 100:
            raise SystemExit("PREPAYMENT_PERCENT должен быть числом от 0 до 100")
        if self.payment_hold_min < 1:
            raise SystemExit("PAYMENT_HOLD_MIN должен быть не меньше 1 минуты")

    @property
    def prepayment_enabled(self) -> bool:
        """Предоплата включена, только если есть и токен провайдера, и процент."""
        return bool(self.payment_provider_token) and self.prepayment_percent > 0


def now_in_tz(timezone: str) -> datetime:
    """Текущее «настенное» время в часовом поясе бизнеса, без tzinfo."""
    return datetime.now(ZoneInfo(timezone)).replace(tzinfo=None)


settings = Settings()
