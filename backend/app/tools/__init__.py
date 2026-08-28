"""Tool registry: name -> {schema, fn}.

The agent loop (a later phase) consumes TOOL_SCHEMAS for the Anthropic `tools`
parameter and dispatches tool_use blocks through TOOL_FNS. Nothing here calls
Claude; nothing here contains business logic.

CALLING CONVENTION
------------------
    TOOL_FNS[name](db, session_id, **block.input)   # + injected trusted context

`db` and `session_id` are injected by the tool node (app/agent/tool_node.py), and
for the two writes that need a trusted identity, so is `customer_id` (resolved
from the session -> customer_id binding minted by find_or_create_customer) and a
per-invocation `correlation_id`. All are deliberately absent from every
input_schema: Claude never sees them, so it cannot forge a session, book under
another customer's id, or reach another customer's data. Tool functions accept
**_ignored so an unexpected argument from the model is dropped rather than
raising TypeError mid-turn.

AUDIT (behaviour rule 15, FR-AD-02) — TWO PLACES, ONE CORRELATION ID
--------------------------------------------------------------------
The tool node is the single choke point every tool call passes through. It emits
a `tool_call` audit event for EVERY tool in the registry — "the agent asked" — stamped
with a fresh correlation_id per invocation. Auditing reads inside the services
would be wrong: catalogue.search is also called by the console/REST path, where
logging it as agent grounding would be a lie. So the "the agent invoked X" fact
lives here, at the choke point, and nowhere else.

WRITES ALSO audit in the SERVICE (see services/audit.py) — the semantic event
("booking TD-2026-0142 created"), not the invocation. Every write service has a
second caller that never passes through this registry — the one-tap WhatsApp
reminder buttons post structured intents straight to a REST route, skipping the
LLM entirely. Auditing the semantic write here would silently lose every one of
those actions, and FR-AD-02 requires all of it.

So a single agent write produces two rows — the choke-point `tool_call` and the
service's `intent`/`consent`/`notification` — and they share the correlation_id
the tool node threads into the injected context. The console groups by it: one
action, several facets, not double-logging.

WRITE TOOLS carry the service-layer write guards: lock ownership (enforceable
only because session_id is injected, never modelled), idempotency, and
service-layer audit. This now includes the booking-management journey —
reschedule_booking and cancel_booking (FR-RS, FR-CN, rules 10/11) — which resolve
an EXISTING booking by mobile OR TD reference (a declared lookup key, unlike
create_booking's injected customer_id: see services/booking._lookup for why that
relaxation is safe only on the look-up-an-existing-booking path). get_booking is
the read front door for the same journey (FR-RS-01/FR-CN-01).
"""

from __future__ import annotations

from app.tools.booking_tools import (
    CANCEL_BOOKING_SCHEMA,
    CREATE_BOOKING_SCHEMA,
    GET_BOOKING_SCHEMA,
    RESCHEDULE_BOOKING_SCHEMA,
    cancel_booking,
    create_booking,
    get_booking,
    reschedule_booking,
)
from app.tools.catalogue_tools import (
    GET_SHOWROOMS_SCHEMA,
    GET_VEHICLE_DETAILS_SCHEMA,
    SEARCH_VEHICLES_SCHEMA,
    get_showrooms,
    get_vehicle_details,
    search_vehicles,
)
from app.tools.compare_tools import (
    COMPARE_VEHICLES_SCHEMA,
    compare_vehicles,
)
from app.tools.location_tools import (
    NEAREST_SHOWROOM_SCHEMA,
    nearest_showroom,
)
from app.tools.conversation_tools import (
    CLOSE_CONVERSATION_SCHEMA,
    close_conversation,
)
from app.tools.escalation_tools import (
    REQUEST_HUMAN_CALLBACK_SCHEMA,
    request_human_callback,
)
from app.tools.customer_tools import (
    FIND_OR_CREATE_CUSTOMER_SCHEMA,
    IDENTIFY_BY_REFERENCE_OR_MOBILE_SCHEMA,
    RECORD_CONSENT_SCHEMA,
    find_or_create_customer,
    identify_by_reference_or_mobile,
    record_consent,
)
from app.tools.memory_tools import (
    FORGET_ME_SCHEMA,
    SAVE_MEMORY_SCHEMA,
    forget_me,
    save_memory,
)
from app.tools.recognition_tools import (
    CONFIRM_RETURNING_CUSTOMER_SCHEMA,
    confirm_returning_customer,
)
from app.tools.brochure_tools import (
    SEND_BROCHURE_SCHEMA,
    send_brochure,
)
from app.tools.ui_tools import (
    OFFER_OPTIONS_SCHEMA,
    offer_options,
)
from app.tools.slot_tools import (
    ACKNOWLEDGE_LICENCE_REMINDER_SCHEMA,
    GET_AVAILABLE_SLOTS_SCHEMA,
    LOCK_SLOT_SCHEMA,
    acknowledge_licence_reminder,
    get_available_slots,
    lock_slot,
)

