"""Running one sub-agent on Haiku (T5.2, bound at Phase 9).

WHAT THIS IS. `turn.execute` has always taken an injected `runner_fn` so the
whole cascade could be tested offline with a stub. This is the real one: it runs
ONE brief through Haiku with that agent's prompt and its READ-ONLY tools, and
returns structured findings.

IT REUSES `tool_node._dispatch` RATHER THAN CALLING TOOLS DIRECTLY, and that is
the load-bearing choice in this file. `_dispatch` injects `db`, `session_id` and
`customer_id` from trusted context, refuses a write from an unidentified session,
and turns every failure into an error envelope instead of an exception. A
sub-agent reaching TOOL_FNS itself would bypass all of that — and the one thing a
sub-agent must never be able to do is name its own customer.

READ-ONLY IS ENFORCED TWICE. The card declares reads only (asserted by
evals/differential.py), and this refuses to bind a write even if a card ever
gained one. Belt and braces, because the whole reason a sub-agent may run on a
cheaper model is that a bad generation cannot write.

IT RETURNS DATA, NEVER PROSE. The sub-agent prompts carry no persona, no Arabic
register and no output rules — those live in base/ and aisha/ alone. What comes
back is the tool results, which Aisha composes into the sentence a customer
reads. A sub-agent that started writing customer-facing prose would be a second
voice with none of the guardrails.

EVERY TOOL EVENT IS FORWARDED to the parent stream. Miss that and the cards
silently vanish from the UI with no error anywhere — see turn.forward_events.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date

log = logging.getLogger("app.orchestrator.subagent")

# What a sub-agent is told to produce. Short and blunt: it is appended to a
# prompt that already carries the agent's own rules, and its only job is to stop
# a model that has just read pages of conversational guidance from answering
# conversationally.
FINDINGS_INSTRUCTION = """\
YOU ARE NOT TALKING TO THE CUSTOMER. Another assistant is. Your job is to gather \
facts for her using your tools and then stop.

Call the tools you need. When you have what the request asked for, reply with a \
single line of JSON and nothing else, in this shape:

{"findings": {...}, "note": "one short sentence for the assistant, not the customer"}

Put the ids the next step will need in `findings` (vehicle_id, variant_id, \
slot_id and so on) alongside the facts. If you could not find something, say so \
in `note` and leave it out of `findings` rather than guessing it.

KEEP `findings` SHORT. Everything your tools returned is passed on in full \
already — you do not need to copy prices, specifications, tables or lists out \
of a tool result, and re-typing them wastes the customer's time and risks \
changing a number. Put the ids, anything you WORKED OUT that is not already in \
the results, and nothing else.

Do not greet, do not apologise, do not write to the customer, and do not format \
anything. One JSON object.
"""

# Appended ONLY for an agent whose card declares `proposes`. A sub-agent that
# cannot propose anything must not be told the vocabulary exists — an agent
# offered a booking verb it may not use will eventually reach for it, and every
# such attempt is a refusal the customer waits through.
#
# PROPOSING IS NOT DOING, and the wording has to carry that or the model reports
# success. It asks; orchestrator/write.py decides; the service guards beneath
# still refuse independently. A sub-agent that said "I've booked it" in its note
# would put a lie in front of Aisha with nothing to catch it.
PROPOSALS_INSTRUCTION = """\

ASKING FOR SOMETHING TO BE DONE. You cannot change anything yourself — you have \
no tools that write. What you CAN do is ASK, by adding a `proposals` list beside \
your findings:

{"findings": {...}, "proposals": [{"action": "hold_slot", "args": {"slot_id": 12}}], "note": "..."}

The actions you may ask for are: %s

An entry is a REQUEST, not a result. It may be refused — because the step before \
it has not happened, because the slot went, because a rule says not yet. So never \
write in your note that something HAS been done, booked, held or cancelled. Say \
what you asked for. Somebody else decides, and the customer is told by somebody \
else.

