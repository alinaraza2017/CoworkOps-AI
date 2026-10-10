"""AI layer for CoworkOps AI.

Public functions:
    extract_record(text, today=None, model_call=None) -> dict
    answer_question(question, ...) -> str   (implemented separately)

Design / safety notes:
  * The model is asked to return ONLY JSON. We additionally strip any
    <think>...</think> blocks (Qwen3 "thinking" output) before parsing.
  * We request Ollama structured output via format="json" and disable Qwen3
    thinking with think=False / /no_think, where supported.
  * Output is validated with pydantic when available, otherwise with manual
    checks. On any failure we retry exactly once, then raise AIError.
  * The ollama library is imported lazily inside the model-call helper so this
    module imports fine (and tests run) in environments without ollama/live
    server. Tests inject a fake `model_call` to stay fully offline.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

try:  # pydantic is preferred but optional
    from pydantic import BaseModel, field_validator  # type: ignore
    _HAS_PYDANTIC = True
except Exception:  # pragma: no cover - exercised only when pydantic missing
    _HAS_PYDANTIC = False


DEFAULT_MODEL = "qwen3:4b"

RECORD_TYPES = {"booking", "event", "payment"}
PAYMENT_STATUSES = {"paid", "pending", "unknown"}

# Keys the extraction must contain (filled with None when unknown).
OUTPUT_KEYS = [
    "record_type", "name", "room", "date", "start_time", "end_time",
    "amount_pkr", "payment_status", "event_name", "notes",
]


class AIError(Exception):
    """Raised when the model is unreachable or output can't be validated."""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are CoworkOps AI, an assistant that extracts coworking-space "
    "operations data from messy, informal messages (WhatsApp texts, notes). "
    "Messages may be in English, Roman Urdu, or a mix. You return ONLY a "
    "single JSON object and nothing else: no markdown, no code fences, no "
    "explanations, no <think> text.\n\n"
    "Classify each message as one of: 'booking' (someone reserves a room), "
    "'payment' (money received or owed), or 'event' (a gathering/workshop/"
    "meetup with an organizer).\n\n"
    "Output JSON schema (every key must be present):\n"
    "{\n"
    '  "record_type": "booking" | "event" | "payment",\n'
    '  "name": string | null,            // customer or payer name\n'
    '  "room": string | null,            // room name mentioned\n'
    '  "date": "YYYY-MM-DD" | null,\n'
    '  "start_time": "HH:MM" | null,     // 24-hour\n'
    '  "end_time": "HH:MM" | null,       // 24-hour\n'
    '  "amount_pkr": number | null,      // numeric PKR amount, no symbols\n'
    '  "payment_status": "paid" | "pending" | "unknown",\n'
    '  "event_name": string | null,      // only for events\n'
    '  "notes": string | null            // short free-text leftovers\n'
    "}\n\n"
    "Rules:\n"
    "- If a field is not present or cannot be determined, use null (or "
    "\"unknown\" for payment_status). Never invent values.\n"
    "- Convert all times to 24-hour HH:MM (e.g. '5pm' -> '17:00').\n"
    "- Amounts: extract the number only. '5k' means 5000. Strip 'Rs', 'PKR', "
    "commas.\n"
    "- Resolve relative dates ('today', 'tomorrow', 'Friday', 'next Monday') "
    "using the TODAY date provided in the user message. Output absolute "
    "YYYY-MM-DD.\n"
    "- Roman-Urdu hints: 'kal' = tomorrow, 'aaj' = today, 'parso' = day after "
    "tomorrow, 'baje' = o'clock, 'ada'/'paid' = paid, 'baqi'/'reh gaye'/"
    "'pending' = pending.\n"
)