REGISTRY: dict[str, dict] = {
    # --- reads ------------------------------------------------------------- #
    "search_vehicles": {"schema": SEARCH_VEHICLES_SCHEMA, "fn": search_vehicles},
    "get_vehicle_details": {"schema": GET_VEHICLE_DETAILS_SCHEMA, "fn": get_vehicle_details},
    "get_showrooms": {"schema": GET_SHOWROOMS_SCHEMA, "fn": get_showrooms},
    # Two models side by side (2026-08-15). A READ, and same-brand only:
    # every figure comes from our own catalogue, so nothing is asserted
    # about anyone else's car. It returns rows and never a verdict.
    "compare_vehicles": {"schema": COMPARE_VEHICLES_SCHEMA, "fn": compare_vehicles},
    "nearest_showroom": {"schema": NEAREST_SHOWROOM_SCHEMA, "fn": nearest_showroom},
    "get_available_slots": {"schema": GET_AVAILABLE_SLOTS_SCHEMA, "fn": get_available_slots},
    "get_booking": {"schema": GET_BOOKING_SCHEMA, "fn": get_booking},
    # The manage-a-booking front door (2026-08-05): identifies by TD reference OR
    # mobile, either alone, and asks for no name. A READ — it resolves an existing
    # customer and can never create one, which is what makes the relaxed identity
    # safe to attach to it. Making a NEW booking still goes through
    # find_or_create_customer below, name and all.
    "identify_by_reference_or_mobile": {
        "schema": IDENTIFY_BY_REFERENCE_OR_MOBILE_SCHEMA,
        "fn": identify_by_reference_or_mobile,
    },
    # The third identity door (C10, 2026-08-14) — a returning customer on a
    # browser we recognise, confirmed by them in words. A READ for the same reason
    # identify_by_reference_or_mobile is one: it resolves an EXISTING customer from
    # a device link and can never create one. It returns the same envelope as the
    # other two, so the binding, session_facts and memory recall downstream stay
    # one code path. device_token is injected from the cookie; the model supplies
    # nothing at all, which is why its input_schema is empty.
    "confirm_returning_customer": {
        "schema": CONFIRM_RETURNING_CUSTOMER_SCHEMA,
        "fn": confirm_returning_customer,
    },
    # Presentation only (2026-08-14): draws tappable chips under a question whose
    # answers are a fixed set. Touches nothing, so it sits with the reads. Called
    # ALONE and ends the turn — see tools/ui_tools.py for why the agent declares
    # the options instead of the browser pattern-matching her prose.
    "offer_options": {"schema": OFFER_OPTIONS_SCHEMA, "fn": offer_options},
    # --- writes ------------------------------------------------------------ #
    "find_or_create_customer": {
        "schema": FIND_OR_CREATE_CUSTOMER_SCHEMA,
        "fn": find_or_create_customer,
    },
    "record_consent": {"schema": RECORD_CONSENT_SCHEMA, "fn": record_consent},
    # C3 (2026-08-14). A WRITE: it queues a real message to a real phone.
    # customer_id is injected (whose phone must never be modelled) and the
    # consent gate is checked twice — here so Aisha can ask for permission in
    # the same breath, and again in the dispatcher, which is the one that
    # actually stops the send.
    "send_brochure": {"schema": SEND_BROCHURE_SCHEMA, "fn": send_brochure},
    "lock_slot": {"schema": LOCK_SLOT_SCHEMA, "fn": lock_slot},
    # Rule 9 precondition 3. A write in the sense that matters here: it records
    # state create_booking then refuses to proceed without, which is what makes
    # the licence reminder an enforced STEP rather than a hope in the prompt.
    "acknowledge_licence_reminder": {
        "schema": ACKNOWLEDGE_LICENCE_REMINDER_SCHEMA,
        "fn": acknowledge_licence_reminder,
    },
    "create_booking": {"schema": CREATE_BOOKING_SCHEMA, "fn": create_booking},
    # FR-MEM / rule 12. save_memory takes ONLY free-text notes: what they booked
    # is learned as a side effect of booking.create, so the model cannot overwrite
    # an observed fact with a paraphrase. There is no recall tool by design — a
    # consented profile rides on find_or_create_customer's result.
    "save_memory": {"schema": SAVE_MEMORY_SCHEMA, "fn": save_memory},
    # Two-call handshake: the unconfirmed call CANNOT delete, so the confirmation
    # is structural rather than a hope in the prompt (see services/memory.forget).
    "forget_me": {"schema": FORGET_ME_SCHEMA, "fn": forget_me},
    "reschedule_booking": {"schema": RESCHEDULE_BOOKING_SCHEMA, "fn": reschedule_booking},
    "cancel_booking": {"schema": CANCEL_BOOKING_SCHEMA, "fn": cancel_booking},
    # Escalation stub (rule 13, FR-ES): offers a human callback and LOGS it (an
    # `escalation` audit row via services/escalation), for out-of-scope requests
    # (price/finance/complaints), safety topics, an explicit "get me a human", or
    # two consecutive failures. customer_id is injected — an escalation needs
    # someone to call back, so an unidentified session is refused as data.
    "request_human_callback": {
        "schema": REQUEST_HUMAN_CALLBACK_SCHEMA,
        "fn": request_human_callback,
    },
    # Terminal action (rule 19): ends the conversation, releases any live slot
    # lock, and leaves an `intent` row so a chat that ENDED is as auditable as
    # one that booked. Guarded clean-end-only at the choke point — see tool_node.
    "close_conversation": {"schema": CLOSE_CONVERSATION_SCHEMA, "fn": close_conversation},
}

