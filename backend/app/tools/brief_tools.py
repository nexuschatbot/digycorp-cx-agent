"""emit_briefs (T5.1) — how Aisha hands work to the specialists.

NOT AN ORDINARY TOOL, and it is the one exception in this package. Every other
tool here is `fn(db, session_id, ...) -> dict` dispatched by tool_node. This one
is answered by the ORCHESTRATOR NODE instead: the graph routes a message
containing an emit_briefs call away from the tools node entirely, runs the
cascade, and returns the findings as this call's tool_result. There is therefore
no `emit_briefs` function — a `fn` would be a lie about where the work happens.

WHY A TOOL CALL AND NOT A SECOND GENERATION. Aisha streams her opening line AND
emits the briefs in ONE generation, so the first token reaches the customer in
about half a second instead of after every sub-agent has finished. The
`text_break` that seals her bubble is what makes that safe: the opening line is
DELIVERED, not retracted, so she can never take it back once the findings
disagree with it. Asking for the briefs in a separate call would double the
latency and buy nothing.

SHE NEVER LEARNS THE ROSTER. There is no `agent` field in this schema on
purpose. Aisha writes what needs finding out; orchestrator/select.py decides who
finds it. Letting her name the specialist would put routing accuracy inside a
prompt — unmeasurable, untunable, and impossible to change without a prompt
rewrite — instead of in a threshold with an eval behind it.

CALLED ALONE. The orchestrator node answers every tool_use block in the message
it lands in, and refuses any other tool called alongside it. Mixing a brief with
a direct tool call would make the turn's ordering ambiguous — the tool node owns
the write transaction, the orchestrator owns the cascade, and a single message
cannot be inside both.
"""

from __future__ import annotations

EMIT_BRIEFS_SCHEMA = {
    "name": "emit_briefs",
    "description": (
        "Hand one or more questions to the specialists who can answer them, "
        "and get their findings back. Use it whenever answering the customer "
        "needs a fact you do not already have: anything about which cars we "
        "have, what they cost to buy or finance, what is in stock and where, "
        "which branches exist, or when a test drive could happen.\n\n"
        "CALL IT ALONE, in the same message as your opening line to the "
        "customer. Say something natural first — acknowledge what they asked "
        "and tell them you are looking it up — then emit the briefs. Your "
        "opening line is DELIVERED IMMEDIATELY and cannot be taken back, so it "
        "must not contain any fact you have not been given yet: no prices, no "
        "model names you are not certain of, no availability. Acknowledge and "
        "promise, never pre-empt.\n\n"
        "WRITE A BRIEF AS A COMPLETE QUESTION, in your own words, as if asking "
        "a well-informed colleague who cannot see this conversation. Include "
        "everything they need — 'a seven-seater under AED 130,000, petrol' is "
        "a brief; 'seven seaters' is not. Do NOT name which specialist should "
        "handle it: you do not choose, and there is no field for it.\n\n"
        "ONE BRIEF PER QUESTION. Two unrelated questions in one brief get one "
        "muddled answer. Split them, and they run at the same time.\n\n"
        "USE depends_on WHEN ONE BRIEF NEEDS ANOTHER'S ANSWER. 'What does the "
        "T2 cost on the road' needs a specific trim first, so the pricing "
        "brief depends on the trim brief. Give the INDEX of the brief it "
        "depends on, counting from 0 in the list you are sending. Get this "
        "right and the answers thread together; leave it out where it was "
        "needed and the second brief comes back blocked.\n\n"
        "DO NOT use it for anything you can do yourself: greeting, asking who "
        "someone is, recording consent, remembering a preference, saying "
        "goodbye, or offering choices. Those are yours, and going out to a "
        "specialist for them wastes a second of the customer's time.\n\n"
        "WHAT COMES BACK: 'findings' from each specialist that succeeded, and "
        "'unavailable' naming any that did not, with the reason. Answer from "
        "the findings, and be honest about the gaps — 'partial': true means "
        "some of what you asked for is missing, and the customer must hear "
        "which part rather than be given a confident half-answer.\n\n"
        "'actions' IS WHAT ACTUALLY HAPPENED, and it is the only thing you may "
        "treat as done. A specialist can ASK for a slot to be held or a booking "
        "to be made; whether it happened is decided afterwards, and the answer "
        "is here. Each entry has a 'status': 'executed' means it is real and "
        "you may confirm it — quote the booking_ref if there is one. 'refused' "
        "means it did NOT happen and the 'detail' says why; tell the customer "
        "plainly and do the step it names. 'failed' means it broke; apologise "
        "and offer to try again. NEVER confirm a booking, a hold or a "
        "cancellation that is not listed here as 'executed', however confidently "
        "a specialist's note is worded — their notes describe what they asked "
        "for, not what was done."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "briefs": {
                "type": "array",
                "description": (
                    "One entry per question. Order matters only for "
                    "depends_on indices."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "brief": {
                            "type": "string",
                            "description": (
                                "The complete question, in your own words, "
                                "with every constraint the customer gave."
                            ),
                        },
                        "depends_on": {
                            "type": "array",
                            "description": (
                                "Indices of briefs in THIS list whose answers "
                                "this one needs. Usually omitted."
                            ),
                            "items": {"type": "integer"},
                        },
                    },
                    "required": ["brief"],
                },
            }
        },
        "required": ["briefs"],
    },
}

# The refusal handed back for any other tool called in the same message. Aisha
# recovers by re-calling it alone on the next generation; the checkpoint stays
# consistent because every tool_use still receives a tool_result.
CALLED_ALONGSIDE_ERROR = {
    "error": "emit_briefs_must_be_called_alone",
    "detail": (
        "emit_briefs was called in the same message as another tool, so that "
        "other tool was not run. Call emit_briefs on its own, then use its "
        "findings; call your own tools in a separate message."
    ),
}
