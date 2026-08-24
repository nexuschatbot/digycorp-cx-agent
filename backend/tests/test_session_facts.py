"""The session-facts layer (2026-07-24): confirmed conversation facts injected into
the system prompt so a name/number/car given early survives a long chat, even after
the identifying turn falls out of the model's message window.

OFFLINE ONLY (CLAUDE.md): every test drives the real compiled graph with a scripted
`generate` injected via config, or exercises pure functions. No ANTHROPIC_API_KEY,
no vendor call — the scripted model never builds the SDK client.

What this locks in:
- the tool node populates session_facts from tool RESULTS (identity + chosen car);
- system_prompt() injects those facts as an anchor, and ONLY when present;
- the facts outlive the trim window that drops the turn they came from;
- a confirmed forget_me clears them, in the live state AND the persisted checkpoint.
"""

from __future__ import annotations

from app.agent import build_config, build_graph, new_sqlite_checkpointer
from app.agent.graph import TRIM_KEEP, _trim_messages
from app.agent.prompt import system_prompt

MOBILE = "+971509998877"  # fresh number — not a seeded customer
_ANCHOR_HEADER = "WHO YOU ARE ALREADY SPEAKING WITH"


# --------------------------------------------------------------------------- #
# A scripted "model" (same shape as test_agent_graph): replays canned turns.
# --------------------------------------------------------------------------- #
class ScriptedModel:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.i = 0

    def __call__(self, messages, *, model, system, tools, config=None) -> dict:
        msg = self.steps[self.i]
        self.i += 1
        return msg


def _tool_use(tid: str, tool: str, /, **inp) -> dict:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}


def _text(s: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": s}]}


def _run(db, session_id: str, first_message: str, steps: list[dict], *, checkpointer=None):
    graph = build_graph(checkpointer or new_sqlite_checkpointer())
    config = build_config(db, session_id, generate=ScriptedModel(steps))
    final = graph.invoke(
        {"messages": [{"role": "user", "content": first_message}], "iterations": 0, "customer_id": None},
        config,
    )
    return final, graph, config


# --------------------------------------------------------------------------- #
# 1. session_facts is populated from a scripted find_or_create_customer RESULT
# --------------------------------------------------------------------------- #
def test_session_facts_populated_after_find_or_create(db):
    steps = [
        _tool_use("u1", "find_or_create_customer", mobile=MOBILE, name="Test Driver"),
        _text("Lovely to meet you."),
    ]
    final, _, _ = _run(db, "sess-facts-id", "Hi, I'm Test Driver on +971509998877", steps)

    facts = final.get("session_facts") or {}
    assert facts.get("customer_id"), facts
    assert facts.get("name") == "Test Driver", facts
    assert facts.get("mobile") == MOBILE, facts  # normalised to canonical +971 form


# --------------------------------------------------------------------------- #
# 1b. THE OTHER IDENTITY DOOR (2026-08-05). A customer managing an existing
#     booking identifies by TD reference or mobile, with no name — and the
#     session must bind and the anchor fill exactly as it does through
#     find_or_create_customer. Both tools return the same envelope for precisely
#     this reason; _IDENTITY_TOOLS is what keeps the two binding sites from
#     learning only one of them (the bug shape this test exists to catch).
# --------------------------------------------------------------------------- #
def test_session_facts_populated_after_identifying_by_mobile(db):
    from app.services import customers

    customers.find_or_create(db, mobile=MOBILE, name="Test Driver")
    db.commit()

    steps = [
        _tool_use("u1", "identify_by_reference_or_mobile", mobile=MOBILE),
        _text("Of course, let me pull that up."),
    ]
    final, _, _ = _run(db, "sess-facts-ref", "I want to cancel my booking", steps)

    facts = final.get("session_facts") or {}
    assert final.get("customer_id"), "the session must be bound, not just described"
    assert facts.get("customer_id") == final["customer_id"]
    # The name we already hold fills the anchor even though nobody asked for it —
    # which is the point: the customer never has to say it.
    assert facts.get("name") == "Test Driver", facts
    assert facts.get("mobile") == MOBILE, facts


# --------------------------------------------------------------------------- #
# 2. system_prompt() carries the facts anchor ONLY when facts are present
# --------------------------------------------------------------------------- #
def test_system_prompt_carries_facts_only_when_present():
    facts = {"customer_id": 3, "title": "Mr", "name": "Ravi Sharma", "mobile": "+971 50 555 0103"}
    with_facts = system_prompt(session_facts=facts)
    assert "Mr Ravi Sharma" in with_facts
    assert "+971 50 555 0103" in with_facts
    assert _ANCHOR_HEADER in with_facts

    # A new customer (no facts) gets a byte-identical prompt to before this existed.
    bare = system_prompt()
    assert "Ravi Sharma" not in bare
    assert _ANCHOR_HEADER not in bare
    assert system_prompt(session_facts={}) == bare


# --------------------------------------------------------------------------- #
# 3. The facts outlive the trim: injected even though the identifying turn is gone
# --------------------------------------------------------------------------- #
def test_facts_survive_a_long_conversation_trim():
    name = "Ravi Sharma"
    # Only the FIRST message names the customer; then a long tail that never does.
    # The tail is sized FROM TRIM_KEEP rather than hardcoded: it used to be a flat
    # 20 exchanges, which comfortably exceeded a window of 12 and then exactly
    # matched one of 40, so raising the constant on 2026-07-27 turned this test
    # vacuous (nothing was trimmed, and it failed rather than silently passing only
    # because of the assertion below). Derived, it stays meaningful at any window.
    msgs = [{"role": "user", "content": f"Hi, I'm {name} on {MOBILE}"}]
    for i in range(TRIM_KEEP):
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"reply {i}"}]})
        msgs.append({"role": "user", "content": f"one more thing {i}"})

    trimmed = _trim_messages(msgs)
    assert len(trimmed) < len(msgs), "a long history should be trimmed"
    # The turn that carried the name is no longer in what the model is sent...
    assert not any(name in str(m.get("content")) for m in trimmed), "identifying turn should be trimmed away"
    # ...yet the anchor re-states it, which is the whole point of the layer.
    assert name in system_prompt(session_facts={"name": name, "mobile": MOBILE})