# Three few-shot examples (one of them messy Roman-Urdu/English).
FEW_SHOT = [
    (
        "TODAY is 2026-10-10 (Saturday).\n"
        "MESSAGE: Booked Conference Hall for Ayesha tomorrow 2pm to 4pm, "
        "Rs 7000 paid.",
        {
            "record_type": "booking",
            "name": "Ayesha",
            "room": "Conference Hall",
            "date": "2026-10-11",
            "start_time": "14:00",
            "end_time": "16:00",
            "amount_pkr": 7000,
            "payment_status": "paid",
            "event_name": None,
            "notes": None,
        },
    ),
    (
        "TODAY is 2026-10-10 (Saturday).\n"
        "MESSAGE: PyData meetup on 18th Oct in Event Space, organizer PyData "
        "Karachi, 6pm onwards.",
        {
            "record_type": "event",
            "name": None,
            "room": "Event Space",
            "date": "2026-10-18",
            "start_time": "18:00",
            "end_time": None,
            "amount_pkr": None,
            "payment_status": "unknown",
            "event_name": "PyData meetup",
            "notes": "organizer PyData Karachi",
        },
    ),
    (
        # Messy Roman-Urdu / English mix.
        "TODAY is 2026-09-20 (Sunday).\n"
        "MESSAGE: bilal bhai ne Team Room liya tha kal 11 baje se 1 baje, "
        "3000 me se 1500 ada baqi pending",
        {
            "record_type": "booking",
            "name": "Bilal",
            "room": "Team Room",
            "date": "2026-09-21",
            "start_time": "11:00",
            "end_time": "13:00",
            "amount_pkr": 3000,
            "payment_status": "pending",
            "event_name": None,
            "notes": "1500 paid, 1500 pending",
        },
    ),
]


def _weekday_name(d: date) -> str:
    return d.strftime("%A")


