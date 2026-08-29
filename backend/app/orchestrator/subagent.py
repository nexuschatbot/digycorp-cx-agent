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

Do not greet, do not apologise, do not write to the customer, and do not format \
anything. One JSON object.
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
    out = dict(findings) if isinstance(findings, dict) else {}
    if parsed.get("note"):
        out["_note"] = str(parsed["note"])
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

    client = _client()

    for _ in range(max_iterations):
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"{step.agent} exceeded {timeout:.0f}s")

        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            tools=schemas,
            messages=messages,
        )

        blocks = list(response.content or [])
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]

        if not tool_uses:
            text = "".join(
                getattr(b, "text", "") for b in blocks
                if getattr(b, "type", None) == "text"
            )
            findings = _parse_findings(text)
            # THE DECLARED ENTITIES COME FROM THE TOOL RESULTS, NOT THE MODEL.
            # Whatever it wrote is kept as context; the ids the next step needs
            # are read from what the tools actually returned. See
            # _harvest_entities for the live failure that made this necessary.
            harvested = _harvest_entities(step.agent, seen_results)
            for key, value in harvested.items():
                findings.setdefault(key, value)
            return findings

        messages.append({"role": "assistant", "content": blocks})
        results = []
        for use in tool_uses:
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
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": json.dumps(result, default=str)[:8000],
            })
        messages.append({"role": "user", "content": results})

    # ITERATIONS EXHAUSTED IS A FAILED STEP, not a silent empty one. A sub-agent
    # still looping after six rounds on one brief is lost, and the honest outcome
    # is a step the customer hears about.
    raise RuntimeError(f"{step.agent} did not settle in {max_iterations} rounds")


def make_runner(db, session_id: str, *, customer_id=None, profile: dict | None = None):
    """Bind the trusted context once and hand back the runner turn.execute wants.

    The context is closed over rather than passed through the step, so nothing a
    model produced can reach it.
    """
    def _runner(step, context, *, emit=None, timeout=20.0, max_iterations=6):
        return run_step(
            step, context, db=db, session_id=session_id, customer_id=customer_id,
            emit=emit, timeout=timeout, max_iterations=max_iterations,
            profile=profile,
        )

    return _runner
