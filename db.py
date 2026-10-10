"""SQLite helpers for CoworkOps AI.

Holds the schema (rooms, bookings, payments, events), parameterized write
helpers, parameterized dashboard readers, and a whitelisted query-plan
dispatcher used by the Ask AI feature. The model never produces SQL; it only
selects a metric/group_by/month/room, and run_query_plan maps that to one of
the fixed readers below.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

DB_PATH = os.getenv("COWORKOPS_DB", "coworkops.db")


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------
def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """Return a SQLite connection with row access by column name and FKs on."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    capacity    INTEGER NOT NULL,
    hourly_rate REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name  TEXT,
    room_id        INTEGER REFERENCES rooms(id),
    date           TEXT NOT NULL,          -- yyyy-mm-dd
    start_time     TEXT,                   -- HH:MM
    end_time       TEXT,                   -- HH:MM
    amount         REAL DEFAULT 0,         -- PKR
    payment_status TEXT DEFAULT 'pending'  -- 'paid' | 'pending'
);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER REFERENCES bookings(id),
    amount     REAL NOT NULL,              -- PKR
    status     TEXT DEFAULT 'paid',        -- 'paid' | 'pending'
    date       TEXT NOT NULL               -- yyyy-mm-dd
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    organizer  TEXT,
    room_id    INTEGER REFERENCES rooms(id),
    date       TEXT NOT NULL,              -- yyyy-mm-dd
    start_time TEXT,
    end_time   TEXT
);
"""


def init_db(db_path: str | None = None) -> None:
    """Create tables if they do not already exist."""
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def list_rooms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, capacity, hourly_rate FROM rooms ORDER BY id"
    ).fetchall()


def get_room_id_by_name(conn: sqlite3.Connection, name: str | None):
    """Return a room id by (case-insensitive) name, or None."""
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM rooms WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# Writes (parameterized)
# ---------------------------------------------------------------------------
def insert_room(conn, name, capacity, hourly_rate) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO rooms (name, capacity, hourly_rate) VALUES (?, ?, ?)",
        (name, capacity, hourly_rate),
    )
    conn.commit()
    return cur.lastrowid


def insert_booking(conn, customer_name, room_id, date, start_time,
                   end_time, amount, payment_status) -> int:
    cur = conn.execute(
        """INSERT INTO bookings
           (customer_name, room_id, date, start_time, end_time, amount, payment_status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (customer_name, room_id, date, start_time, end_time, amount, payment_status),
    )
    conn.commit()
    return cur.lastrowid


def insert_payment(conn, booking_id, amount, status, date) -> int:
    cur = conn.execute(
        "INSERT INTO payments (booking_id, amount, status, date) VALUES (?, ?, ?, ?)",
        (booking_id, amount, status, date),
    )
    conn.commit()
    return cur.lastrowid


def insert_event(conn, event_name, organizer, room_id, date,
                 start_time, end_time) -> int:
    cur = conn.execute(
        """INSERT INTO events
           (event_name, organizer, room_id, date, start_time, end_time)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_name, organizer, room_id, date, start_time, end_time),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _month_prefix(month: str | None) -> str:
    """Return a LIKE prefix like '2026-10%' for a yyyy-mm month, or '%' for all."""
    if not month:
        return "%"
    return f"{month}%"


def _hours_between(start_time: str | None, end_time: str | None) -> float:
    """Return duration in hours between two HH:MM strings, or 0 if unparseable."""
    if not start_time or not end_time:
        return 0.0
    try:
        s = datetime.strptime(start_time, "%H:%M")
        e = datetime.strptime(end_time, "%H:%M")
        delta = (e - s).total_seconds() / 3600.0
        return round(delta, 2) if delta > 0 else 0.0
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Dashboard readers (parameterized, whitelisted)
# ---------------------------------------------------------------------------
def total_bookings(conn, month: str | None = None) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM bookings WHERE date LIKE ?",
        (_month_prefix(month),),
    ).fetchone()
    return row["n"]


def total_revenue(conn, month: str | None = None) -> float:
    """Sum of amounts for bookings marked 'paid' in the month."""
    row = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total
           FROM bookings
           WHERE payment_status = 'paid' AND date LIKE ?""",
        (_month_prefix(month),),
    ).fetchone()
    return float(row["total"])


def pending_payments(conn, month: str | None = None) -> float:
    """Sum of amounts for bookings marked 'pending' in the month."""
    row = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total
           FROM bookings
           WHERE payment_status = 'pending' AND date LIKE ?""",
        (_month_prefix(month),),
    ).fetchone()
    return float(row["total"])


def room_utilization(conn, month: str | None = None) -> list[dict]:
    """Return [{room, hours}] of total booked hours per room in the month."""
    rows = conn.execute(
        """SELECT r.name AS room, b.start_time AS start_time, b.end_time AS end_time
           FROM rooms r
           LEFT JOIN bookings b
                  ON b.room_id = r.id AND b.date LIKE ?
           ORDER BY r.id""",
        (_month_prefix(month),),
    ).fetchall()

    totals: dict[str, float] = {}
    for row in rows:
        totals.setdefault(row["room"], 0.0)
        totals[row["room"]] += _hours_between(row["start_time"], row["end_time"])
    return [{"room": name, "hours": round(hours, 2)} for name, hours in totals.items()]


def upcoming_events(conn, month: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT e.event_name, e.organizer, r.name AS room,
                  e.date, e.start_time, e.end_time
           FROM events e
           LEFT JOIN rooms r ON r.id = e.room_id
           WHERE e.date LIKE ?
           ORDER BY e.date, e.start_time""",
        (_month_prefix(month),),
    ).fetchall()
    return [dict(r) for r in rows]


