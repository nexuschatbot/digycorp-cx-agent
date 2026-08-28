"""Dynamic prompt anchors.

THE STATIC PROSE IS NO LONGER HERE (T2.1). base.py, testdrive.py,
bookingmanagement.py and compose.py were deleted: their text now lives in
app/agents/{base,aisha}/v1.md, where it is versioned, gated and editable without
a deploy. app/agents/loader.py assembles it, and the assembled body is still
byte-identical to what shipped — tests/test_prompt_versions.py asserts that
against the same fixture that has guarded it since S5.

WHY DELETED RATHER THAN LEFT IN PLACE. Two copies of the prompt is one copy that
drifts, and the one that drifts is always the one nobody is looking at. The
Markdown is the source of truth; there is deliberately no second one.

WHAT REMAINS. anchors.py — the DYNAMIC blocks, composed per turn from live state
(today's date, the session facts, the booking anchor). They are code because they
are computed, not written, so nothing about them belongs in a file an editor
opens.
"""

from app.agent.prompts import anchors

__all__ = ["anchors"]
