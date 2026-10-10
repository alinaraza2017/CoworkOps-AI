"""CoworkOps AI — Streamlit application.

Pages:
  * Intake    — paste messy text, extract with AI, review/edit, save.
  * Dashboard — month-wise metrics, charts and tables.
  * Ask AI    — (wired separately)
  * Rooms     — (wired separately)

Run:  streamlit run app.py
"""

from __future__ import annotations

import os
from datetime import date, datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import ai
import db
import seed

load_dotenv()

st.set_page_config(page_title="CoworkOps AI", page_icon="🏢", layout="wide")


# ---------------------------------------------------------------------------
# One-time startup: ensure DB exists and is seeded.
# ---------------------------------------------------------------------------
@st.cache_resource
def _bootstrap() -> bool:
    db.init_db()
    conn = db.get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"]
    finally:
        conn.close()
    if n == 0:
        seed.seed()
    return True


_bootstrap()


def get_conn():
    return db.get_conn()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def pkr(amount) -> str:
    """Format a number as PKR with thousands separators."""
    try:
        return f"PKR {float(amount):,.0f}"
    except (TypeError, ValueError):
        return "PKR 0"


def _blank_record() -> dict:
    return {k: None for k in ai.OUTPUT_KEYS}


# ===========================================================================
# INTAKE PAGE
# ===========================================================================
RECORD_TYPE_OPTIONS = ["booking", "event", "payment"]
PAYMENT_STATUS_OPTIONS = ["paid", "pending", "unknown"]

INTAKE_PLACEHOLDER = (
    "e.g. Booked Conference Hall for Ayesha tomorrow 2pm to 4pm, Rs 7000 paid.\n"
    "or:  PyData meetup on 18th Oct in Event Space, organizer PyData Karachi, 6pm."
)


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except (ValueError, TypeError):
        return None


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _save_record(rec: dict, room_name: str | None) -> str:
    """Persist the reviewed record to the right table. Returns a message."""
    conn = get_conn()
    try:
        room_id = db.get_room_id_by_name(conn, room_name)
        rt = rec["record_type"]
        d = rec.get("date")
        start = rec.get("start_time")
        end = rec.get("end_time")
        amount = rec.get("amount_pkr") or 0
        status = rec.get("payment_status") or "pending"
        if status == "unknown":
            status = "pending"

        if rt == "booking":
            bid = db.insert_booking(
                conn, rec.get("name"), room_id, d, start, end, amount, status,
            )
            if status == "paid" and amount:
                db.insert_payment(conn, bid, amount, "paid", d or date.today().isoformat())
            return f"Booking saved (#{bid}) for {rec.get('name') or 'guest'}."

        if rt == "event":
            eid = db.insert_event(
                conn, rec.get("event_name") or rec.get("name") or "Event",
                rec.get("name"), room_id, d, start, end,
            )
            return f"Event saved (#{eid}): {rec.get('event_name') or 'Event'}."

        if rt == "payment":
            pid = db.insert_payment(
                conn, None, amount, status if status != "unknown" else "paid",
                d or date.today().isoformat(),
            )
            return f"Payment saved (#{pid}): {pkr(amount)}."

        return "Unknown record type; nothing saved."
    finally:
        conn.close()


