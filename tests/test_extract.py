"""Tests for ai.extract_record.

The sandbox has no live Ollama server, so these tests inject a fake
`model_call` that returns canned raw strings (mimicking real model output,
including <think> blocks and a first-attempt failure that forces the retry
path). This exercises the full clean -> parse -> validate -> retry pipeline
deterministically and offline.

Run:  pytest -s tests/test_extract.py     (-s to see printed expected/actual)
"""

from __future__ import annotations

import json
from datetime import date

import pytest

import ai


TODAY = date(2026, 10, 10)  # Saturday


# Each case: (name, message, fake_raw_responses, expected_subset)
# fake_raw_responses is a list; index 0 is returned on first call, index 1 on
# the retry. A single-element list means both attempts return the same thing.
CASES = [
    (
        "booking_relative_tomorrow",
        "Booked Conference Hall for Ayesha tomorrow 2pm to 4pm, Rs 7000 paid.",
        [json.dumps({
            "record_type": "booking", "name": "Ayesha", "room": "Conference Hall",
            "date": "2026-10-11", "start_time": "14:00", "end_time": "16:00",
            "amount_pkr": 7000, "payment_status": "paid",
            "event_name": None, "notes": None,
        })],
        {"record_type": "booking", "name": "Ayesha", "room": "Conference Hall",
         "date": "2026-10-11", "start_time": "14:00", "end_time": "16:00",
         "amount_pkr": 7000, "payment_status": "paid"},
    ),
    (
        "event_with_think_block",
        "PyData meetup on 18th Oct in Event Space, organizer PyData Karachi, 6pm.",
        # Model emits a <think> block before the JSON -> must be stripped.
        ["<think>The user is describing an event. Date is Oct 18.</think>\n"
         + json.dumps({
             "record_type": "event", "name": None, "room": "Event Space",
             "date": "2026-10-18", "start_time": "18:00", "end_time": None,
             "amount_pkr": None, "payment_status": "unknown",
             "event_name": "PyData meetup", "notes": "organizer PyData Karachi",
         })],
        {"record_type": "event", "room": "Event Space", "date": "2026-10-18",
         "start_time": "18:00", "event_name": "PyData meetup",
         "payment_status": "unknown"},
    ),
    (
        "roman_urdu_mix_booking",
        "bilal bhai ne Team Room liya kal 11 baje se 1 baje, 3000 me se 1500 ada baqi pending",
        [json.dumps({
            "record_type": "booking", "name": "Bilal", "room": "Team Room",
            "date": "2026-10-11", "start_time": "11:00", "end_time": "13:00",
            "amount_pkr": 3000, "payment_status": "pending",
            "event_name": None, "notes": "1500 paid, 1500 pending",
        })],
        {"record_type": "booking", "name": "Bilal", "room": "Team Room",
         "date": "2026-10-11", "start_time": "11:00", "end_time": "13:00",
         "amount_pkr": 3000, "payment_status": "pending"},
    ),
    (
        "payment_amount_shorthand_5k",
        "Received 5k from Usman for last week's Quiet Pod booking.",
        # amount comes back as a string "5k" -> _coerce_amount -> 5000.
        [json.dumps({
            "record_type": "payment", "name": "Usman", "room": "Quiet Pod",
            "date": None, "start_time": None, "end_time": None,
            "amount_pkr": "5k", "payment_status": "paid",
            "event_name": None, "notes": "last week's booking",
        })],
        {"record_type": "payment", "name": "Usman", "amount_pkr": 5000,
         "payment_status": "paid"},
    ),
    (
        "retry_after_bad_first_reply",
        "Focus Room booked by Sana on 2026-09-25 10:00-12:00, 1600 pending.",
        [
            "sorry, I can't output JSON here",  # 1st attempt: invalid -> retry
            json.dumps({
                "record_type": "booking", "name": "Sana", "room": "Focus Room",
                "date": "2026-09-25", "start_time": "10:00", "end_time": "12:00",
                "amount_pkr": 1600, "payment_status": "pending",
                "event_name": None, "notes": None,
            }),
        ],
        {"record_type": "booking", "name": "Sana", "room": "Focus Room",
         "date": "2026-09-25", "amount_pkr": 1600, "payment_status": "pending"},
    ),
    (
        "event_fenced_json",
        "Resume Clinic workshop by Freelancers Hub, 5th Oct, Team Room, 3pm-5pm.",
        # Model wraps output in markdown fences -> must be stripped.
        ["```json\n" + json.dumps({
            "record_type": "event", "name": None, "room": "Team Room",
            "date": "2026-10-05", "start_time": "15:00", "end_time": "17:00",
            "amount_pkr": None, "payment_status": "unknown",
            "event_name": "Resume Clinic", "notes": "by Freelancers Hub",
        }) + "\n```"],
        {"record_type": "event", "room": "Team Room", "date": "2026-10-05",
         "start_time": "15:00", "end_time": "17:00",
         "event_name": "Resume Clinic", "payment_status": "unknown"},
    ),
]


def _make_fake(responses):
    """Return a model_call that yields canned responses per attempt."""
    state = {"i": 0}

    def fake(system, user, model):
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return responses[idx]

    return fake


@pytest.mark.parametrize("name,message,responses,expected", CASES,
                         ids=[c[0] for c in CASES])
def test_extract_cases(name, message, responses, expected):
    actual = ai.extract_record(message, today=TODAY, model_call=_make_fake(responses))

    print(f"\n--- {name} ---")
    print(f"message : {message}")
    print(f"expected: {expected}")
    print(f"actual  : {actual}")

    for key, exp_val in expected.items():
        assert actual[key] == exp_val, f"{name}: {key} expected {exp_val!r}, got {actual[key]!r}"
    # Canonical shape is always complete.
    assert set(actual.keys()) == set(ai.OUTPUT_KEYS)


def test_empty_input_raises():
    with pytest.raises(ai.AIError):
        ai.extract_record("   ", today=TODAY, model_call=_make_fake(["{}"]))


def test_model_unreachable_raises():
    def boom(system, user, model):
        raise ConnectionError("no ollama server")

    with pytest.raises(ai.AIError):
        ai.extract_record("Book Focus Room", today=TODAY, model_call=boom)


def test_invalid_twice_raises_friendly_error():
    # Both attempts invalid -> AIError after single retry.
    fake = _make_fake(["not json", "still not json"])
    with pytest.raises(ai.AIError):
        ai.extract_record("Book Focus Room", today=TODAY, model_call=fake)


def test_bad_enum_rejected():
    bad = _make_fake([json.dumps({
        "record_type": "reservation",  # not in allowed set
        "name": "X", "room": None, "date": None, "start_time": None,
        "end_time": None, "amount_pkr": None, "payment_status": "unknown",
        "event_name": None, "notes": None,
    })] * 2)
    with pytest.raises(ai.AIError):
        ai.extract_record("something", today=TODAY, model_call=bad)
