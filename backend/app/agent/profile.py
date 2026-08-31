"""The conversation profile: what he TOLD us, kept apart from what we VERIFIED (T5.5).

WHAT BREAKS WITHOUT IT. "There are six of us" and "I'm near Deira" live in the
transcript and NOWHERE ELSE — nothing structured captures them. That works today
only because Aisha reads the whole conversation. Sub-agents do not: they get a
brief, and the brief is the only channel, so anything not in it is invisible to
them. A customer who said "six of us" in message one and asks for a car in
message ten would be shown five-seaters.

IT IS NOT `session_facts`, AND MERGING THEM WOULD BREAK THE ONE INVARIANT THAT
KEEPS AISHA HONEST. session_facts is folded from TOOL RESULTS only, on purpose
(rule 2) — customer_id, name, mobile, the booking ref, the car once a slot is
locked. Family size never comes from a tool; it comes from somebody saying it.
Mixing an assertion into the grounded anchor would make "he has six children" as
authoritative as "booking TD-2026-HTXE exists", and nothing downstream could
tell them apart.

So: two layers, rendered separately, labelled differently. WHAT WE VERIFIED, and
WHAT HE TOLD US.

A CONSTRAINT, NEVER A PRECONDITION. The profile narrows what a tool is asked
for; it never decides whether an agent runs. `showroom_id` is the worked
example: the profile supplies it when it has it, get_available_slots already
accepts it, and TestDrive runs unchanged when it does not. That is why
showroom_id stays out of every card's `requires` — a precondition nothing
reliably produces deadlocks the ordering forever.

PERSISTENCE IS CONSENT-GATED, AND NOTHING HERE PERSISTS. This is working memory,
no different from the transcript being in context, until the customer says yes.
Then it promotes into MemoryProfile.notes through the consent gate and the
forget_me erasure path that already exist. There is deliberately no write in
this module.

BRIEFS CARRY THE PROFILE, NEVER THE PERSON. Constraints go down — seats >= 7,
near Deira, strong audio. Name, mobile and booking ref do not. Three more models
see conversation context from Phase 5, and this is the data-minimisation line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict

log = logging.getLogger("app.agent.profile")

# The soft facts worth carrying. Deliberately a CLOSED set: an open dictionary
# would become a place to stash anything, including the personal details the
# minimisation rule above exists to keep out of briefs.
FIELDS = (
    "seats_needed",      # "there are six of us"
    "area",              # "I'm near Deira"
    "budget_aed",        # "around 100k"
    "use_case",          # "mostly the school run"
    "must_haves",        # ["good sound system", "seven seats"]
    "fuel_preference",   # "something hybrid"
)

# Fields that may travel into a brief. Everything here is a CONSTRAINT on a
# search, not a fact about a person.
BRIEF_SAFE = frozenset(FIELDS)

# Never, under any circumstances, in a brief. Listed explicitly rather than
# left implicit, so a future field has to be argued about.
NEVER_IN_BRIEF = frozenset({
    "name", "mobile", "email", "booking_ref", "customer_id", "emirates_id",
})


@dataclass
class Profile:
    """Soft facts a customer volunteered. Asserted, never verified."""

    seats_needed: int | None = None
    area: str | None = None
    budget_aed: int | None = None
    use_case: str | None = None
    must_haves: list[str] = field(default_factory=list)
    fuel_preference: str | None = None

    def merge(self, **updates) -> "Profile":
        """Fold in what she just learned. Unknown keys are DROPPED, loudly.

        A closed set is only closed if writing outside it fails visibly —
        silently accepting `mobile` would put a phone number one render away
        from a brief.
        """
        for key, value in updates.items():
            if key not in FIELDS:
                log.warning("ignoring unknown profile field %r", key)
                continue
            if value in (None, "", []):
                continue
            if key == "must_haves":
                existing = list(self.must_haves)
                for item in (value if isinstance(value, list) else [value]):
                    if item and item not in existing:
                        existing.append(item)
                self.must_haves = existing
            else:
                setattr(self, key, value)
        return self

    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in FIELDS)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])}

    # ------------------------------------------------------------------ #
    def render(self) -> str:
        """The block that goes in Aisha's prompt, BESIDE session_facts.

        LABELLED AS ASSERTED, in the text itself. If it ever renders next to the
        grounded anchor without that label, a model has every reason to treat
        "six of us" as verified — and to state it back as though we checked.
        """
        if self.is_empty():
            return ""
        lines = ["WHAT THEY HAVE TOLD YOU (their words, not verified facts):"]
        if self.seats_needed:
            lines.append(f"- they need room for about {self.seats_needed}")
        if self.area:
            lines.append(f"- they are near {self.area}")
        if self.budget_aed:
            lines.append(f"- they mentioned a budget around AED {self.budget_aed:,}")
        if self.use_case:
            lines.append(f"- the car is mainly for {self.use_case}")
        if self.fuel_preference:
            lines.append(f"- they leaned towards {self.fuel_preference}")
        for item in self.must_haves:
            lines.append(f"- they care about {item}")
        lines.append(
            "Use these to narrow what you look up. They are what the customer "
            "SAID, so treat them as preferences you may check, never as facts "
            "you have confirmed — and do not read them back as though we had."
        )
        return "\n".join(lines) + "\n"

    def constraints_for_brief(self) -> dict:
        """What rides down to a sub-agent. Constraints only, never the person."""
        out = {k: v for k, v in self.as_dict().items() if k in BRIEF_SAFE}
        # Belt and braces against a future field being added to FIELDS without
        # anybody thinking about this line.
        for banned in NEVER_IN_BRIEF:
            out.pop(banned, None)
        return out


def from_state(state) -> Profile:
    """Read the profile out of graph state. Always returns one."""
    try:
        raw = (state or {}).get("profile") or {}
        return Profile().merge(**raw) if isinstance(raw, dict) else Profile()
    except Exception:  # noqa: BLE001
        return Profile()


def promotion_note(profile: Profile) -> str:
    """The free-text note save_memory would store, if the customer consents.

    REUSES THE EXISTING CONSENT-GATED PATH rather than inventing a second one:
    MemoryProfile.notes already exists for exactly this, and forget_me already
    clears it. Nothing new to defend on a privacy review.
    """
    if profile.is_empty():
        return ""
    bits = []
    if profile.seats_needed:
        bits.append(f"needs ~{profile.seats_needed} seats")
    if profile.area:
        bits.append(f"near {profile.area}")
    if profile.budget_aed:
        bits.append(f"budget ~AED {profile.budget_aed:,}")
    if profile.use_case:
        bits.append(f"for {profile.use_case}")
    if profile.fuel_preference:
        bits.append(f"prefers {profile.fuel_preference}")
    bits.extend(profile.must_haves)
    return "; ".join(bits)