# Ready to hand to the Anthropic SDK's `tools=` parameter.
TOOL_SCHEMAS: list[dict] = [entry["schema"] for entry in REGISTRY.values()]
TOOL_FNS = {name: entry["fn"] for name, entry in REGISTRY.items()}


# --------------------------------------------------------------------------- #
# Which tools WRITE (T4.1)
# --------------------------------------------------------------------------- #
# DATA, NOT A COMMENT. The reads/writes split above was a `--- writes ---`
# divider a reader had to honour; from Phase 4 it is an invariant a TEST
# enforces, because the whole write architecture rests on it:
#
#   - no sub-agent may hold a write tool at all (they return proposals as data);
#   - Aisha may hold the CONVERSATIONAL writes and no commercial one;
#   - only the orchestrator books.
#
# None of that is checkable while "is this a write?" lives in a comment.
#
# THE SPLIT IS BY WHAT THE TOOL DOES TO THE DATABASE, not by how it feels.
# `send_brochure` is a write because it queues a real message to a real phone;
# `offer_options` is a read because it draws chips and touches nothing.
WRITE_TOOLS: frozenset[str] = frozenset({
    "find_or_create_customer",
    "record_consent",
    "send_brochure",
    "lock_slot",
    "acknowledge_licence_reminder",
    "create_booking",
    "save_memory",
    "forget_me",
    "reschedule_booking",
    "cancel_booking",
    "request_human_callback",
    "close_conversation",
})

# COMMERCIAL writes: the ones that move inventory or money. From Phase 4 these
# belong to the orchestrator alone — see app/orchestrator/write.py. The rest of
# WRITE_TOOLS are acts of DIALOGUE (who you are, a yes, a licence
# acknowledgement, a remembered preference, goodbye) and stay with whoever is
# talking, because only Aisha talks.
COMMERCIAL_WRITE_TOOLS: frozenset[str] = frozenset({
    "lock_slot",
    "create_booking",
    "reschedule_booking",
    "cancel_booking",
})

READ_TOOLS: frozenset[str] = frozenset(REGISTRY) - WRITE_TOOLS

__all__ = [
    "REGISTRY",
    "TOOL_SCHEMAS",
    "TOOL_FNS",
    "WRITE_TOOLS",
    "COMMERCIAL_WRITE_TOOLS",
    "READ_TOOLS",
]