# --------------------------------------------------------------------------- #
# 3b. The trim NEVER orphans a tool_result from its tool_use, and always begins
# on a user turn — the two things the Anthropic API rejects.
# --------------------------------------------------------------------------- #
def test_trim_keeps_tool_use_result_pairs_intact():
    def turn(n: int, k: int) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": f"turn {n} customer"}]
        for i in range(k):
            tid = f"t{n}_{i}"
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "x", "input": {}}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "{}"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"turn {n} reply"}]})
        return msgs

    hist = turn(1, 3) + turn(2, 3) + turn(3, 3)  # 24 messages, well over TRIM_KEEP
    out = _trim_messages(hist)

    # Starts on a genuine customer turn (never on an orphaned tool_result).
    first = out[0]
    assert first["role"] == "user" and isinstance(first["content"], str)

    # Every tool_result in the window has its tool_use earlier in the window.
    seen: set[str] = set()
    for m in out:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if b.get("type") == "tool_use":
                seen.add(b["id"])
            if b.get("type") == "tool_result":
                assert b["tool_use_id"] in seen, f"orphaned tool_result {b['tool_use_id']}"


# --------------------------------------------------------------------------- #
# 4. A confirmed forget_me clears the LIVE session_facts (the tool-node clear)
# --------------------------------------------------------------------------- #
def test_confirmed_forget_me_clears_session_facts(db, monkeypatch):
    # Keep erasure's persisted-checkpoint step a no-op here so this isolates the
    # tool-node clear (the live-state guarantee). The persisted-checkpoint clear is
    # covered on its own below.
    from app import config as _config

    monkeypatch.setattr(_config, "AGENT_CHECKPOINT_PATH", "/nonexistent/agent_checkpoints.db")

    steps = [
        _tool_use("u1", "find_or_create_customer", mobile=MOBILE, name="Test Driver"),
        _tool_use("u2", "forget_me", confirmed=True),
        _text("All cleared. Take care!"),
    ]
    final, _, _ = _run(db, "sess-forget", "please forget me", steps)

    assert (final.get("session_facts") or {}) == {}, final.get("session_facts")


# --------------------------------------------------------------------------- #
# 5. Erasure clears session_facts from the PERSISTED checkpoint too (step 5)
# --------------------------------------------------------------------------- #
def test_forget_checkpoints_clears_persisted_session_facts(db, tmp_path, monkeypatch):
    from app import config as _config
    from app.services import erasure

    ckpt = str(tmp_path / "agent_checkpoints.db")
    monkeypatch.setattr(_config, "AGENT_CHECKPOINT_PATH", ckpt)

    session_id = "sess-persisted"
    # Populate a real, file-backed checkpoint thread carrying session_facts.
    _run(
        db,
        session_id,
        "Hi, I'm Test Driver on +971509998877",
        [_tool_use("u1", "find_or_create_customer", mobile=MOBILE, name="Test Driver"), _text("Hi.")],
        checkpointer=new_sqlite_checkpointer(ckpt),
    )

    config = build_config(db, session_id)
    before = build_graph(new_sqlite_checkpointer(ckpt)).get_state(config).values
    assert (before.get("session_facts") or {}).get("name") == "Test Driver", before.get("session_facts")

    # The erasure path drops the thread (and explicitly clears session_facts first).
    assert erasure._forget_checkpoints({session_id}) == 1

    after = build_graph(new_sqlite_checkpointer(ckpt)).get_state(config).values
    assert (after.get("session_facts") or {}) == {}, after.get("session_facts")