def events_by_organizer(conn, month: str | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT organizer, COUNT(*) AS count
           FROM events
           WHERE date LIKE ?
           GROUP BY organizer
           ORDER BY count DESC""",
        (_month_prefix(month),),
    ).fetchall()
    return [dict(r) for r in rows]


def total_events(conn, month: str | None = None) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE date LIKE ?",
        (_month_prefix(month),),
    ).fetchone()
    return row["n"]


def revenue_per_room(conn, month: str | None = None) -> list[dict]:
    """Return [{room, revenue}] of paid booking revenue per room in the month.

    Every room appears (0 when it has no paid bookings).
    """
    rows = conn.execute(
        """SELECT r.name AS room,
                  COALESCE(SUM(CASE WHEN b.payment_status = 'paid'
                                    THEN b.amount ELSE 0 END), 0) AS revenue
           FROM rooms r
           LEFT JOIN bookings b
                  ON b.room_id = r.id AND b.date LIKE ?
           GROUP BY r.id
           ORDER BY r.id""",
        (_month_prefix(month),),
    ).fetchall()
    return [{"room": r["room"], "revenue": float(r["revenue"])} for r in rows]


# Operating window used for utilization %: 9am - 9pm = 12 hours/day.
OPEN_HOUR = 9
CLOSE_HOUR = 21
OPEN_HOURS_PER_DAY = CLOSE_HOUR - OPEN_HOUR


def _days_in_month(month: str) -> int:
    """Return the number of days in a yyyy-mm month string."""
    try:
        import calendar
        year, mon = (int(x) for x in month.split("-")[:2])
        return calendar.monthrange(year, mon)[1]
    except (ValueError, AttributeError):
        return 30


def room_utilization_pct(conn, month: str | None = None) -> list[dict]:
    """Return [{room, hours, available, pct}] utilization % per room.

    Available hours = days_in_month * OPEN_HOURS_PER_DAY (9am-9pm).
    pct is booked hours / available hours * 100, rounded to 1 decimal.
    """
    util = room_utilization(conn, month)
    days = _days_in_month(month) if month else 30
    available = days * OPEN_HOURS_PER_DAY
    out = []
    for row in util:
        hours = row["hours"]
        pct = round((hours / available) * 100, 1) if available else 0.0
        out.append({
            "room": row["room"],
            "hours": hours,
            "available": available,
            "pct": pct,
        })
    return out


def available_months(conn) -> list[str]:
    """Return sorted distinct yyyy-mm values seen across bookings and events."""
    rows = conn.execute(
        """SELECT substr(date, 1, 7) AS m FROM bookings
           UNION
           SELECT substr(date, 1, 7) AS m FROM events"""
    ).fetchall()
    months = sorted({r["m"] for r in rows if r["m"]})
    return months


def revenue_by_month(conn) -> list[dict]:
    """Return [{month, revenue}] of paid revenue for every month, sorted."""
    rows = conn.execute(
        """SELECT substr(date, 1, 7) AS month,
                  COALESCE(SUM(amount), 0) AS revenue
           FROM bookings
           WHERE payment_status = 'paid'
           GROUP BY month
           ORDER BY month"""
    ).fetchall()
    return [{"month": r["month"], "revenue": float(r["revenue"])} for r in rows]


def find_conflicting_bookings(conn, room_id, date, start_time, end_time) -> list[dict]:
    """Return existing bookings on the same room/date whose time overlaps.

    Overlap is strict: two intervals [s1,e1) and [s2,e2) overlap when
    s1 < e2 AND s2 < e1. Bookings missing times are ignored (can't compare).
    """
    if not room_id or not date or not start_time or not end_time:
        return []
    rows = conn.execute(
        """SELECT id, customer_name, date, start_time, end_time
           FROM bookings
           WHERE room_id = ? AND date = ?
                 AND start_time IS NOT NULL AND end_time IS NOT NULL""",
        (room_id, date),
    ).fetchall()
    conflicts = []
    for r in rows:
        if start_time < r["end_time"] and r["start_time"] < end_time:
            conflicts.append(dict(r))
    return conflicts


# ---------------------------------------------------------------------------
# Whitelisted query-plan dispatcher (used by Ask AI)
# ---------------------------------------------------------------------------
# Maps a validated plan "metric" to one fixed reader above. The model never
# emits SQL; it only picks a metric/month/room from a constrained set.
METRIC_DISPATCH = {
    "total_bookings": lambda conn, p: total_bookings(conn, p.get("month")),
    "total_revenue": lambda conn, p: total_revenue(conn, p.get("month")),
    "pending_payments": lambda conn, p: pending_payments(conn, p.get("month")),
    "room_utilization": lambda conn, p: room_utilization(conn, p.get("month")),
    "events": lambda conn, p: upcoming_events(conn, p.get("month")),
    "events_by_organizer": lambda conn, p: events_by_organizer(conn, p.get("month")),
}


def run_query_plan(conn, plan: dict):
    """Execute a validated query plan against the whitelisted readers.

    `plan` is a dict with keys: metric, group_by, month, room.
    Returns the reader's result (scalar or list of dicts). If a `room` filter
    is present for a per-room metric, the result is narrowed in Python.
    Raises ValueError if the metric is not whitelisted.
    """
    metric = plan.get("metric")
    if metric not in METRIC_DISPATCH:
        raise ValueError(f"Unknown metric: {metric!r}")

    result = METRIC_DISPATCH[metric](conn, plan)

    room = plan.get("room")
    if room and isinstance(result, list):
        result = [r for r in result if str(r.get("room", "")).lower() == room.lower()]
    return result


if __name__ == "__main__":
    init_db()
    print(f"Initialized schema in {DB_PATH}")
