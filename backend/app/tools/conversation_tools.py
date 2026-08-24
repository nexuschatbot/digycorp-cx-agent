"""Conversation lifecycle tool (schema + thin fn) — rule 19.

close_conversation is the agent's only terminal action. It takes NO arguments:
which conversation to close is `session_id`, injected by the executor and absent
from the schema like every other piece of trusted context. A session identity the
model supplied would be an assertion it could change, not a fact — and closing
the wrong conversation is not a mistake we want to be possible.

Its write guards live where every other guard lives (the service layer + the tool
node choke point), never in the prompt:
  - CLEAN-END-ONLY — the tool node refuses a close batched with any other tool
    call in the same assistant turn (see tool_node._CLOSE_TOOL). Closing is not
    something that happens halfway through booking someone.
  - NO STRANDED STATE — services.conversation.close releases this session's live
    slot lock, so "never mind, bye" returns the inventory instead of holding it
    for the full TTL.
  - IDEMPOTENT + AUDITED — see services/conversation.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import conversation

# --------------------------------------------------------------------------- #
# close_conversation (rule 19, LOC-10)
# --------------------------------------------------------------------------- #
CLOSE_CONVERSATION_SCHEMA = {
    "name": "close_conversation",
    "description": (
        "End this conversation, after you have said goodbye. Call it ONLY when "
        "the customer is genuinely finished — they have declined your offer of "
        "further help, said goodbye, or thanked you and asked for nothing more. "
        "Say your farewell in the same message that calls this tool; there is no "
        "turn after it.\n\n"
        "Do NOT call it to tidy up, to end an awkward pause, or because a "
        "booking completed — a completed booking is when you ASK whether there "
        "is anything else, not when you close. Never close while anything is "
        "unfinished: not mid-booking, not while a slot is held and unbooked, not "
        "in the same turn as any other tool call (a close sent alongside other "
        "work is refused). If you are unsure whether they are done, ask; do not "
        "close on a guess. A conversation closed too early is a customer shown "
        "the door.\n\n"
        "It takes no arguments and needs none. Nothing is lost by closing: the "
        "booking, the reference and their details are all saved already, and the "
        "customer can start a new chat whenever they like — so never warn them "
        "that closing will lose anything, and never use it as pressure to decide."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


def close_conversation(
    db: Session,
    session_id: str,
    correlation_id: str | None = None,
    **_ignored: object,
) -> dict:
    """Thin wrapper over conversation.close (AD-J: in-process, never HTTP).

    The service's `released_locks` count is DELIBERATELY NOT RETURNED to the
    model — same reasoning as the lock TTL (rule 14's corollary): inventory
    hygiene is not a conversational fact, and a model told "I released your held
    slot" will eventually narrate it to a customer who is trying to say goodbye.
    The count still reaches the audit trail, which is where it belongs.
    """
    result = conversation.close(db, session_id=session_id, correlation_id=correlation_id)
    return {"closed": result["closed"], "already_closed": result["already_closed"]}