def page_intake():
    st.header("📥 Smart Intake")
    st.caption(
        "Paste a messy message (WhatsApp text, a note). The AI extracts the "
        "details — you review and edit before saving."
    )

    # --- Text + extract button ------------------------------------------------
    text = st.text_area(
        "Paste message", height=140, placeholder=INTAKE_PLACEHOLDER, key="intake_text",
    )
    extract_clicked = st.button("✨ Extract with AI", type="primary")

    if extract_clicked:
        if not text or not text.strip():
            st.warning("Please paste a message first.")
        else:
            with st.spinner("Reading the message with the AI model…"):
                try:
                    rec = ai.extract_record(text, today=date.today())
                    st.session_state["intake_record"] = rec
                    st.session_state["intake_saved"] = False
                    st.success("Extracted! Review the fields below and save.")
                except ai.AIError as exc:
                    st.session_state.pop("intake_record", None)
                    st.error(f"Couldn't extract: {exc}")

    rec = st.session_state.get("intake_record")
    if not rec:
        return

    # --- Editable, pre-filled form -------------------------------------------
    conn = get_conn()
    try:
        room_names = [r["name"] for r in db.list_rooms(conn)]
    finally:
        conn.close()

    st.subheader("Review & edit")
    with st.form("intake_form"):
        c1, c2 = st.columns(2)
        with c1:
            rt_default = rec.get("record_type") if rec.get("record_type") in RECORD_TYPE_OPTIONS else "booking"
            record_type = st.selectbox(
                "Record type", RECORD_TYPE_OPTIONS,
                index=RECORD_TYPE_OPTIONS.index(rt_default),
            )
            name = st.text_input("Name (customer / organizer)", value=rec.get("name") or "")

            # Room select, allowing the extracted room even if unseeded.
            room_choices = ["(none)"] + room_names
            extracted_room = rec.get("room")
            if extracted_room and extracted_room not in room_names:
                room_choices.append(extracted_room)
            room_index = room_choices.index(extracted_room) if extracted_room in room_choices else 0
            room = st.selectbox("Room", room_choices, index=room_index)

            event_name = st.text_input("Event name", value=rec.get("event_name") or "")

        with c2:
            d_val = _parse_date(rec.get("date"))
            use_date = st.checkbox("Has date", value=d_val is not None)
            date_in = st.date_input("Date", value=d_val or date.today(), disabled=not use_date)

            t_start = _parse_time(rec.get("start_time"))
            t_end = _parse_time(rec.get("end_time"))
            start_in = st.text_input("Start time (HH:MM)", value=rec.get("start_time") or "")
            end_in = st.text_input("End time (HH:MM)", value=rec.get("end_time") or "")

            amount_in = st.number_input(
                "Amount (PKR)", min_value=0.0, step=500.0,
                value=float(rec.get("amount_pkr") or 0),
            )
            status_in = st.selectbox(
                "Payment status", PAYMENT_STATUS_OPTIONS,
                index=PAYMENT_STATUS_OPTIONS.index(
                    rec.get("payment_status") if rec.get("payment_status") in PAYMENT_STATUS_OPTIONS else "unknown"
                ),
            )

        notes_in = st.text_area("Notes", value=rec.get("notes") or "")

        save_clicked = st.form_submit_button("💾 Save", type="primary")

    # Override lives outside the form so it is reactive between save attempts.
    acknowledge = st.checkbox(
        "Conflict override: I understand there may be a double-booking — save anyway",
        key="intake_override",
    )

    if save_clicked:
        reviewed = {
            "record_type": record_type,
            "name": name or None,
            "room": None if room == "(none)" else room,
            "date": date_in.isoformat() if use_date else None,
            "start_time": start_in.strip() or None,
            "end_time": end_in.strip() or None,
            "amount_pkr": amount_in or None,
            "payment_status": status_in,
            "event_name": event_name or None,
            "notes": notes_in or None,
        }

        # --- Double-booking detection (bookings only) --------------------------
        conflicts = []
        if reviewed["record_type"] == "booking" and reviewed["room"]:
            conn = get_conn()
            try:
                room_id = db.get_room_id_by_name(conn, reviewed["room"])
                conflicts = db.find_conflicting_bookings(
                    conn, room_id, reviewed["date"],
                    reviewed["start_time"], reviewed["end_time"],
                )
            finally:
                conn.close()

        if conflicts and not acknowledge:
            st.warning(
                f"⚠️ Possible double-booking: **{reviewed['room']}** is already "
                f"booked on {reviewed['date']} during this time."
            )
            st.dataframe(
                pd.DataFrame(conflicts)[
                    ["customer_name", "date", "start_time", "end_time"]
                ],
                hide_index=True, use_container_width=True,
            )
            st.info("Tick 'save anyway' above and press Save again to override.")
        else:
            msg = _save_record(reviewed, reviewed["room"])
            st.session_state.pop("intake_record", None)
            st.success("✅ " + msg)
            st.balloons()


