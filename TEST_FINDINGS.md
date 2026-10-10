# CoworkOps AI — Test Findings

**Date run:** 2026-10-10
**Command:** `pytest -v tests/`
**Interpreter:** Python 3.11.15 (pyenv), pytest 9.1.1
**Result:** **40 passed, 0 failed** in ~0.06s

> Honest environment caveats are in [Known Limitations](#known-limitations) below. In short: the live Ollama model is **not** exercised (no server / no network in the sandbox) — all model calls are mocked. `pydantic` and `streamlit` are **not installed** here, so the extraction tests ran against the **manual-validation fallback** and the Streamlit UI is not covered by automated tests.

---

## 1. Database helpers (`tests/test_db.py`)

| Scenario | Input | Expected output | Actual outcome | Pass/Fail | Observed issues |
|---|---|---|---|---|---|
| Schema creates all tables | `db.SCHEMA` on in-memory DB | tables rooms, bookings, payments, events exist | all four present | **PASS** | — |
| List rooms + case-insensitive lookup | 2 seeded rooms; lookup "focus room", "No Such Room", `None` | ordered names; id for "focus room"; `None` for unknown/None | matched | **PASS** | — |
| Total bookings per month | Oct=3, Sep=1, all=4 | 3 / 1 / 4 | 3 / 1 / 4 | **PASS** | — |
| Total revenue counts paid only | Oct paid 1600+24000, pending 800 excluded | Oct=25600, Sep=18000 | 25600.0 / 18000.0 | **PASS** | — |
| Pending payments sum | Oct pending=800, Sep=0 | 800 / 0 | 800.0 / 0.0 | **PASS** | — |
| Room utilization (hours) | Focus 2h+1h, Event 4h (Oct) | Focus=3.0, Event=4.0 | 3.0 / 4.0 | **PASS** | — |
| Revenue per room (paid only) | Focus 1600 paid (+800 pending), Event 24000 | Focus=1600, Event=24000 | 1600.0 / 24000.0 | **PASS** | Pending correctly not counted |
| Utilization % available hours | Oct = 31 days × 12h (9am–9pm) | available=372; pct=3/372×100 | available=372, pct matches | **PASS** | Assumes every room open all 31 days |
| Total events + by organizer | Oct 3 events; PyData KHI 2, Startup Grind 1 | counts as stated | matched | **PASS** | — |
| Available months + revenue by month | Sep & Oct data | `["2026-09","2026-10"]`; Sep=18000, Oct=25600 | matched | **PASS** | — |
| Empty month returns zeroes | query month 2026-01 (no data) | 0 bookings, 0.0 revenue, `[]` events | matched | **PASS** | No crash on empty month |

## 2. Double-booking detection (`tests/test_db.py`)

| Scenario | Input | Expected output | Actual outcome | Pass/Fail | Observed issues |
|---|---|---|---|---|---|
| Overlapping slot flagged | Focus 2026-10-02, new 11:00–13:00 vs existing 10:00–12:00 | 1 conflict (customer "A") | 1 conflict, "A" | **PASS** | — |
| Adjacent slot not a conflict | new 12:00–13:00 vs existing 10:00–12:00 | 0 conflicts | `[]` | **PASS** | Boundary treated as non-overlap (half-open interval) |
| Different room, same time | Event Space 10:00–12:00 | 0 conflicts | `[]` | **PASS** | — |
| Different day, same time | Focus 2026-10-09 10:00–12:00 | 0 conflicts | `[]` | **PASS** | — |
| Missing time / room | start_time `None`; room_id `None` | 0 conflicts (cannot compare) | `[]` | **PASS** | Silently skips — see limitations |
| Fully contained slot | Focus new 10:30–11:00 inside 10:00–12:00 | 1 conflict | 1 conflict | **PASS** | — |

## 3. Query plan execution (`tests/test_db.py` + `tests/test_answer.py`)

| Scenario | Input | Expected output | Actual outcome | Pass/Fail | Observed issues |
|---|---|---|---|---|---|
| Dispatcher scalar metric | plan `{metric: total_bookings/total_revenue, month: 2026-10}` | 3 / 25600.0 | 3 / 25600.0 | **PASS** | — |
| Dispatcher room filter | plan `{metric: room_utilization, room: Event Space}` | `[{room: Event Space, hours: 4.0}]` | exact match | **PASS** | — |
| Dispatcher rejects unknown metric | plan `{metric: drop_table}` | raises `ValueError` | `ValueError` raised | **PASS** | Whitelist blocks non-metrics |
| answer: top revenue room | plan revenue/room/2026-10 | names Event Space, PKR 24,000 | "...top earner was Event Space with PKR 24,000." | **PASS** | — |
| answer: total revenue value | plan revenue/none/2026-10 | PKR 25,600 (paid only) | "...was PKR 25,600." | **PASS** | — |
| answer: pending payments | plan pending_payments/2026-10 | PKR 800 | "...total PKR 800." | **PASS** | — |
| answer: least-used room | plan utilization/room/2026-10 | Focus least, Event most | "Event Space used most... Focus Room least" | **PASS** | — |
| answer: bookings count | plan bookings/2026-10 | 3 bookings | "There were 3 bookings..." | **PASS** | — |
| answer: events by organizer | plan events/organizer/2026-10 | PyData KHI (2) | "...PyData KHI (2), Startup Grind (1)." | **PASS** | — |
| answer: unanswerable (metric null) | plan `{metric: null}` ("what's the weather") | refuses, no guess | "I can only answer questions about this coworking space's data…" | **PASS** | Does not fabricate |
| answer: invalid plan twice | plan_call returns non-JSON both tries | graceful message after 1 retry | "Sorry, I couldn't turn that into a question…" | **PASS** | — |
| answer: empty question | `"   "` | raises `AIError` | `AIError` | **PASS** | — |
| answer: model phrasing used | fake phrase_call returns a sentence | returns that sentence | returned verbatim | **PASS** | Phrasing confined to given numbers |

## 4. Extraction parsing — model mocked (`tests/test_extract.py`)

| Scenario | Input | Expected output | Actual outcome | Pass/Fail | Observed issues |
|---|---|---|---|---|---|
| Booking w/ relative date | "...Ayesha tomorrow 2pm to 4pm, Rs 7000 paid" (TODAY=2026-10-10) + canned JSON | booking, date 2026-10-11, 14:00–16:00, 7000, paid | exact | **PASS** | Date resolution is in the (mocked) prompt, not re-derived in Python |
| Event with `<think>` block | model reply prefixed with `<think>…</think>` + JSON | think stripped; event parsed | parsed, event_name "PyData meetup" | **PASS** | — |
| Roman-Urdu/English mix | "bilal bhai ne Team Room liya kal 11 baje se 1 baje, 3000 … baqi pending" | booking, Bilal, Team Room, 11:00–13:00, 3000, pending | exact | **PASS** | Interpretation depends on mocked model output |
| Amount shorthand "5k" | model returns `amount_pkr: "5k"` | coerced to 5000 | 5000 | **PASS** | `_coerce_amount` handles k / Rs / commas |
| Retry after bad first reply | 1st reply invalid, 2nd valid JSON | succeeds via single retry | parsed on retry | **PASS** | Confirms exactly-one-retry works |
| Fenced JSON | ` ```json … ``` ` wrapper | fences stripped, parsed | parsed | **PASS** | — |
| Empty input | `"   "` | raises `AIError` (no model call) | `AIError` | **PASS** | — |
| Model unreachable | model_call raises `ConnectionError` | raises `AIError` | `AIError` | **PASS** | Error surfaced, no save |
| Invalid twice | both replies non-JSON | raises `AIError` after retry | `AIError` | **PASS** | Friendly error |
| Bad enum rejected | `record_type: "reservation"` | raises `AIError` | `AIError` | **PASS** | Enum enforced in validator |

---

## Known Limitations

These are honest gaps, not test failures. They reflect what the sandbox and current scope do/do not cover.

1. **The live LLM is never called in tests.** The sandbox has no running Ollama server and no outbound network, so every test injects a mocked `model_call`/`plan_call`/`phrase_call` returning canned output. The tests verify *our* parsing, validation, retry, whitelisting, and phrasing-from-numbers logic — **not** the real model's extraction/planning quality. Real-world accuracy of Qwen3-4B on messy/Roman-Urdu text is unverified here and should be spot-checked manually against a live `ollama serve`.

2. **`pydantic` is not installed in the test environment**, so `ai._HAS_PYDANTIC` is `False` and the extraction/validation tests exercised the **manual-validation fallback path**. The pydantic `ExtractionRecord` validators are therefore **not** covered by an automated run here. (Code compiles and is wired; it just wasn't executed with pydantic present.)

3. **No Streamlit UI tests.** `streamlit`/`pandas`/`python-dotenv` are not installable in the sandbox (no PyPI access), so `app.py` is only syntax-checked (`py_compile`) and its pure helpers (`_save_record`, `pkr`, conflict flow) were validated via a stubbed harness — there is no automated test of actual page rendering, buttons, chat input, or charts.

4. **Double-booking detection silently skips records with missing times.** A booking with no `start_time`/`end_time` (or no room) returns zero conflicts rather than warning that a conflict could not be evaluated. This is intentional (can't compare unknown intervals) but means some real overlaps could go undetected if times weren't extracted.

5. **Utilization % uses a flat assumption.** Available hours = days-in-month × 12 (9am–9pm), applied uniformly to every room with no accounting for closures, holidays, or rooms added mid-month. Percentages are a rough capacity signal, not an exact occupancy figure.

6. **Revenue counts bookings marked `paid` only.** Standalone `payments` rows and partially-paid bookings are not summed into dashboard revenue; a booking is treated as fully paid or fully pending via its `payment_status`. Partial payments noted by the model land in the free-text `notes` field, not in the revenue math.

7. **Test data is synthetic.** Numbers come from a small hand-built in-memory fixture (and the seeded demo data), chosen to make assertions deterministic. They are not representative of real-world volume or edge cases.

## How to reproduce

```bash
# In an environment with the dependencies installed:
pip install -r requirements.txt      # streamlit, ollama, pandas, python-dotenv, pytest
pytest -v tests/                     # 40 tests
pytest -s tests/                     # also prints expected-vs-actual for AI tests
```