Only propose what the request actually asked for, and leave `proposals` out \
entirely when it asked for nothing.
"""


def _client():
    from app.agent.graph import _build_client

    return _build_client()


def _schemas_for(agent: str) -> list[dict]:
    """The agent's card tools, filtered to reads. Never returns a write."""
    from app.agents import loader
    from app.tools import SUBAGENT_REGISTRY, WRITE_TOOLS

    declared = list(loader.frontmatter(agent).get("tools") or [])
    out = []
    for name in declared:
        if name in WRITE_TOOLS:
            # Should be impossible — differential.py fails the build first — but
            # this is the last gate before a cheap model gets a write.
            log.error("refusing to bind write tool %r to sub-agent %s", name, agent)
            continue
        entry = SUBAGENT_REGISTRY.get(name)
        if entry is None:
            # A CARD NAMING A TOOL THAT DOES NOT EXIST IS A BUILD ERROR, not a
            # thing to skip. This used to `continue` silently, and it cost the
            # finance agent every one of its three tools: it ran with an empty
            # `tools` list, could call nothing, and reported "blocked" as though
            # the dependency ordering had stopped it. Nothing failed, nothing
            # logged, and the smoke test read as a pass.
            # tests/test_subagent_tools.py fails the build on this now; the
            # error here is the runtime half of the same guard.
            log.error(
                "card for %s declares unknown tool %r — the agent will run "
                "without it", agent, name,
            )
            continue
        out.append(entry["schema"])
    return out


def _proposes(agent: str) -> list[str]:
    """What this agent's card says it may ASK for. Empty for most agents."""
    from app.agents import loader
    from app.orchestrator.write import PROPOSABLE

    declared = list(loader.frontmatter(agent).get("proposes") or [])
    out = []
    for name in declared:
        if name not in PROPOSABLE:
            # A card asking for something write.py cannot perform. Reported
            # rather than skipped: differential.py fails the build on it, and
            # this is the runtime half of the same guard.
            log.error("card for %s proposes unknown action %r", agent, name)
            continue
        out.append(name)
    return out


def _proposals_clause(agent: str) -> str:
    actions = _proposes(agent)
    if not actions:
        return ""
    return PROPOSALS_INSTRUCTION % ", ".join(actions)


def _validate_proposals(agent: str, raw: list[dict]) -> list[dict]:
    """Keep only what this agent's card actually permits it to ask for.

    THE CARD IS THE ALLOWLIST, not the prompt. A model that invents an action —
    or borrows one from another agent's vocabulary — has its request dropped
    here, before write.py ever sees it. write.py refuses it again; neither gate
    is redundant, because this one names the AGENT and that one names the STATE.
    """
    allowed = set(_proposes(agent))
    out = []
    for item in raw or []:
        action = str(item.get("action") or "").strip()
        if action not in allowed:
            log.warning("dropping %r proposed by %s (not on its card)", action, agent)
            continue
        args = item.get("args")
        out.append({
            "action": action,
            "args": dict(args) if isinstance(args, dict) else {},
            "agent": agent,
        })
    return out


def _harvest_entities(agent: str, tool_results: list[dict]) -> dict:
    """Pull the entities this agent DECLARES IT PRODUCES out of its tool results.

    WHY THIS EXISTS, FOUND BY RUNNING IT FOR REAL. The findings instruction asks
    the model to put the ids the next step needs into its JSON. Live, Haiku
    returned `{"results": ..., "search_performed": true}` for a Sales brief whose
    card declares `produces: [vehicle_id, variant_id]` — so nothing threaded
    forward, and the TestDrive step that depended on it ran with an empty
    context and found nothing.

    ASKING HARDER IS THE WRONG FIX, and this repo has learned that twice already
    (see agent/sanitise.py on rule 17). The ids are not something the model needs
    to remember: they are sitting in the tool result it just received. Reading
    them is deterministic and done once.

    FIRST OCCURRENCE WINS, and it is the right choice: a search returns a ranked
    list, and the first row is the one it recommended. Later steps get the car it
    actually led with rather than the last one it happened to mention.
    """
    from app.orchestrator.order import produces

    wanted = produces(agent)
    if not wanted:
        return {}

    found: dict = {}

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in wanted and key not in found and isinstance(value, (int, str)):
                    found[key] = value
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for result in tool_results:
        _walk(result)
        if wanted <= set(found):
            break
    return found


