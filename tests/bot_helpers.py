"""Общие помощники для сквозных тестов бота: фейковая сессия Telegram и сборщики апдейтов."""

import asyncio
from datetime import datetime

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendInvoice, SendMessage
from aiogram.types import Chat, Message

NOW = datetime(2026, 10, 19, 8, 0)  # понедельник
ADMIN_ID = 999


class FakeSession(BaseSession):
    """Вместо сети запоминает все вызовы Telegram API в self.calls."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_invoice = False  # имитирует отказ Telegram выставить счёт

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendInvoice) and self.fail_invoice:
            raise TelegramBadRequest(method=method, message="Bad Request: CURRENCY_TOTAL_AMOUNT_INVALID")
        if isinstance(method, (SendMessage, SendInvoice)):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


def user(uid, name="Иван"):
    return {"id": uid, "is_bot": False, "first_name": name, "username": f"user{uid}"}


def cb_update(n, uid, data):
    return {
        "update_id": n,
        "callback_query": {
            "id": str(n),
            "from": user(uid),
            "chat_instance": "x",
            "data": data,
            "message": {
                "message_id": 10,
                "date": 1760000000,
                "chat": {"id": uid, "type": "private"},
                "text": "prev",
            },
        },
    }


def msg_update(n, uid, **fields):
    return {
        "update_id": n,
        "message": {
            "message_id": n,
            "date": 1760000000,
            "chat": {"id": uid, "type": "private"},
            "from": user(uid),
            **fields,
        },
    }


def command_update(n, uid, command):
    return msg_update(
        n, uid, text=command, entities=[{"type": "bot_command", "offset": 0, "length": len(command)}]
    )


def contact_update(n, uid, phone, contact_user_id=None):
    return msg_update(
        n,
        uid,
        contact={
            "phone_number": phone,
            "first_name": "Иван",
            "user_id": uid if contact_user_id is None else contact_user_id,
        },
    )


def pre_checkout_update(n, uid, booking_id, amount_rub, currency="RUB"):
    return {
        "update_id": n,
        "pre_checkout_query": {
            "id": str(n),
            "from": user(uid),
            "currency": currency,
            "total_amount": amount_rub * 100,
            "invoice_payload": f"b:{booking_id}",
        },
    }


def payment_update(n, uid, booking_id, amount_rub, charge="charge-1", payload=None):
    return msg_update(
        n,
        uid,
        successful_payment={
            "currency": "RUB",
            "total_amount": amount_rub * 100,
            "invoice_payload": payload if payload is not None else f"b:{booking_id}",
            "telegram_payment_charge_id": f"tg-{charge}",
            "provider_payment_charge_id": charge,
        },
    )


def buttons(method):
    return [b.callback_data for row in method.reply_markup.inline_keyboard for b in row]


def calls_of(fake, cls):
    return [c for c in fake.calls if isinstance(c, cls)]


def last(fake, cls):
    return calls_of(fake, cls)[-1]


def feed(dp, bot, update):
    return asyncio.run(dp.feed_raw_update(bot, update))