def build_user_prompt(text: str, today: date) -> str:
    """Build the user-turn prompt with few-shots and the TODAY anchor."""
    parts = ["Here are examples of the expected behavior:\n"]
    for msg, obj in FEW_SHOT:
        parts.append(msg)
        parts.append("JSON: " + json.dumps(obj, ensure_ascii=False))
        parts.append("")
    parts.append("Now extract from this message. Return ONLY the JSON object.")
    parts.append(f"TODAY is {today.isoformat()} ({_weekday_name(today)}).")
    parts.append(f"MESSAGE: {text.strip()}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Model call (lazy ollama import) + cleaning
# ---------------------------------------------------------------------------
def _default_model_call(system: str, user: str, model: str) -> str:
    """Call Ollama with JSON format and Qwen3 thinking disabled.

    Imported lazily so this module works without ollama installed. Returns the
    raw string content from the model.
    """
    import ollama  # lazy

    options = {"temperature": 0}
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            # /no_think is a Qwen3 toggle; harmless to other models.
            {"role": "user", "content": user + "\n/no_think"},
        ],
        format="json",          # Ollama structured output: force JSON
        options=options,
    )
    try:
        # Newer ollama clients accept think=False to disable reasoning tokens.
        resp = ollama.chat(think=False, **kwargs)
    except TypeError:
        resp = ollama.chat(**kwargs)
    return resp["message"]["content"]


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _clean(raw: str) -> str:
    """Strip <think> blocks, code fences, and isolate the JSON object."""
    if not raw:
        return ""
    text = _THINK_RE.sub("", raw)
    # Handle an unterminated <think> with no closing tag: drop the think prefix
    # but keep anything after it (e.g. a trailing JSON object).
    if re.search(r"<think>", text, flags=re.IGNORECASE):
        brace = text.find("{")
        if brace != -1:
            text = text[brace:]  # keep from the first JSON brace onward
        else:
            text = re.sub(r"<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    # Remove markdown code fences if present.
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    # Isolate the outermost {...} if there is surrounding prose.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _coerce_amount(value):
    """Coerce an amount into a number or None. Handles '5k', 'Rs 3,000'."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip().lower().replace(",", "")
    s = s.replace("rs", "").replace("pkr", "").strip()
    mult = 1
    if s.endswith("k"):
        mult = 1000
        s = s[:-1].strip()
    try:
        num = float(s) * mult
        return int(num) if num.is_integer() else num
    except ValueError:
        return None


def _valid_date(value) -> bool:
    if value is None:
        return True
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _valid_time(value) -> bool:
    if value is None:
        return True
    try:
        datetime.strptime(str(value), "%H:%M")
        return True
    except ValueError:
        return False


if _HAS_PYDANTIC:

    class ExtractionRecord(BaseModel):  # type: ignore[misc]
        record_type: str
        name: str | None = None
        room: str | None = None
        date: str | None = None
        start_time: str | None = None
        end_time: str | None = None
        amount_pkr: float | None = None
        payment_status: str = "unknown"
        event_name: str | None = None
        notes: str | None = None

        @field_validator("record_type")
        @classmethod
        def _rt(cls, v):
            if v not in RECORD_TYPES:
                raise ValueError(f"record_type must be one of {RECORD_TYPES}")
            return v

        @field_validator("payment_status")
        @classmethod
        def _ps(cls, v):
            if v not in PAYMENT_STATUSES:
                raise ValueError(f"payment_status must be one of {PAYMENT_STATUSES}")
            return v

        @field_validator("date")
        @classmethod
        def _d(cls, v):
            if not _valid_date(v):
                raise ValueError("date must be YYYY-MM-DD or null")
            return v

        @field_validator("start_time", "end_time")
        @classmethod
        def _t(cls, v):
            if not _valid_time(v):
                raise ValueError("time must be HH:MM or null")
            return v


def _validate(data: dict) -> dict:
    """Validate + normalize a parsed dict into the canonical record.

    Raises AIError (via ValueError) when the data is not a valid record.
    """
    if not isinstance(data, dict):
        raise ValueError("model output is not a JSON object")

    # Normalize before validation.
    data = dict(data)
    data.setdefault("payment_status", "unknown")
    if data.get("payment_status") in (None, ""):
        data["payment_status"] = "unknown"
    data["amount_pkr"] = _coerce_amount(data.get("amount_pkr"))
    for key in OUTPUT_KEYS:
        data.setdefault(key, None)

    if _HAS_PYDANTIC:
        rec = ExtractionRecord(**{k: data.get(k) for k in OUTPUT_KEYS})
        out = rec.model_dump()
    else:
        # Manual validation fallback.
        if data["record_type"] not in RECORD_TYPES:
            raise ValueError(f"record_type must be one of {RECORD_TYPES}")
        if data["payment_status"] not in PAYMENT_STATUSES:
            raise ValueError(f"payment_status must be one of {PAYMENT_STATUSES}")
        if not _valid_date(data["date"]):
            raise ValueError("date must be YYYY-MM-DD or null")
        if not _valid_time(data["start_time"]) or not _valid_time(data["end_time"]):
            raise ValueError("times must be HH:MM or null")
        out = {k: data.get(k) for k in OUTPUT_KEYS}

    # Ensure amount stays numeric/None after model_dump.
    out["amount_pkr"] = _coerce_amount(out.get("amount_pkr"))
    # Keep only the canonical key set, in order.
    return {k: out.get(k) for k in OUTPUT_KEYS}


# ---------------------------------------------------------------------------
# Public: extract_record
# ---------------------------------------------------------------------------
def extract_record(text: str, today: date | None = None, model_call=None) -> dict:
    """Extract a structured coworking record from messy text.

    Args:
        text: the pasted message.
        today: date used to resolve relative dates (defaults to date.today()).
        model_call: optional callable(system, user, model) -> raw str, used to
            inject a fake model in tests. Defaults to the live Ollama call.

    Returns:
        dict with the canonical OUTPUT_KEYS.

    Raises:
        AIError: on empty input, model-unreachable, or validation failure after
            one retry.
    """
    if not text or not text.strip():
        raise AIError("Please enter a message to extract.")

    today = today or date.today()
    model = os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
    call = model_call or _default_model_call

    system = SYSTEM_PROMPT
    user = build_user_prompt(text, today)

    last_err: Exception | None = None
    for attempt in range(2):  # initial try + one retry
        try:
            raw = call(system, user, model)
        except Exception as exc:  # model unreachable / client error
            raise AIError(f"Could not reach the AI model: {exc}") from exc

        try:
            parsed = json.loads(_clean(raw))
            return _validate(parsed)
        except Exception as exc:
            last_err = exc
            # Tighten the instruction for the retry.
            user = (
                user
                + "\n\nYour previous reply was invalid. Return ONLY a valid "
                "JSON object matching the schema. No prose, no <think>."
            )

    raise AIError(
        "The AI returned data we couldn't understand. Please try rephrasing "
        f"the message. ({last_err})"
    )


# ===========================================================================
# Ask AI: question -> JSON query plan -> whitelisted query -> short answer
# ===========================================================================

# User-facing metric vocabulary (as specified for the plan).
PLAN_METRICS = {"revenue", "bookings", "utilization", "pending_payments", "events"}
PLAN_GROUP_BY = {"room", "month", "organizer", "none"}

PLAN_SYSTEM_PROMPT = (
    "You are CoworkOps AI's query planner. The user asks a question in plain "
    "English about a coworking space's data. You translate it into a strict "
    "JSON query plan and return ONLY that JSON object — no prose, no markdown, "
    "no <think> text.\n\n"
    "Query plan schema (all keys required):\n"
    "{\n"
    '  "metric": "revenue" | "bookings" | "utilization" | "pending_payments" '
    '| "events" | null,\n'
    '  "group_by": "room" | "month" | "organizer" | "none",\n'
    '  "month": "YYYY-MM" | null,\n'
    '  "room": string | null\n'
    "}\n\n"
    "Rules:\n"
    "- Pick the single metric that best answers the question.\n"
    "- 'revenue' = money earned (paid). 'pending_payments' = money owed/unpaid.\n"
    "- 'utilization' = how much rooms were used (booked hours). 'bookings' = "
    "count of bookings. 'events' = events/meetups.\n"
    "- Use group_by 'room' for per-room questions (which room ...), 'organizer' "
    "for per-organizer event questions, 'month' for trends over months, else "
    "'none'.\n"
    "- Set month to the YYYY-MM the user names (e.g. 'October 2026' -> "
    '"2026-10"); null if no specific month.\n'
    "- Set room to the room name only if the user names one room; else null.\n"
    "- If the question is NOT about this data (revenue, bookings, utilization, "
    'pending payments, events), set "metric" to null. Do not guess.\n'
)

PLAN_FEW_SHOT = [
    ("Which room made the most revenue in October 2026?",
     {"metric": "revenue", "group_by": "room", "month": "2026-10", "room": None}),
    ("How much money is still pending this September?",
     {"metric": "pending_payments", "group_by": "none", "month": "2026-09", "room": None}),
    ("Which room was used least in October?",
     {"metric": "utilization", "group_by": "room", "month": "2026-10", "room": None}),
    ("What's the weather like today?",
     {"metric": None, "group_by": "none", "month": None, "room": None}),
]


def build_plan_prompt(question: str) -> str:
    parts = ["Examples:\n"]
    for q, plan in PLAN_FEW_SHOT:
        parts.append(f"Q: {q}")
        parts.append("PLAN: " + json.dumps(plan))
        parts.append("")
    parts.append("Now produce the PLAN for this question. Return ONLY JSON.")
    parts.append(f"Q: {question.strip()}")
    return "\n".join(parts)


def _default_plan_call(system: str, user: str, model: str) -> str:
    """Model call for the query planner (JSON format, thinking disabled)."""
    return _default_model_call(system, user, model)


def _validate_plan(data: dict) -> dict:
    """Validate + normalize a query plan. Raises ValueError if malformed."""
    if not isinstance(data, dict):
        raise ValueError("plan is not a JSON object")
    metric = data.get("metric")
    if metric is not None and metric not in PLAN_METRICS:
        raise ValueError(f"metric must be one of {PLAN_METRICS} or null")
    group_by = data.get("group_by") or "none"
    if group_by not in PLAN_GROUP_BY:
        raise ValueError(f"group_by must be one of {PLAN_GROUP_BY}")
    month = data.get("month")
    if month is not None and not re.match(r"^\d{4}-\d{2}$", str(month)):
        raise ValueError("month must be YYYY-MM or null")
    room = data.get("room")
    return {
        "metric": metric,
        "group_by": group_by,
        "month": month,
        "room": room if room else None,
    }


# Map the user-facing plan metric to the whitelisted db reader(s).
def _execute_plan(conn, plan: dict) -> dict:
    """Run the whitelisted query for a validated plan.

    Returns a result dict: {"kind": ..., "rows"/"value": ..., "plan": plan}.
    Never builds SQL from model text — only calls fixed db functions.
    """
    import db

    metric = plan["metric"]
    month = plan.get("month")
    room = plan.get("room")
    gb = plan.get("group_by")

    if metric == "revenue":
        if gb == "room" or room:
            rows = db.revenue_per_room(conn, month)
            if room:
                rows = [r for r in rows if r["room"].lower() == room.lower()]
            return {"kind": "rows", "field": "revenue", "rows": rows}
        if gb == "month":
            return {"kind": "rows", "field": "revenue",
                    "rows": db.revenue_by_month(conn)}
        return {"kind": "value", "field": "revenue",
                "value": db.total_revenue(conn, month)}

    if metric == "pending_payments":
        return {"kind": "value", "field": "pending",
                "value": db.pending_payments(conn, month)}

    if metric == "bookings":
        return {"kind": "value", "field": "bookings",
                "value": db.total_bookings(conn, month)}

    if metric == "utilization":
        rows = db.room_utilization_pct(conn, month)
        if room:
            rows = [r for r in rows if r["room"].lower() == room.lower()]
        return {"kind": "rows", "field": "utilization", "rows": rows}

    if metric == "events":
        if gb == "organizer":
            return {"kind": "rows", "field": "events_by_organizer",
                    "rows": db.events_by_organizer(conn, month)}
        return {"kind": "rows", "field": "events",
                "rows": db.upcoming_events(conn, month)}

    raise ValueError(f"Unsupported metric: {metric!r}")


def _month_label(month) -> str:
    if not month:
        return "all time"
    try:
        return datetime.strptime(str(month), "%Y-%m").strftime("%B %Y")
    except ValueError:
        return str(month)


def _pkr(n) -> str:
    try:
        return f"PKR {float(n):,.0f}"
    except (TypeError, ValueError):
        return "PKR 0"


def _template_answer(question: str, plan: dict, result: dict) -> str:
    """Deterministic natural-language answer built ONLY from returned numbers.

    Used as the phrasing fallback (and in tests) so answers never invent data.
    """
    when = _month_label(plan.get("month"))
    field = result.get("field")

    if result["kind"] == "value":
        v = result["value"]
        if field == "revenue":
            return f"Total paid revenue for {when} was {_pkr(v)}."
        if field == "pending":
            return f"Pending (unpaid) payments for {when} total {_pkr(v)}."
        if field == "bookings":
            return f"There were {int(v)} bookings in {when}."

    rows = result.get("rows", [])
    if field == "revenue":
        if not rows:
            return f"No paid revenue was recorded for {when}."
        top = max(rows, key=lambda r: r["revenue"])
        parts = ", ".join(f"{r['room']} {_pkr(r['revenue'])}" for r in rows)
        return (f"For {when}, revenue by room was: {parts}. "
                f"The top earner was {top['room']} with {_pkr(top['revenue'])}.")
    if field == "utilization":
        if not rows:
            return f"No bookings were recorded for {when}."
        least = min(rows, key=lambda r: r["hours"])
        most = max(rows, key=lambda r: r["hours"])
        return (f"For {when}, {most['room']} was used most "
                f"({most['hours']}h, {most['pct']}%) and {least['room']} least "
                f"({least['hours']}h, {least['pct']}%).")
    if field == "events_by_organizer":
        if not rows:
            return f"No events were recorded for {when}."
        parts = ", ".join(f"{r['organizer']} ({r['count']})" for r in rows)
        return f"Events by organizer for {when}: {parts}."
    if field == "events":
        if not rows:
            return f"No events are scheduled for {when}."
        names = ", ".join(
            f"{r.get('event_name')} on {r.get('date')}" for r in rows[:6]
        )
        return f"{len(rows)} event(s) in {when}: {names}."

    return "I don't have the data to answer that."


def _default_phrase_call(system: str, user: str, model: str) -> str:
    """Model call that phrases the final sentence from the computed numbers."""
    return _default_model_call(system, user, model)


PHRASE_SYSTEM_PROMPT = (
    "You are CoworkOps AI. You are given a user question and the ONLY data "
    "available to answer it (already computed from the database). Write one or "
    "two short, friendly sentences that answer the question using ONLY these "
    "numbers. Do not invent any value not present in the data. Do not output "
    "JSON or <think> text — just the sentence."
)


def answer_question(
    question: str,
    conn=None,
    plan_call=None,
    phrase_call=None,
    phrase_with_model: bool = True,
) -> str:
    """Answer a natural-language question from the database.

    Flow: question -> JSON query plan (validated, retry once) -> whitelisted
    query -> short answer phrased ONLY from the returned numbers.

    Args:
        question: the user's plain-English question.
        conn: optional sqlite connection (tests pass an in-memory one).
        plan_call / phrase_call: injectable model callables for tests.
        phrase_with_model: when False, use the deterministic template phrasing.

    Returns:
        A short answer string. If the question is not answerable from the data,
        returns a clear message rather than guessing.

    Raises:
        AIError: on empty input or model-unreachable.
    """
    if not question or not question.strip():
        raise AIError("Please type a question.")

    model = os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
    pcall = plan_call or _default_plan_call

    # 1) Question -> validated query plan (retry once).
    plan = None
    last_err: Exception | None = None
    user = build_plan_prompt(question)
    for _ in range(2):
        try:
            raw = pcall(PLAN_SYSTEM_PROMPT, user, model)
        except Exception as exc:
            raise AIError(f"Could not reach the AI model: {exc}") from exc
        try:
            plan = _validate_plan(json.loads(_clean(raw)))
            break
        except Exception as exc:
            last_err = exc
            plan = None
            user = user + ("\n\nYour previous reply was invalid. Return ONLY a "
                           "valid JSON plan. If unrelated to the data, set "
                           '"metric" to null.')

    if plan is None:
        return (
            "Sorry, I couldn't turn that into a question about your data. "
            "Try asking about revenue, bookings, utilization, pending "
            f"payments, or events. ({last_err})"
        )

    # 2) Unanswerable (not about the data) -> say so, don't guess.
    if plan.get("metric") is None:
        return (
            "I can only answer questions about this coworking space's data — "
            "revenue, bookings, room utilization, pending payments, or events. "
            "That question doesn't map to any of those."
        )

    # 3) Execute the whitelisted query.
    own_conn = False
    if conn is None:
        import db
        conn = db.get_conn()
        own_conn = True
    try:
        try:
            result = _execute_plan(conn, plan)
        except Exception as exc:
            return f"I couldn't run that query: {exc}"
    finally:
        if own_conn:
            conn.close()

    # 4) Phrase the answer using ONLY the returned numbers.
    template = _template_answer(question, plan, result)
    if not phrase_with_model:
        return template

    phrase = phrase_call or _default_phrase_call
    facts = json.dumps({"question": question, "plan": plan, "data": result},
                       ensure_ascii=False, default=str)
    try:
        raw = phrase(
            PHRASE_SYSTEM_PROMPT,
            f"QUESTION: {question}\nDATA (the only numbers you may use): {facts}\n"
            f"Reference answer (you may rephrase): {template}",
            model,
        )
        sentence = _clean_text(raw)
        return sentence or template
    except Exception:
        # If phrasing fails, fall back to the deterministic template.
        return template


def _clean_text(raw: str) -> str:
    """Strip <think> blocks and fences from a free-text (non-JSON) reply."""
    if not raw:
        return ""
    text = _THINK_RE.sub("", raw)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^```(?:\w+)?", "", text.strip()).strip()
    text = re.sub(r"```$", "", text).strip()
    return text.strip()