# --------------------------------------------------------------------------- #
# 7. AUTHORSHIP (2026-08-03). create_booking's own result is folded into
#    session_facts["booked_refs"], and the bookings anchor then attributes that
#    booking to the AGENT instead of listing it as something the customer arrived
#    with.
#
#    Why this is not cosmetic: the bookings anchor is re-read from the DB on every
#    generation, so a booking confirmed one second earlier appears in it instantly.
#    Under the old heading ("WHAT THIS CUSTOMER ALREADY HAS WITH US") the model
#    believed the heading over its own tool result — it read two refs it had just
#    created as pre-existing bookings and explained the coincidence to itself as
#    duplicate detection, inventing a mechanism to account for a contradiction we
#    had manufactured (rule 2). Driven through the REAL service here, so the fold is
#    pinned to create_booking's actual result keys rather than to a fixture's guess.
# --------------------------------------------------------------------------- #
def test_create_booking_records_that_this_session_made_it(slots):
    from sqlalchemy import select

    from app.models import Slot, SlotStatus

    db = slots
    slot = db.scalars(
        select(Slot).where(Slot.status == SlotStatus.open).order_by(Slot.start, Slot.id)
    ).first()

    steps = [
        _tool_use("u1", "find_or_create_customer", mobile=MOBILE, name="Author Tester"),
        _tool_use("u2", "lock_slot", slot_id=slot.id),
        _tool_use("u3", "acknowledge_licence_reminder"),
        _tool_use("u4", "create_booking", vehicle_id=slot.vehicle_id, slot_id=slot.id),
        _text("You're all set."),
    ]
    final, _, _ = _run(db, "sess-facts-author", "I'd like to book a drive", steps)

    refs = (final.get("session_facts") or {}).get("booked_refs") or []
    assert len(refs) == 1, final.get("session_facts")
    assert refs[0].startswith("TD-"), refs

    # And the anchor says WHO made it, in the words the model reads back.
    prompt = system_prompt(
        session_facts=final["session_facts"],
        live_bookings=[{"booking_ref": refs[0], "vehicle_name": "Jetour X50",
                        "showroom_name": "Jetour Al Quoz", "location_type": "showroom",
                        "start_human": "Saturday, 1 August 2026 at 7 PM"}],
    )
    assert "You booked one drive for them in this conversation just now" in prompt
    assert "never call it pre-existing" in prompt


# --------------------------------------------------------------------------- #
# 7b. An IDEMPOTENT create_booking is NOT this session's work. It means the
#     booking already existed and the service merely matched it, which is exactly
#     the case that must not be claimed — the discriminator is `created`, never
#     the presence of a booking in the result.
# --------------------------------------------------------------------------- #
def test_idempotent_create_is_not_claimed_as_this_sessions_work():
    from app.agent.tool_node import _fold_session_facts

    matched = {"booking": {"booking_ref": "TD-2026-OLD1"}, "created": False, "idempotent": True}
    assert _fold_session_facts(None, "create_booking", matched, {}) == {}

    made = {"booking": {"booking_ref": "TD-2026-NEW1"}, "created": True}
    assert _fold_session_facts(None, "create_booking", made, {}) == {
        "booked_refs": ["TD-2026-NEW1"]
    }

    # Repeated folds of the same ref do not duplicate it, and a second booking
    # appends in order.
    facts = {"booked_refs": ["TD-2026-NEW1"]}
    assert _fold_session_facts(None, "create_booking", made, facts) == facts
    second = {"booking": {"booking_ref": "TD-2026-NEW2"}, "created": True}
    assert _fold_session_facts(None, "create_booking", second, facts)["booked_refs"] == [
        "TD-2026-NEW1",
        "TD-2026-NEW2",
    ]


# --------------------------------------------------------------------------- #
# 7c. A confirmed forget_me clears authorship along with everything else — rule 12
#     leaves nothing behind, including which bookings this session made.
# --------------------------------------------------------------------------- #
def test_forget_me_clears_booked_refs():
    from app.agent.tool_node import _fold_session_facts

    facts = {"name": "Author Tester", "booked_refs": ["TD-2026-NEW1"]}
    assert _fold_session_facts(None, "forget_me", {"forgotten": True}, facts) == {}
