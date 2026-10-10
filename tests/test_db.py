"""Tests for db.py: schema, write/read helpers, dashboard readers, utilization,
double-booking detection, and the whitelisted query-plan dispatcher.

All tests use a seeded in-memory SQLite DB so they are fast and isolated.

Run:  pytest -s tests/test_db.py
"""

from __future__ import annotations

import sqlite3

import pytest

import db


@pytest.fixture()
def conn():
    """In-memory DB seeded with 2 rooms and known bookings/events."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)

    db.insert_room(c, "Focus Room", 4, 800.0)
    db.insert_room(c, "Event Space", 40, 6000.0)
    f = db.get_room_id_by_name(c, "Focus Room")
    e = db.get_room_id_by_name(c, "Event Space")

    # October 2026 bookings
    db.insert_booking(c, "A", f, "2026-10-02", "10:00", "12:00", 1600, "paid")     # Focus 2h paid
    db.insert_booking(c, "B", e, "2026-10-03", "14:00", "18:00", 24000, "paid")    # Event 4h paid
    db.insert_booking(c, "C", f, "2026-10-04", "09:00", "10:00", 800, "pending")   # Focus 1h pending
    # September 2026 booking
    db.insert_booking(c, "D", e, "2026-09-10", "09:00", "12:00", 18000, "paid")    # Event 3h paid

    # Events
    db.insert_event(c, "PyData", "PyData KHI", e, "2026-10-18", "18:00", "20:00")
    db.insert_event(c, "Mixer", "Startup Grind", f, "2026-10-20", "17:00", "19:00")
    db.insert_event(c, "Clinic", "PyData KHI", f, "2026-10-22", "15:00", "17:00")
    c.commit()
    return c


# ---------------------------------------------------------------------------
# Schema + lookups
# ---------------------------------------------------------------------------
def test_schema_creates_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"rooms", "bookings", "payments", "events"} <= tables


def test_list_rooms_and_lookup(conn):
    rooms = db.list_rooms(conn)
    assert [r["name"] for r in rooms] == ["Focus Room", "Event Space"]
    assert db.get_room_id_by_name(conn, "focus room") is not None  # case-insensitive
    assert db.get_room_id_by_name(conn, "No Such Room") is None
    assert db.get_room_id_by_name(conn, None) is None


# ---------------------------------------------------------------------------
# Dashboard readers
# ---------------------------------------------------------------------------
def test_total_bookings(conn):
    assert db.total_bookings(conn, "2026-10") == 3
    assert db.total_bookings(conn, "2026-09") == 1
    assert db.total_bookings(conn, None) == 4  # all months


def test_total_revenue_paid_only(conn):
    # Oct paid: 1600 + 24000 = 25600 (pending 800 excluded)
    assert db.total_revenue(conn, "2026-10") == 25600.0
    assert db.total_revenue(conn, "2026-09") == 18000.0


def test_pending_payments(conn):
    assert db.pending_payments(conn, "2026-10") == 800.0
    assert db.pending_payments(conn, "2026-09") == 0.0


def test_room_utilization_hours(conn):
    util = {r["room"]: r["hours"] for r in db.room_utilization(conn, "2026-10")}
    assert util["Focus Room"] == 3.0   # 2h + 1h
    assert util["Event Space"] == 4.0  # 4h


def test_revenue_per_room(conn):
    rev = {r["room"]: r["revenue"] for r in db.revenue_per_room(conn, "2026-10")}
    assert rev["Focus Room"] == 1600.0      # only the paid one
    assert rev["Event Space"] == 24000.0


def test_utilization_pct_available_hours(conn):
    rows = {r["room"]: r for r in db.room_utilization_pct(conn, "2026-10")}
    # October has 31 days * 12 open hours (9am-9pm) = 372 available hours
    assert rows["Focus Room"]["available"] == 372
    assert rows["Focus Room"]["pct"] == round(3.0 / 372 * 100, 1)


def test_total_events_and_by_organizer(conn):
    assert db.total_events(conn, "2026-10") == 3
    by_org = {r["organizer"]: r["count"] for r in db.events_by_organizer(conn, "2026-10")}
    assert by_org["PyData KHI"] == 2
    assert by_org["Startup Grind"] == 1


def test_available_months_and_revenue_by_month(conn):
    assert db.available_months(conn) == ["2026-09", "2026-10"]
    rbm = {r["month"]: r["revenue"] for r in db.revenue_by_month(conn)}
    assert rbm["2026-09"] == 18000.0
    assert rbm["2026-10"] == 25600.0


def test_empty_month_returns_zeroes(conn):
    assert db.total_bookings(conn, "2026-01") == 0
    assert db.total_revenue(conn, "2026-01") == 0.0
    assert db.upcoming_events(conn, "2026-01") == []


# ---------------------------------------------------------------------------
# Double-booking detection
# ---------------------------------------------------------------------------
def test_double_booking_overlap_detected(conn):
    f = db.get_room_id_by_name(conn, "Focus Room")
    # Existing Focus booking on 2026-10-02 is 10:00-12:00.
    c = db.find_conflicting_bookings(conn, f, "2026-10-02", "11:00", "13:00")
    assert len(c) == 1
    assert c[0]["customer_name"] == "A"


def test_double_booking_adjacent_not_conflict(conn):
    f = db.get_room_id_by_name(conn, "Focus Room")
    # 12:00-13:00 starts exactly when 10:00-12:00 ends -> no overlap.
    assert db.find_conflicting_bookings(conn, f, "2026-10-02", "12:00", "13:00") == []


def test_double_booking_different_room(conn):
    e = db.get_room_id_by_name(conn, "Event Space")
    # Same time as Focus booking but different room -> no conflict.
    assert db.find_conflicting_bookings(conn, e, "2026-10-02", "10:00", "12:00") == []


def test_double_booking_different_day(conn):
    f = db.get_room_id_by_name(conn, "Focus Room")
    assert db.find_conflicting_bookings(conn, f, "2026-10-09", "10:00", "12:00") == []


def test_double_booking_missing_fields(conn):
    f = db.get_room_id_by_name(conn, "Focus Room")
    assert db.find_conflicting_bookings(conn, f, "2026-10-02", None, "12:00") == []
    assert db.find_conflicting_bookings(conn, None, "2026-10-02", "10:00", "12:00") == []


def test_double_booking_fully_contained(conn):
    f = db.get_room_id_by_name(conn, "Focus Room")
    # New slot 10:30-11:00 is inside existing 10:00-12:00.
    c = db.find_conflicting_bookings(conn, f, "2026-10-02", "10:30", "11:00")
    assert len(c) == 1


# ---------------------------------------------------------------------------
# Whitelisted query-plan dispatcher
# ---------------------------------------------------------------------------
def test_run_query_plan_scalar(conn):
    assert db.run_query_plan(conn, {"metric": "total_bookings", "month": "2026-10"}) == 3
    assert db.run_query_plan(conn, {"metric": "total_revenue", "month": "2026-10"}) == 25600.0


def test_run_query_plan_room_filter(conn):
    plan = {"metric": "room_utilization", "group_by": "room",
            "month": "2026-10", "room": "Event Space"}
    result = db.run_query_plan(conn, plan)
    assert result == [{"room": "Event Space", "hours": 4.0}]


def test_run_query_plan_rejects_unknown_metric(conn):
    with pytest.raises(ValueError):
        db.run_query_plan(conn, {"metric": "drop_table", "month": None})
