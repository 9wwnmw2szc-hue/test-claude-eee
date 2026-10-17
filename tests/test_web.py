"""Проверка админ-панели: доступ, отображение, CSRF-защита и основные действия."""

from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import booking, db
from app import web as web_module
from app.models import ACTIVE, CANCELLED, Booking, Master, Service, WorkingHours

NOW = datetime(2026, 10, 19, 8, 0)  # понедельник
AUTH = ("admin", "s3cret-pass")
SAME_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture()
def client(engine, data, monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(db, "now_local", lambda: NOW)
    monkeypatch.setattr(
        web_module, "settings", replace(web_module.settings, admin_password=AUTH[1])
    )
    # без контекстного менеджера: lifespan (и init_db на боевой базе) не запускается
    return TestClient(web_module.app)


@pytest.fixture()
def made_booking(session, data):
    service, master = data
    c = booking.get_or_create_client(session, 111, "<script>alert(1)</script>", "ivan")
    b = booking.create_booking(
        session, c, master.id, service.id, datetime(2026, 10, 19, 11, 0), NOW
    )
    session.commit()
    return b


def test_requires_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/", auth=("hacker", AUTH[1])).status_code == 401


def test_panel_is_closed_with_default_password(client, monkeypatch):
    monkeypatch.setattr(
        web_module, "settings", replace(web_module.settings, admin_password="change-me")
    )
    r = client.get("/", auth=("admin", "change-me"))
    assert r.status_code == 503
    assert "ADMIN_PASSWORD" in r.text


def test_pages_render(client, made_booking):
    for path in ("/", "/clients", "/services", "/masters"):
        assert client.get(path, auth=AUTH).status_code == 200, path


def test_user_input_is_escaped(client, made_booking):
    html = client.get("/", auth=AUTH).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_cancel_booking_via_panel(client, made_booking, session):
    r = client.post(
        f"/bookings/{made_booking.id}/cancel", auth=AUTH, headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    session.expire_all()
    assert session.get(Booking, made_booking.id).status == CANCELLED


def test_cross_site_post_is_rejected(client, made_booking, session):
    r = client.post(
        f"/bookings/{made_booking.id}/cancel", auth=AUTH,
        headers={"Origin": "https://evil.example"}, follow_redirects=False,
    )
    assert r.status_code == 403
    session.expire_all()
    assert session.get(Booking, made_booking.id).status == ACTIVE


def test_add_and_toggle_service(client, session):
    r = client.post(
        "/services", auth=AUTH, headers=SAME_ORIGIN, follow_redirects=False,
        data={"name": "Укладка", "duration_min": "45", "price": "900"},
    )
    assert r.status_code == 303
    svc = session.scalars(select(Service).where(Service.name == "Укладка")).one()
    assert (svc.duration_min, svc.price, svc.active) == (45, 900, True)

    client.post(f"/services/{svc.id}/toggle", auth=AUTH, headers=SAME_ORIGIN)
    session.expire_all()
    assert session.get(Service, svc.id).active is False


def test_invalid_service_input_rejected(client):
    r = client.post(
        "/services", auth=AUTH, headers=SAME_ORIGIN,
        data={"name": "Ноль минут", "duration_min": "0", "price": "100"},
    )
    assert r.status_code == 422


def test_working_hours_validation_and_delete(client, data, session):
    _, master = data
    bad = client.post(
        f"/masters/{master.id}/hours", auth=AUTH, headers=SAME_ORIGIN,
        data={"weekday": "5", "start": "15:00", "end": "12:00"},
    )
    assert bad.status_code == 422

    ok = client.post(
        f"/masters/{master.id}/hours", auth=AUTH, headers=SAME_ORIGIN,
        data={"weekday": "5", "start": "10:00", "end": "14:00"}, follow_redirects=False,
    )
    assert ok.status_code == 303
    window = session.scalars(
        select(WorkingHours).where(WorkingHours.master_id == master.id, WorkingHours.weekday == 5)
    ).one()

    window_id = window.id  # читаем до удаления: после expire_all() у стёртой строки атрибуты недоступны
    client.post(f"/hours/{window_id}/delete", auth=AUTH, headers=SAME_ORIGIN)
    session.expire_all()
    assert session.get(WorkingHours, window_id) is None


def test_add_and_toggle_master(client, session):
    client.post("/masters", auth=AUTH, headers=SAME_ORIGIN, data={"name": "Ольга"})
    m = session.scalars(select(Master).where(Master.name == "Ольга")).one()
    assert m.active is True
    client.post(f"/masters/{m.id}/toggle", auth=AUTH, headers=SAME_ORIGIN)
    session.expire_all()
    assert session.get(Master, m.id).active is False
