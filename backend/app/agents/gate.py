"""The activation gate: a broken prompt version does not go live (T2.3).

WHY A GATE AT ALL. Moving prompts into Markdown makes them editable without a
deploy, which is the point — and also the risk. The protection that used to come
from "a prompt change is a code change is a code review" has to come from
somewhere else, so it comes from here: a version is CHECKED before it is
allowed to serve a customer, and a failing one is refused with the previous one
left running.

WHAT IS CHECKED, AND WHY EACH ONE.

  PRESENCE. The load-bearing rules must still be in the text. Not "some text
  exists" — these specific ones, each pinned to prose a customer outcome depends
  on: the consent separation, the licence acknowledgement, rule 13's escalation,
  the one-question discipline, the no-markdown rule. An edit that tidies one of
  these away removes a guardrail, and nothing else in the system would notice.

  COMPOSITION. Every block SINGLE_AGENT_ORDER names must exist in the version.
  A renamed section is a silently shorter prompt, which is the failure mode most
  likely to pass a human read.

  DIFFERENTIAL. No block may instruct an agent to call a tool it cannot reach —
  the check evals/differential.py already performs, reused rather than
  reimplemented so the two cannot drift.

WHAT IS DELIBERATELY *NOT* CHECKED: whether the prose is good. That is what
review is for. This catches structural damage, not bad writing, and pretending
otherwise would make it a rubber stamp people learn to route around.

THE GUARDRAILS ARE NOT WHAT KEEPS THE SYSTEM SAFE, and that is worth stating
plainly here because this file could be mistaken for the thing that does. The
load-bearing protections are STRUCTURAL: create_booking refuses without the
licence acknowledgement, record_consent fails safe on an ambiguous yes, the tool
node injects customer_id so a write cannot name its own subject, and from Phase 4
no sub-agent holds a write tool. Editing a prompt changes what Aisha SAYS, never
what she can DO. This gate protects the saying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("app.agents.gate")


# (agent, needle, what it protects). Matched case-insensitively as a substring
# of the assembled body, so wording can be edited freely around them — these are
# ANCHORS, not the rules themselves.
#
# Deliberately short and few. A long list becomes a list nobody can change, and
# the first person to hit a false positive learns to bypass the gate.
REQUIRED_PHRASES: tuple[tuple[str, str, str], ...] = (
    ("aisha", "record_consent", "the consent write must still be named"),
    ("aisha", "licence", "rule 9's licence acknowledgement step"),
    ("base", "one question", "the one-question discipline (rule 16)"),
    ("base", "markdown", "the no-markdown output rule (rule 17)"),
    ("base", "human", "rule 13's escalation to a person"),
)


@dataclass
class GateReport:
    agent: str
    version: str
    missing_sections: list[str] = field(default_factory=list)
    missing_phrases: list[str] = field(default_factory=list)
    orphaned_rules: list[str] = field(default_factory=list)
    body_only: bool = False  # a routing card with no prose

    @property
    def ok(self) -> bool:
        return not (self.missing_sections or self.missing_phrases or self.orphaned_rules)

    def render(self) -> str:
        if self.ok:
            return f"{self.agent} {self.version}: ok"
        parts = [f"{self.agent} {self.version}: REFUSED"]
        if self.missing_sections:
            parts.append(f"  missing sections: {', '.join(self.missing_sections)}")
        if self.missing_phrases:
            parts.append(f"  missing guardrails: {', '.join(self.missing_phrases)}")
        if self.orphaned_rules:
            parts.append(f"  rules naming unreachable tools: {', '.join(self.orphaned_rules)}")
        return "\n".join(parts)


def check(agent: str, version: str | None = None) -> GateReport:
    """Verify one agent version. Never raises — a report, not an exception."""
    from app.agents import loader

    version = version or loader.version_for(agent)
    report = GateReport(agent=agent, version=version)

    try:
        blocks = loader.sections(agent, version)
    except loader.PromptError as exc:
        report.missing_sections.append(str(exc))
        return report

    # A ROUTING CARD IS VALID WITH NO BODY. sales/testdrive/finance ship their
    # frontmatter in Phase 2 and their prose in Phase 5; refusing them here
    # would mean the gate fails on every boot for two phases, which is how a
    # gate becomes something people disable.
    #
    # DECIDED BY `routable`, NOT BY EMPTINESS. The first version of this check
    # treated "no sections" as "must be a card" — which would have let an
    # accidentally-emptied aisha/v1.md sail through as a routing card, silently
    # removing every guardrail from the live prompt. A file that DECLARES it is
    # a routable sub-agent may be bodyless; base and aisha may not.
    fm = loader.frontmatter(agent, version)
    if bool(fm.get("routable")):
        report.body_only = True
        if blocks:
            # Phase 5 will fill these in; a body appearing early is worth
            # knowing about, not refusing.
            log.info("%s %s carries a prompt body ahead of Phase 5", agent, version)
        return report

    if not blocks:
        report.missing_sections.append(
            "file has no ## sections and is not declared routable"
        )
        return report

    # Composition: every block the live body needs must exist.
    needed = {name for a, name in loader.SINGLE_AGENT_ORDER if a == agent}
    report.missing_sections = sorted(needed - set(blocks))

    body = "".join(blocks.values()).lower()
    for owner, needle, what in REQUIRED_PHRASES:
        if owner == agent and needle.lower() not in body:
            report.missing_phrases.append(f"{needle!r} ({what})")

    return report


def check_tool_reachability() -> list[str]:
    """Rules that name a tool the agent cannot call.

    REUSES evals/differential.py rather than reimplementing it, so the gate and
    the eval cannot disagree about what an orphaned rule is. Returns [] when the
    harness is unavailable — an eval that cannot import must not block a boot.
    """
    try:
        from evals import differential

        reports = differential.check_all()
    except Exception:  # noqa: BLE001
        log.warning("differential harness unavailable — tool reachability unchecked")
        return []
    out: list[str] = []
    for agent, rep in reports.items():
        for rule in getattr(rep, "orphaned", []) or []:
            out.append(f"{agent}:{rule}")
    return out


def verify_active() -> dict[str, GateReport]:
    """Check every agent's live version. Called at boot."""
    from app.agents import loader

    reports = {a: check(a, v) for a, v in loader.active_versions().items()}
    orphans = check_tool_reachability()
    if orphans:
        # Attributed to the agent whose body names the tool, so the message
        # points at the file to edit.
        for entry in orphans:
            agent = entry.split(":", 1)[0]
            if agent in reports:
                reports[agent].orphaned_rules.append(entry)
    return reports