# ===========================================================================
# DASHBOARD PAGE
# ===========================================================================
def _month_label(m: str) -> str:
    try:
        return datetime.strptime(m, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return m


def page_dashboard():
    st.header("📊 Month-wise Dashboard")

    conn = get_conn()
    try:
        months = db.available_months(conn)
    finally:
        conn.close()

    if not months:
        st.info("No data yet. Add records on the Intake page.")
        return

    # Default to October 2026 when present, else the latest month.
    default_month = "2026-10" if "2026-10" in months else months[-1]
    month = st.selectbox(
        "Month", months, index=months.index(default_month),
        format_func=_month_label,
    )

    conn = get_conn()
    try:
        n_bookings = db.total_bookings(conn, month)
        revenue = db.total_revenue(conn, month)
        pending = db.pending_payments(conn, month)
        n_events = db.total_events(conn, month)
        rev_room = db.revenue_per_room(conn, month)
        util = db.room_utilization_pct(conn, month)
        events = db.upcoming_events(conn, month)
        rev_month = db.revenue_by_month(conn)
    finally:
        conn.close()

    # --- Metric cards ---------------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total bookings", n_bookings)
    c2.metric("Total revenue", pkr(revenue))
    c3.metric("Pending payments", pkr(pending))
    c4.metric("Events", n_events)

    st.divider()

    # --- Revenue per room (bar) + utilization % -------------------------------
    left, right = st.columns(2)

    with left:
        st.subheader("Revenue per room")
        df_rev = pd.DataFrame(rev_room)
        if not df_rev.empty and df_rev["revenue"].sum() > 0:
            st.bar_chart(df_rev.set_index("room")["revenue"], color="#2E86DE")
        else:
            st.caption("No paid revenue recorded this month.")

    with right:
        st.subheader("Room utilization")
        st.caption("Booked hours ÷ available hours (open 9am–9pm).")
        df_util = pd.DataFrame(util)
        if not df_util.empty:
            df_show = df_util.rename(
                columns={"room": "Room", "hours": "Booked hrs",
                         "available": "Available hrs", "pct": "Utilization %"}
            )
            st.dataframe(
                df_show,
                hide_index=True, use_container_width=True,
                column_config={
                    "Utilization %": st.column_config.ProgressColumn(
                        "Utilization %", format="%.1f%%", min_value=0, max_value=100,
                    ),
                },
            )
        else:
            st.caption("No rooms found.")

    st.divider()

    # --- Upcoming events table ------------------------------------------------
    st.subheader("Upcoming events")
    if events:
        df_ev = pd.DataFrame(events)[
            ["date", "event_name", "organizer", "room", "start_time", "end_time"]
        ].rename(columns={
            "date": "Date", "event_name": "Event", "organizer": "Organizer",
            "room": "Room", "start_time": "Start", "end_time": "End",
        })
        st.dataframe(df_ev, hide_index=True, use_container_width=True)
    else:
        st.caption("No events scheduled this month.")

    st.divider()

    # --- Month-over-month revenue (line) --------------------------------------
    st.subheader("Revenue trend (month over month)")
    df_mom = pd.DataFrame(rev_month)
    if not df_mom.empty:
        df_mom["label"] = df_mom["month"].map(_month_label)
        st.line_chart(df_mom.set_index("label")["revenue"], color="#27AE60")
    else:
        st.caption("Not enough data for a trend yet.")


# ===========================================================================
# Placeholder pages (implemented separately)
# ===========================================================================
EXAMPLE_QUESTIONS = [
    "Which room made the most revenue in October 2026?",
    "Which room was used least in October 2026?",
    "How much money is still pending in September 2026?",
    "How many bookings did we have in October 2026?",
    "Which organizer ran the most events in October 2026?",
]


def _answer_and_store(question: str):
    """Run answer_question and append the Q/A to the chat history."""
    st.session_state.setdefault("chat", [])
    st.session_state["chat"].append({"role": "user", "content": question})
    try:
        with st.spinner("Thinking…"):
            answer = ai.answer_question(question)
    except ai.AIError as exc:
        answer = f"⚠️ {exc}"
    st.session_state["chat"].append({"role": "assistant", "content": answer})


def page_ask_ai():
    st.header("💬 Ask AI")
    st.caption(
        "Ask about revenue, bookings, room utilization, pending payments or "
        "events. Answers come only from your saved data."
    )

    st.session_state.setdefault("chat", [])

    # --- Example questions (clickable) ---------------------------------------
    st.markdown("**Try an example:**")
    cols = st.columns(len(EXAMPLE_QUESTIONS))
    for i, q in enumerate(EXAMPLE_QUESTIONS):
        if cols[i].button(q, key=f"ex_{i}"):
            _answer_and_store(q)
            st.rerun()

    # --- Chat history ---------------------------------------------------------
    for msg in st.session_state["chat"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # --- Chat input -----------------------------------------------------------
    prompt = st.chat_input("Ask a question about your coworking data…")
    if prompt:
        _answer_and_store(prompt)
        st.rerun()

    if st.session_state["chat"]:
        if st.button("Clear chat"):
            st.session_state["chat"] = []
            st.rerun()


def page_rooms():
    st.header("🚪 Rooms")
    conn = get_conn()
    try:
        rooms = [dict(r) for r in db.list_rooms(conn)]
    finally:
        conn.close()
    df = pd.DataFrame(rooms).rename(columns={
        "name": "Room", "capacity": "Capacity", "hourly_rate": "Hourly rate (PKR)",
    })
    st.dataframe(df[["Room", "Capacity", "Hourly rate (PKR)"]],
                 hide_index=True, use_container_width=True)


# ===========================================================================
# Routing
# ===========================================================================
PAGES = {
    "Intake": page_intake,
    "Dashboard": page_dashboard,
    "Ask AI": page_ask_ai,
    "Rooms": page_rooms,
}


def main():
    st.sidebar.title("🏢 CoworkOps AI")
    st.sidebar.caption(f"Model: `{os.getenv('OLLAMA_MODEL', ai.DEFAULT_MODEL)}`")
    choice = st.sidebar.radio("Navigate", list(PAGES.keys()))
    PAGES[choice]()


if __name__ == "__main__":
    main()
