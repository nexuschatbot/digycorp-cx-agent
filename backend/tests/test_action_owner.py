"""select.action_owner: a brief that ASKS FOR A WRITE goes to the card that may
perform it — ahead of the embedding lane and ahead of entity_owner (2026-09-03).

Seen live on the test VM: Aisha wrote "Create a booking for Ali Test ...
(vehicle_id 3) ... slot ID 1147" — `slot ID` with a space, so entity_owner
matched only `vehicle_id`, which SALES produces, and a booking request went to
Sales. Sales has no write to propose, answered in prose, and the customer
waited eight seconds for a step that could never do anything.
"""
from __future__ import annotations

import pytest

from app.orchestrator import select

# The brief, verbatim from session f42164fc on 2026-09-03.
LIVE_FAILURE = (
    "Create a booking for Ali Test (customer ID 13, mobile +971509876543) for a "
    "Jetour T2 i-DM (vehicle_id 3) at Jetour Al Quoz (showroom_id 1), slot ID "
    "1147, Friday 4 September 2026 at 9 AM. Just the customer, no additional "
    "guests, no special requirements."
)


def test_the_live_failure_now_routes_to_the_card_that_can_book():
    r = select.action_owner(LIVE_FAILURE)
    assert r is not None and r.agent == "testdrive"
    assert "create_booking" in r.reason


def test_it_outranks_the_entity_named_in_passing():
    """entity_owner still reads `vehicle_id` here and still answers Sales — the
    fix is the ORDER, so select() must never reach it for a write request."""
    assert select.entity_owner(LIVE_FAILURE).agent == "sales"
    assert select.select(LIVE_FAILURE, judge=None).agent == "testdrive"


@pytest.mark.parametrize(
    "brief, action",
    [
        ("Please create a test drive booking for hadia hamad (vehicle_id 8).", "create_booking"),
        ("call create_booking for customer_id 13, slot_id 1147", "create_booking"),
        ("Reschedule booking TD-2026-BJN2 to slot_id 1478.", "reschedule_booking"),
        ("Cancel booking TD-2026-BJN2 for Ali Test.", "cancel_booking"),
        ("Please hold slot_id 1392, Saturday 5 September at 11 AM.", "hold_slot"),
        # The card calls it hold_slot; the brief calls it what the tool does.
        ("Lock slot_id 1678 (Sunday 6 September at 2 PM).", "hold_slot"),
    ],
)
def test_every_spelling_of_every_write_the_card_declares(brief, action):
    r = select.action_owner(brief)
    assert r is not None and r.agent == "testdrive" and action in r.reason


@pytest.mark.parametrize(
    "brief",
    [
        "What fuel-efficient 7-seater SUVs does Elite have available?",
        "What are the interior features of the Jetour X50 (vehicle_id 9)?",
        "What is the on_road_aed for the T2 Luxury Plus?",
        # 'cancellation' is not 'cancel': a policy question is not a write.
        "What is the cancellation policy if a customer cannot attend?",
        "Which showrooms stock the Jetour X70 FL Plus?",
        "",
    ],
)
def test_a_brief_that_asks_for_no_write_changes_nothing(brief):
    assert select.action_owner(brief) is None


def test_the_verb_and_its_object_must_be_close_together():
    """A window, not a bag of words: two unrelated clauses in one brief must
    not add up to a booking request."""
    assert select.action_owner(
        "Cancel that thought — the customer asked which cars we have in stock, "
        "and separately whether the Al Quoz branch takes a booking on Fridays."
    ) is None


def test_an_action_two_cards_could_perform_is_ambiguous(monkeypatch):
    """Same guard as entity_owner: ownership must be unique to be decisive."""
    from app.agents import loader

    real = loader.frontmatter
    monkeypatch.setattr(
        loader, "frontmatter",
        lambda a, *args, **kw: (
            {**real(a, *args, **kw), "proposes": ["create_booking"]}
            if a in ("sales", "testdrive") else real(a, *args, **kw)
        ),
    )
    assert select.action_owner("Create a booking for the customer") is None