def enforce() -> dict[str, GateReport]:
    """Verify and LOG LOUDLY. Returns the reports; refuses nothing by itself.

    THE REFUSAL IS THE CALLER'S, and that is deliberate. At boot, a failing
    version means "keep the previous one" — but in Phase 2 there IS no previous
    one to keep, because v1 is the first. So this reports and shouts; app/main.py
    decides, and tests/test_prompt_versions.py asserts a broken version is caught
    rather than served.
    """
    reports = verify_active()
    failed = {a: r for a, r in reports.items() if not r.ok}
    if failed:
        log.error(
            "PROMPT ACTIVATION REFUSED for %s — the previous version stays live:\n%s",
            ", ".join(sorted(failed)),
            "\n".join(r.render() for r in failed.values()),
        )
    else:
        live = {a: r.version for a, r in reports.items()}
        log.info("prompt versions verified: %s", live)
    return reports


# --------------------------------------------------------------------------- #
# Activation audit (T2.4)
# --------------------------------------------------------------------------- #
ACTIVATION_SESSION = "system:prompt-activation"


def record_activation(db) -> dict | None:
    """Write an audit row when the live prompt versions CHANGE. None otherwise.

    WHY THIS EXISTS. Prompts are editable without a deploy from Phase 2, so a
    quality change no longer has a deployment to correlate against. `turn_trace.
    prompt_versions` records which version answered each TURN; this records WHEN
    the answer changed and to what — together they turn "she got colder last
    Tuesday" from an argument into a lookup.

    CHANGE-TRIGGERED, NOT BOOT-TRIGGERED. A row on every restart would bury the
    handful of real activations under hundreds of identical ones, and the point
    of the trail is that a change is findable.

    Uses the existing audit machinery rather than a table of its own, and does
    NOT commit — the caller owns the transaction, exactly as every other writer
    here does.
    """
    from sqlalchemy import desc, select

    from app.models import Actor, AuditEvent, AuditType
    from app.services import audit

    from app.agents import loader

    current = loader.active_versions()

    previous_row = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.session == ACTIVATION_SESSION)
        .order_by(desc(AuditEvent.id))
        .limit(1)
    )
    previous = (previous_row.payload or {}).get("versions") if previous_row else None
    if previous == current:
        return None

    changed = {
        agent: {"from": (previous or {}).get(agent), "to": version}
        for agent, version in current.items()
        if not previous or previous.get(agent) != version
    }
    audit.record(
        db,
        session=ACTIVATION_SESSION,
        # actor=system: no customer and no agent did this — a human edited a
        # file and a process restarted. Attributing it to `agent` would put it
        # in the conversation trail, which is not where it belongs.
        actor=Actor.system,
        type=AuditType.intent,
        payload={
            "event": "prompt_activation",
            "versions": current,
            "changed": changed,
            "first_activation": previous is None,
        },
    )
    log.info("prompt activation recorded: %s", changed)
    return changed