def _parse_findings(text: str) -> dict:
    """Pull the JSON object out of the reply. Tolerant, but never invents.

    A model that has just read a long conversational prompt sometimes wraps its
    JSON in a sentence. Extracting it is fine; GUESSING at findings when there is
    no JSON is not — that returns an empty result and says so, so the step is
    reported honestly rather than filled in.
    """
    text = (text or "").strip()
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(parsed, dict):
        return {}
    findings = parsed.get("findings")
    if isinstance(findings, dict):
        out = dict(findings)
    else:
        # THE WRAPPER IS OPTIONAL, BECAUSE THE MODEL TREATS IT AS OPTIONAL.
        # Measured live 2026-08-29: a Sales step wrote 1158 chars of valid JSON
        # with the results at the TOP level — no `findings` wrapper — and every
        # byte was discarded here, six seconds of generation for nothing. The
        # same shape is in _harvest_entities' docstring from an earlier run, so
        # this is the second time it has cost something.
        #
        # ASKING HARDER IS THE WRONG FIX (agent/sanitise.py, rule 17, and the
        # findings instruction has already been sharpened once). The reply IS
        # the findings when it is not wrapped in one — reading it that way is
        # deterministic, and the reserved keys stay reserved so a model cannot
        # smuggle a proposal in by omitting the wrapper.
        out = {
            k: v for k, v in parsed.items()
            if k not in ("note", "proposals", "findings")
        }
    if parsed.get("note"):
        out["_note"] = str(parsed["note"])
    # Carried under a reserved key so it rides the existing runner contract
    # rather than widening it; run.py lifts it off into StepResult.proposals so
    # it never reaches Aisha's compose payload as if it were a fact.
    proposals = parsed.get("proposals")
    if isinstance(proposals, list) and proposals:
        out["_proposals"] = [p for p in proposals if isinstance(p, dict)]
    return out


