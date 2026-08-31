"""The orchestration layer (Phase 3).

    select.py  which agent handles a brief
    order.py   what runs before what
    run.py     dispatch, thread results forward

AISHA NEVER LEARNS THE ROSTER. She splits a request into plain-language briefs;
everything about WHICH agent exists and WHAT ORDER they run in lives here. That
is the whole point of the layer, and it is why select.py takes a string rather
than an agent name.

NOTHING HERE WRITES. Commercial writes — holds, bookings, reschedules, cancels —
land in Phase 4's write.py. Selection, ordering and dispatch are read-only.
"""
