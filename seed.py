"""Seed CoworkOps AI with 5 rooms and realistic demo data.

Creates:
  * 5 rooms (name, capacity, hourly rate in PKR)
  * ~40 bookings across September and October 2026 (some pending)
  * payment rows for the 'paid' bookings
  * 10 events across the two months

Idempotent: if rooms already exist, seeding is skipped so re-running does not
duplicate data.
"""

from __future__ import annotations

import random

import db

RANDOM_SEED = 42

ROOMS = [
    # name, capacity, hourly_rate (PKR)
    ("Focus Room", 4, 800.0),
    ("Team Room", 8, 1500.0),
    ("Conference Hall", 20, 3500.0),
    ("Quiet Pod", 2, 500.0),
    ("Event Space", 40, 6000.0),
]

CUSTOMERS = [
    "Ayesha Khan", "Bilal Ahmed", "Sana Malik", "Usman Tariq", "Hira Shah",
    "Zain Abbas", "Mariam Qureshi", "Fahad Iqbal", "Nida Raza", "Omar Farooq",
    "Rabia Aslam", "Kashif Mehmood", "Sadia Noor", "Hamza Butt", "Iqra Javed",
]

ORGANIZERS = [
    "Startup Grind KHI", "PyData Karachi", "Women in Tech PK",
    "Freelancers Hub", "Design Collective", "DevOps Pakistan",
]

EVENT_NAMES = [
    "Founder Mixer", "Python Workshop", "Resume Clinic", "UX Portfolio Review",
    "Networking Night", "Pitch Practice", "Intro to Kubernetes",
    "Freelance Tax Session", "Design Systems Talk", "Open Mic Demo Day",
]

START_HOURS = [9, 10, 11, 13, 14, 15, 16]      # realistic slot starts
DURATIONS = [1, 2, 3, 4]                        # hours

# September 2026 has 30 days, October 2026 has 31 days.
MONTHS = [("2026-09", 30), ("2026-10", 31)]


def _already_seeded(conn) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()
    return row["n"] > 0


def seed(db_path: str | None = None) -> dict:
    """Seed the database. Returns a summary dict of what was inserted."""
    rng = random.Random(RANDOM_SEED)
    db.init_db(db_path)
    conn = db.get_conn(db_path)
    try:
        if _already_seeded(conn):
            print("Database already seeded; skipping.")
            return _summary(conn)

        # Rooms
        room_ids = []
        for name, cap, rate in ROOMS:
            rid = db.insert_room(conn, name, cap, rate)
            # insert_room uses INSERT OR IGNORE, so fetch id to be safe
            if not rid:
                rid = db.get_room_id_by_name(conn, name)
            room_ids.append(rid)

        rate_by_id = {rid: ROOMS[i][2] for i, rid in enumerate(room_ids)}

        # Bookings (~40 total, split across the two months)
        booking_counts = {"2026-09": 20, "2026-10": 20}
        n_bookings = 0
        n_paid = 0
        n_pending = 0
        for month, days in MONTHS:
            for _ in range(booking_counts[month]):
                room_id = rng.choice(room_ids)
                day = rng.randint(1, days)
                start_h = rng.choice(START_HOURS)
                dur = rng.choice(DURATIONS)
                end_h = min(start_h + dur, 21)
                hours = end_h - start_h

                amount = round(rate_by_id[room_id] * hours, 0)
                # ~30% pending, 70% paid
                status = "pending" if rng.random() < 0.3 else "paid"

                date = f"{month}-{day:02d}"
                start_time = f"{start_h:02d}:00"
                end_time = f"{end_h:02d}:00"
                customer = rng.choice(CUSTOMERS)

                booking_id = db.insert_booking(
                    conn, customer, room_id, date, start_time, end_time,
                    amount, status,
                )
                n_bookings += 1

                if status == "paid":
                    db.insert_payment(conn, booking_id, amount, "paid", date)
                    n_paid += 1
                else:
                    n_pending += 1

        # Events (10 total, 5 per month)
        n_events = 0
        for i in range(10):
            month, days = MONTHS[i % 2]
            room_id = rng.choice(room_ids)
            day = rng.randint(1, days)
            start_h = rng.choice([17, 18, 19])      # events tend to be evenings
            dur = rng.choice([1, 2, 3])
            end_h = min(start_h + dur, 22)
            date = f"{month}-{day:02d}"

            db.insert_event(
                conn,
                EVENT_NAMES[i % len(EVENT_NAMES)],
                rng.choice(ORGANIZERS),
                room_id,
                date,
                f"{start_h:02d}:00",
                f"{end_h:02d}:00",
            )
            n_events += 1

        print(
            f"Seeded: 5 rooms, {n_bookings} bookings "
            f"({n_paid} paid / {n_pending} pending), {n_events} events."
        )
        return _summary(conn)
    finally:
        conn.close()


def _summary(conn) -> dict:
    def count(table):
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    return {
        "rooms": count("rooms"),
        "bookings": count("bookings"),
        "payments": count("payments"),
        "events": count("events"),
    }


if __name__ == "__main__":
    seed()
