"""Labelled routing cases — the CI gate and the live accuracy check share these.

WHAT BELONGS HERE. Not "typical" messages: the ones that decide whether the
router is any good. English, Gulf Arabic and mixed; the money distinction (a
catalogue price is testdrive, haggling is fallback); a car named while managing a
booking; "change" meaning a different car rather than a different day; and the
follow-ups that are unroutable without the session note.

Every misroute observed in turn_trace becomes a case here. That is the whole
maintenance rule.

`expect_rules_only=True` marks cases the cascade must settle WITHOUT the model.
Those are latency guarantees, not just accuracy ones — the voice path depends on
them exiting early.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    message: str
    expect: str
    current_agent: str | None = None
    session_facts: dict = field(default_factory=dict)
    live_bookings: tuple = ()
    expect_rules_only: bool = False
    note: str = ""


CASES: tuple[Case, ...] = (
    # --- pre-filter: must never reach the model ---------------------------- #
    Case("Move TD-2026-HTXE to Sunday", "bookingmanagement", expect_rules_only=True),
    Case("my ref is TD 2026 HTXE", "bookingmanagement", expect_rules_only=True),
    Case("td.2026.htxe", "bookingmanagement", expect_rules_only=True),
    Case("I want to cancel my booking", "bookingmanagement", expect_rules_only=True),
    Case("أريد إلغاء الحجز", "bookingmanagement", expect_rules_only=True),
    Case("can I reschedule?", "bookingmanagement", expect_rules_only=True),

    # --- greetings: the most common first turn, and it must be free -------- #
    # Live, before this rule existed, "Hi" cost 1688ms to return fallback at 0.5.
    Case("Hi", "testdrive", expect_rules_only=True),
    Case("hello!", "testdrive", expect_rules_only=True),
    Case("Good morning", "testdrive", expect_rules_only=True),
    Case("السلام عليكم", "testdrive", expect_rules_only=True),
    Case("Hi", "bookingmanagement", current_agent="bookingmanagement",
         expect_rules_only=True, note="a greeting keeps the active agent"),
    Case("Hi, I want to cancel my booking", "bookingmanagement",
         expect_rules_only=True, note="a cancellation wearing a hello"),

    # --- referring expressions are continuations, whatever their length ----- #
    Case("Tell me more about it", "testdrive", current_agent="testdrive",
         expect_rules_only=True, note="5 words — missed the old word cap"),
    Case("is it available on Saturday?", "testdrive", current_agent="testdrive",
         expect_rules_only=True),
    Case("cancel it", "bookingmanagement", current_agent="testdrive",
         note="refers back AND changes journey — must NOT stick"),
    Case("move it to Sunday", "bookingmanagement", current_agent="testdrive",
         note="same veto"),

    # --- sticky: a follow-up answers the active agent ---------------------- #
    Case("yes", "testdrive", current_agent="testdrive", expect_rules_only=True),
    Case("3pm", "testdrive", current_agent="testdrive", expect_rules_only=True),
    Case("تمام", "bookingmanagement", current_agent="bookingmanagement",
         expect_rules_only=True),
    Case("Al Quoz", "testdrive", current_agent="testdrive", expect_rules_only=True),
    Case("the second one", "testdrive", current_agent="testdrive", expect_rules_only=True),

    # --- discovery -> testdrive -------------------------------------------- #
    Case("what SUVs do you have?", "testdrive"),
    Case("i want to know about jetour x70 fl", "testdrive"),
    Case("tell me about the T2", "testdrive"),
    Case("which one seats seven?", "testdrive"),
    Case("ايه الفرق بين X70 و X70 Plus", "testdrive"),
    Case("do you have anything hybrid", "testdrive"),

    # --- booking a NEW drive -> testdrive ---------------------------------- #
    Case("I'd like to book a test drive", "testdrive"),
    Case("can I come in on Saturday morning", "testdrive"),
    Case("أريد حجز تجربة قيادة", "testdrive"),

    # --- money: catalogue fact vs negotiation ------------------------------ #
    Case("how much is the X70 Plus", "testdrive", note="a fact we hold"),
    Case("what does the top trim include", "testdrive"),
    Case("can you do better on the price", "fallback", note="negotiation"),
    Case("what are the monthly instalments", "fallback", note="finance"),
    Case("do you take trade-ins", "fallback"),
    Case("I want to speak to a person", "fallback"),

    # --- the traps --------------------------------------------------------- #
    Case("can I move my X70 drive to Sunday", "bookingmanagement",
         note="a car is named, but this is about the booking"),
    Case("can I try the 7-seater instead", "testdrive",
         current_agent="testdrive",
         note="'change/instead' about a CAR, not a booking"),
    Case("what time is my booking on Thursday", "bookingmanagement",
         live_bookings=({"ref": "TD-2026-HTXE"},)),
    Case("are there any dealer add-on fees", "testdrive",
         current_agent="testdrive", note="a fee question mid-booking is still a fact"),

    # --- unroutable without the session note ------------------------------- #
    Case("what about the weekend?", "testdrive", current_agent="testdrive",
         session_facts={"vehicle_name": "Jetour X70 Plus"},
         note="availability for a drive being booked"),
    Case("what about the weekend?", "bookingmanagement",
         current_agent="bookingmanagement",
         live_bookings=({"ref": "TD-2026-HTXE"},),
         note="moving the booking they already have"),
)