def run_step(
    step,
    context: dict,
    *,
    db,
    session_id: str,
    customer_id=None,
    emit=None,
    timeout: float = 20.0,
    max_iterations: int = 6,
    model: str | None = None,
    profile: dict | None = None,
) -> dict:
    """Run one brief on Haiku and return its findings. Never raises.

    The signature matches what `turn.execute` passes its runner, so this drops
    in where the stub was.
    """
    from app import config
    from app.agent import graph as _graph
    from app.agent import tool_node
    from app.agent.prompt import _DATE_ANCHOR, _relative_dates
    from app.agents import loader
    from app.tools import SUBAGENT_TOOL_FNS

    started = time.monotonic()
    model = model or config.CLAUDE_MODEL
    correlation_id = uuid.uuid4().hex

    # THE DATE ANCHOR, and it is not optional (found live, 2026-08-29). A
    # sub-agent's prompt is its card body and nothing else, so it had no idea
    # what day it was: asked for "slots at Deira on Saturday" it resolved
    # Saturday to 11 Jan 2025, looked up that date, and reported "no slots
    # available" — a wrong answer that reads exactly like a right one, because
    # the date it used never appears in the findings.
    #
    # The single agent has been immune since prompt.py:510 appends this same
    # anchor: Python owns the calendar and the model only reads it out. The
    # sub-agents were split off that path and inherited none of it. Any agent
    # that can be asked about a day needs it, so it goes on ALL of them rather
    # than on TestDrive alone — Finance quotes tenures, Sales quotes arrival
    # dates, and the next one has not been written yet.
    system = (
        loader.body_for(step.agent)
        + _DATE_ANCHOR.format(**_relative_dates(date.today()))
        + "\n\n"
        + FINDINGS_INSTRUCTION
        + _proposals_clause(step.agent)
    )
    schemas = _schemas_for(step.agent)

    # WHAT THE SUB-AGENT IS TOLD, AND WHAT IT IS NOT. The brief, the entities an
    # earlier step produced, and the customer's stated CONSTRAINTS. Never a name,
    # a mobile or a booking ref — three more models see conversation context from
    # here, and this is the data-minimisation line (see agent/profile.py).
    parts = [f"Request: {step.brief}"]
    known = {k: v for k, v in (context or {}).items() if v is not None}
    if known:
        parts.append(f"Already established: {json.dumps(known)}")
    if profile:
        parts.append(f"What the customer has said they want: {json.dumps(profile)}")
    messages: list[dict] = [{"role": "user", "content": "\n".join(parts)}]
    seen_results: list[dict] = []
    # WHAT THE TOOLS RETURNED, kept whole. seen_results above is the bare list
    # _harvest_entities reads ids out of; this one keeps the tool NAME with the
    # result, because Aisha cannot use `{"rows": [...]}` without knowing it came
    # from compare_vehicles. See _EVIDENCE in run.py for why she gets it at all.
    evidence: list[dict] = []
    # WHAT THE STEP ACTUALLY DID, for the line logged on the way out. Without
    # it a slow cascade is opaque from Aisha's side: her log shows only that
    # the orchestrator ran, never which specialist called what, how many times,
    # or where the seconds went. Every duplicate card on screen is one of these
    # calls, so the tool sequence IS the explanation for a repeated card.
    called: list[str] = []
    rounds = 0
    # Summed across the step's rounds. `cache_read` is the number that says
    # whether the caching above is working at all: it is 0 on the first round
    # of the day and should dominate every round after.
    usage = {"uncached": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    # WHERE THE OUTPUT TOKENS WENT. A step costs ~1s + 17.5ms per token it
    # writes, so this is the whole latency story — and the `entities=` line in
    # run.py cannot tell it, because that lists only the SURVIVING keys. A long
    # `note` (underscored, so filtered out of that list) or prose wrapped around
    # the JSON (discarded by _parse_findings) is billed in full and shows up
    # nowhere. One step wrote 483 tokens and yielded two keys; these three
    # numbers are what say which of those it was.
    args_chars = 0
    reply_chars = 0
    json_chars = 0

    client = _client()

    for _ in range(max_iterations):
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"{step.agent} exceeded {timeout:.0f}s")

        rounds += 1
        response = client.messages.create(
            model=model,
            # 400, DOWN FROM 1024 (2026-08-29). A tool round needs ~50 tokens of
            # arguments; the findings round needs ids and one note — yet Haiku
            # was measured spending ~6s of an 11s cascade writing 1,156 tokens
            # of JSON it had been told not to write, copying tool results the
            # evidence channel already carries. The instruction asks for
            # brevity; this enforces it. Entities still thread even through a
            # truncated reply: _harvest_entities reads them from the raw tool
            # results, not from the model's JSON.
            max_tokens=400,
            # CACHED, AND IT WAS NOT (found live, 2026-08-29). A card body is
            # 7-10k tokens and this loop re-sent every one of them, uncached,
            # on every round — while the single agent had cached its prefix
            # since day one. One compare turn spent ~84k input tokens re-reading
            # the same sales card nine times. The two breakpoints are the same
            # pair graph.py uses and are documented there: `tools -> system` is
            # the fixed render order, so marking the end of `system` caches the
            # schemas with it, and the 1h TTL is right for the same reason it is
            # right for Aisha — the next customer, not the next round, is who
            # re-reads it.
            #
            # WHAT KEEPS IT HITTING: _schemas_for(agent) must stay byte-stable,
            # which it is (card frontmatter order, no per-session assembly), and
            # `system` is the card body plus a DATE-granular anchor — so it rolls
            # once at midnight and not per request. A cache_read of 0 in the
            # `settled` line below is the only detector if that ever changes.
            system=_graph._system_blocks(system),
            tools=schemas,
            messages=_graph._cached_messages(messages),
        )

        u = getattr(response, "usage", None)
        if u is not None:
            usage["uncached"] += getattr(u, "input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["output"] += getattr(u, "output_tokens", 0) or 0

        blocks = list(response.content or [])
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]

        if not tool_uses:
            text = "".join(
                getattr(b, "text", "") for b in blocks
                if getattr(b, "type", None) == "text"
            )
            reply_chars = len(text)
            _s, _e = text.find("{"), text.rfind("}")
            json_chars = (_e - _s + 1) if (_s != -1 and _e > _s) else 0
            findings = _parse_findings(text)
            # THE DECLARED ENTITIES COME FROM THE TOOL RESULTS, NOT THE MODEL.
            # Whatever it wrote is kept as context; the ids the next step needs
            # are read from what the tools actually returned. See
            # _harvest_entities for the live failure that made this necessary.
            harvested = _harvest_entities(step.agent, seen_results)
            for key, value in harvested.items():
                findings.setdefault(key, value)
            if "_proposals" in findings:
                kept = _validate_proposals(step.agent, findings["_proposals"])
                if kept:
                    findings["_proposals"] = kept
                else:
                    findings.pop("_proposals")
            # Lifted off findings by run.execute, like _proposals. Underscored
            # so it can never be mistaken for something the model wrote.
            if evidence:
                findings["_evidence"] = evidence
            log.info(
                "%s settled in %d round(s), %dms — model=%s tools=[%s] "
                "(uncached=%d cache_write=%d cache_read=%d output=%d)",
                step.agent, rounds, int((time.monotonic() - started) * 1000),
                model,
                ", ".join(called) or "none",
                usage["uncached"], usage["cache_write"],
                usage["cache_read"], usage["output"],
            )
            log.info(
                "%s wrote: %d chars of tool args, %d chars of reply "
                "(%d json, %d discarded prose), note %d chars, %d finding key(s)",
                step.agent, args_chars, reply_chars, json_chars,
                reply_chars - json_chars,
                len(findings.get("_note") or ""),
                len([k for k in findings if not k.startswith("_")]),
            )
            return findings

        messages.append({"role": "assistant", "content": blocks})
        results = []
        for use in tool_uses:
            called.append(use.name)
            args_chars += len(json.dumps(dict(use.input or {}), default=str))
            result = tool_node._dispatch(
                db, session_id, use.name, dict(use.input or {}),
                customer_id, correlation_id,
                # The sub-agent surface: TOOL_FNS plus the five card reads.
                # Passed explicitly so the single agent's dispatch is untouched.
                fns=SUBAGENT_TOOL_FNS,
            )
            # FORWARDED, OR THE CARDS VANISH. runner._card_anchor builds the
            # vehicle and slot cards from these; a sub-agent that emits them
            # nowhere produces a turn with no cards and no error.
            if emit is not None:
                emit("tool_event", {"tool": use.name, "input": dict(use.input or {}),
                                    "output": result})
            seen_results.append(result)
            evidence.append({"tool": use.name, "result": result})
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": json.dumps(result, default=str)[:8000],
            })
        messages.append({"role": "user", "content": results})

    # ITERATIONS EXHAUSTED IS A FAILED STEP, not a silent empty one. A sub-agent
    # still looping after six rounds on one brief is lost, and the honest outcome
    # is a step the customer hears about.
    log.error("%s did not settle in %d rounds, %dms — tools=[%s]",
              step.agent, max_iterations, int((time.monotonic() - started) * 1000),
              ", ".join(called) or "none")
    raise RuntimeError(f"{step.agent} did not settle in {max_iterations} rounds")


