"""Tests for ai.answer_question.

Offline: a fake `plan_call` returns a canned JSON query plan per question, and
phrasing uses the deterministic template (phrase_with_model=False), so the
whole plan -> whitelisted query -> answer pipeline is exercised without a live
model. Queries run against a seeded in-memory SQLite DB.

Run:  pytest -s tests/test_answer.py
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import ai
import db


@pytest.fixture()
def conn():
    """Seeded in-memory DB with known numbers."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    # Rooms
    db.insert_room(c, "Focus Room", 4, 800.0)
    db.insert_room(c, "Event Space", 40, 6000.0)
    f = db.get_room_id_by_name(c, "Focus Room")
    e = db.get_room_id_by_name(c, "Event Space")
    # October bookings: Focus 2h paid (1600), Event 4h paid (24000), Focus 1h pending (800)
    db.insert_booking(c, "A", f, "2026-10-02", "10:00", "12:00", 1600, "paid")
    db.insert_booking(c, "B", e, "2026-10-03", "14:00", "18:00", 24000, "paid")
    db.insert_booking(c, "C", f, "2026-10-04", "09:00", "10:00", 800, "pending")
    # Events
    db.insert_event(c, "PyData", "PyData KHI", e, "2026-10-18", "18:00", "20:00")
    db.insert_event(c, "Mixer", "Startup Grind", f, "2026-10-20", "17:00", "19:00")
    db.insert_event(c, "Clinic", "PyData KHI", f, "2026-10-22", "15:00", "17:00")
    c.commit()
    return c


def _plan(plan_dict):
    def fake(system, user, model):
        return json.dumps(plan_dict)
    return fake


def test_revenue_per_room_identifies_top(conn):
    plan = {"metric": "revenue", "group_by": "room", "month": "2026-10", "room": None}
    ans = ai.answer_question(
        "Which room made the most revenue in October 2026?",
        conn=conn, plan_call=_plan(plan), phrase_with_model=False,
    )
    print("\nQ: top revenue room ->", ans)
    assert "Event Space" in ans
    assert "PKR 24,000" in ans


def test_total_revenue_value(conn):
    plan = {"metric": "revenue", "group_by": "none", "month": "2026-10", "room": None}
    ans = ai.answer_question("How much did we earn in October?",
                             conn=conn, plan_call=_plan(plan), phrase_with_model=False)
    print("Q: total revenue ->", ans)
    # 1600 + 24000 paid = 25600 (pending 800 excluded)
    assert "PKR 25,600" in ans


def test_pending_payments(conn):
    plan = {"metric": "pending_payments", "group_by": "none", "month": "2026-10", "room": None}
    ans = ai.answer_question("How much is still pending in October?",
                             conn=conn, plan_call=_plan(plan), phrase_with_model=False)
    print("Q: pending ->", ans)
    assert "PKR 800" in ans


def test_utilization_least_used(conn):
    plan = {"metric": "utilization", "group_by": "room", "month": "2026-10", "room": None}
    ans = ai.answer_question("Which room was used least in October?",
                             conn=conn, plan_call=_plan(plan), phrase_with_model=False)
    print("Q: utilization ->", ans)
    # Focus: 2h + 1h = 3h; Event: 4h. Least = Focus Room, most = Event Space.
    assert "Focus Room least" in ans
    assert "Event Space was used most" in ans


def test_bookings_count(conn):
    plan = {"metric": "bookings", "group_by": "none", "month": "2026-10", "room": None}
    ans = ai.answer_question("How many bookings in October?",
                             conn=conn, plan_call=_plan(plan), phrase_with_model=False)
    print("Q: bookings ->", ans)
    assert "3 bookings" in ans


def test_events_by_organizer(conn):
    plan = {"metric": "events", "group_by": "organizer", "month": "2026-10", "room": None}
    ans = ai.answer_question("Which organizer ran the most events in October?",
                             conn=conn, plan_call=_plan(plan), phrase_with_model=False)
    print("Q: events by organizer ->", ans)
    assert "PyData KHI (2)" in ans


def test_unanswerable_metric_null(conn):
    plan = {"metric": None, "group_by": "none", "month": None, "room": None}
    ans = ai.answer_question("What's the weather today?",
                             conn=conn, plan_call=_plan(plan), phrase_with_model=False)
    print("Q: unanswerable ->", ans)
    assert "only answer questions about" in ans.lower()


def test_invalid_plan_twice_graceful(conn):
    bad = lambda s, u, m: "not json at all"
    ans = ai.answer_question("gibberish?", conn=conn, plan_call=bad,
                             phrase_with_model=False)
    print("Q: invalid plan ->", ans)
    assert "couldn't turn that into a question" in ans.lower()


def test_empty_question_raises(conn):
    with pytest.raises(ai.AIError):
        ai.answer_question("   ", conn=conn, plan_call=_plan({}), phrase_with_model=False)


def test_model_phrases_from_numbers(conn):
    # Model phrasing path: a fake phrase_call echoes a sentence; verify used.
    plan = {"metric": "revenue", "group_by": "none", "month": "2026-10", "room": None}
    phrase = lambda s, u, m: "You earned PKR 25,600 in October 2026."
    ans = ai.answer_question("earnings?", conn=conn, plan_call=_plan(plan),
                             phrase_call=phrase, phrase_with_model=True)
    print("Q: model-phrased ->", ans)
    assert ans == "You earned PKR 25,600 in October 2026."
