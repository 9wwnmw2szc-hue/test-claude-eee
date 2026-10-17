"""Админ-панель. Запуск: uvicorn app.web:app --host 0.0.0.0 --port 8000"""

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import time, timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app import booking, db, stats
from app.booking import BookingError
from app.config import settings
from app.models import (
    DONE,
    NO_SHOW,
    PENDING,
    WEEKDAYS,
    Booking,
    Client,
    Master,
    Service,
    WorkingHours,
)

log = logging.getLogger("booking-admin")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["rub"] = lambda n: f"{int(n):,}".replace(",", " ") + " ₽"
security = HTTPBasic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if password_not_set():
        log.warning("ADMIN_PASSWORD не задан — админка закрыта. Укажите пароль в .env")
    yield


def password_not_set() -> bool:
    return settings.admin_password in ("", "change-me")


def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> None:
    # Пароль по умолчанию известен всем, поэтому с ним панель не открываем вообще.
    if password_not_set():
        raise HTTPException(
            status_code=503,
            detail="Админка закрыта: задайте свой ADMIN_PASSWORD в файле .env и перезапустите.",
        )
    user_ok = secrets.compare_digest(
        creds.username.encode(), settings.admin_user.encode()
    )
    pass_ok = secrets.compare_digest(
        creds.password.encode(), settings.admin_password.encode()
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )


def same_origin(request: Request) -> None:
    """Защита от CSRF: браузер сам подставляет Basic-авторизацию, поэтому для POST
    требуем, чтобы Origin/Referer (если присутствуют) совпадали с хостом панели."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if source and urlparse(source).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Запрос с чужого origin")


app = FastAPI(title="Booking admin", lifespan=lifespan, docs_url=None, redoc_url=None)

AUTH = [Depends(require_auth)]
POST_GUARD = [Depends(require_auth), Depends(same_origin)]


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(
        request,
        name,
        {
            "business": settings.business_name,
            "weekdays": WEEKDAYS,
            "PENDING": PENDING,
            "DONE": DONE,
            "NO_SHOW": NO_SHOW,
            **ctx,
        },
    )


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# ---------- записи ----------


@app.get("/", dependencies=AUTH)
def bookings_page(request: Request):
    with db.session_scope() as s:
        items = booking.upcoming_bookings(s, db.now_local())
        return render(request, "bookings.html", bookings=items)


@app.post("/bookings/{booking_id}/cancel", dependencies=POST_GUARD)
def cancel(booking_id: int):
    with db.session_scope() as s:
        try:
            booking.cancel_booking(s, booking_id)
        except BookingError:
            pass
    return back("/")


# ---------- итоги визитов и статистика ----------


@app.get("/past", dependencies=AUTH)
def past_page(request: Request):
    with db.session_scope() as s:
        now = db.now_local()
        return render(
            request,
            "past.html",
            to_close=booking.bookings_to_close(s, now),
            closed=booking.recently_closed(s, now),
        )


@app.post("/bookings/{booking_id}/done", dependencies=POST_GUARD)
def visit_done(booking_id: int):
    with db.session_scope() as s:
        try:
            booking.mark_done(s, booking_id, db.now_local())
        except BookingError:
            pass
    return back("/past")


@app.post("/bookings/{booking_id}/no-show", dependencies=POST_GUARD)
def visit_no_show(booking_id: int):
    with db.session_scope() as s:
        try:
            booking.mark_no_show(s, booking_id, db.now_local())
        except BookingError:
            pass
    return back("/past")


@app.get("/stats", dependencies=AUTH)
def stats_page(request: Request, days: int = Query(30, ge=1, le=730)):
    now = db.now_local()
    with db.session_scope() as s:
        data = stats.period_stats(s, now - timedelta(days=days), now, now)
    return render(request, "stats.html", days=days, s=data)


# ---------- клиенты ----------


@app.get("/clients", dependencies=AUTH)
def clients_page(request: Request):
    with db.session_scope() as s:
        total = func.count(Booking.id).label("total")
        rows = s.execute(
            select(Client, total)
            .outerjoin(Booking, Booking.client_id == Client.id)
            .group_by(Client.id)
            .order_by(Client.created_at.desc())
        ).all()
        return render(request, "clients.html", rows=rows)


# ---------- услуги ----------


@app.get("/services", dependencies=AUTH)
def services_page(request: Request):
    with db.session_scope() as s:
        items = list(s.scalars(select(Service).order_by(Service.id)))
        return render(request, "services.html", services=items)


@app.post("/services", dependencies=POST_GUARD)
def add_service(
    name: str = Form(..., min_length=1, max_length=120),
    duration_min: int = Form(..., ge=5, le=720),
    price: int = Form(0, ge=0),
):
    with db.session_scope() as s:
        s.add(Service(name=name.strip(), duration_min=duration_min, price=price))
    return back("/services")


@app.post("/services/{service_id}/toggle", dependencies=POST_GUARD)
def toggle_service(service_id: int):
    with db.session_scope() as s:
        svc = s.get(Service, service_id)
        if svc is None:
            raise HTTPException(404)
        svc.active = not svc.active
    return back("/services")


# ---------- мастера ----------


@app.get("/masters", dependencies=AUTH)
def masters_page(request: Request):
    with db.session_scope() as s:
        items = list(s.scalars(select(Master).order_by(Master.id)))
        for m in items:
            m.hours.sort(key=lambda h: (h.weekday, h.start))
        all_services = list(s.scalars(select(Service).order_by(Service.id)))
        return render(request, "masters.html", masters=items, services=all_services)


@app.post("/masters", dependencies=POST_GUARD)
def add_master(name: str = Form(..., min_length=1, max_length=120)):
    with db.session_scope() as s:
        s.add(Master(name=name.strip()))
    return back("/masters")


@app.post("/masters/{master_id}/toggle", dependencies=POST_GUARD)
def toggle_master(master_id: int):
    with db.session_scope() as s:
        m = s.get(Master, master_id)
        if m is None:
            raise HTTPException(404)
        m.active = not m.active
    return back("/masters")


@app.post("/masters/{master_id}/services", dependencies=POST_GUARD)
def set_master_services(master_id: int, service_ids: list[int] = Form(default=[])):
    """Какие услуги выполняет мастер. Ничего не отмечено — значит, все."""
    with db.session_scope() as s:
        m = s.get(Master, master_id)
        if m is None:
            raise HTTPException(404)
        chosen = (
            list(s.scalars(select(Service).where(Service.id.in_(service_ids)).order_by(Service.id)))
            if service_ids
            else []
        )
        m.services = chosen
    return back("/masters")


@app.post("/masters/{master_id}/hours", dependencies=POST_GUARD)
def add_hours(
    master_id: int,
    weekday: int = Form(..., ge=0, le=6),
    start: time = Form(...),
    end: time = Form(...),
):
    if start >= end:
        raise HTTPException(422, "Начало должно быть раньше конца")
    with db.session_scope() as s:
        if s.get(Master, master_id) is None:
            raise HTTPException(404)
        s.add(WorkingHours(master_id=master_id, weekday=weekday, start=start, end=end))
    return back("/masters")


@app.post("/hours/{hours_id}/delete", dependencies=POST_GUARD)
def delete_hours(hours_id: int):
    with db.session_scope() as s:
        h = s.get(WorkingHours, hours_id)
        if h is not None:
            s.delete(h)
    return back("/masters")