def make_runner(
    db,
    session_id: str,
    *,
    customer_id=None,
    profile: dict | None = None,
    session_factory=None,
):
    """Bind the trusted context once and hand back the runner turn.execute wants.

    The context is closed over rather than passed through the step, so nothing a
    model produced can reach it.

    `session_factory` IS WHAT MAKES A PARALLEL WAVE LEGAL. A SQLAlchemy Session
    is not thread-safe, so two steps running at once on the shared `db` would
    interleave queries on one connection — which does not raise reliably, it
    corrupts results. Given a factory, each step opens and closes its own
    session and touches nothing the caller owns. Omit it and the runner behaves
    exactly as before, on the caller's session, single-threaded: run.execute
    will not widen a wave it cannot give sessions to.

    SAFE BECAUSE SUB-AGENTS ONLY READ. Their cards bind read-only tools and
    _dispatch never commits here — the write path is _execute_proposals, which
    runs afterwards on the caller's session, in one thread, as it always did.
    """
    def _runner(step, context, *, emit=None, timeout=20.0, max_iterations=6):
        if session_factory is None:
            return run_step(
                step, context, db=db, session_id=session_id,
                customer_id=customer_id, emit=emit, timeout=timeout,
                max_iterations=max_iterations, profile=profile,
            )

        own = session_factory()
        try:
            return run_step(
                step, context, db=own, session_id=session_id,
                customer_id=customer_id, emit=emit, timeout=timeout,
                max_iterations=max_iterations, profile=profile,
            )
        finally:
            # Never commits: whatever a read left pending is discarded with the
            # session, so a sub-agent cannot persist anything by accident.
            own.rollback()
            own.close()

    # run.execute reads this to decide whether a wave may be widened.
    _runner.thread_safe = session_factory is not None  # type: ignore[attr-defined]
    return _runner
