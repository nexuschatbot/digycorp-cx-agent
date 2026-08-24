# CLAUDE.md — Elite Dealership AI (multi-agent)

## Non-negotiable rules

1. **DO NOT ASSUME. EVER.** If anything is unclear, ambiguous, or has more than one
   reasonable interpretation — STOP and ask before doing anything. This applies to
   requirements, file locations, naming, library choices, data shapes, and scope.
   A wrong guess costs more than a question.
2. **Ask before acting.** No file creation, deletion, refactor, dependency install,
   or schema change without confirming the intent first. Propose, wait for approval,
   then execute.
3. **Plans are short.** Bullet points, one line each. No extended paragraphs, no
   essays. If a plan needs more than ~8 bullets, split it and ask which part to do.

## Git

- Commit messages: NEVER add "Co-Authored-By: Claude" or any AI attribution.
- Keep messages short: imperative, one line, e.g. `add haiku router node`.

## Project context

- Architecture: see `docs/elite-multiagent-architecture.md` (v1.2). Follow it;
  don't relitigate its standing decisions (§11).
- Migration rule zero: **port, don't re-author.** Battle-tested prompt prose,
  comments, anchors, tools, and tests move verbatim. Only assembly code
  (prompt composer, router, tool binding) is new.
- Existing tests must stay green at every step.
