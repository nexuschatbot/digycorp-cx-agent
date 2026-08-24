"""WhatsApp Cloud API sender (Meta Graph API) — real outbound messaging.

The ONLY code that talks to Meta. A thin wrapper over
`POST /{phone_number_id}/messages`; every caller is the notification dispatcher
(services/dispatcher), never a tool — sending is a side effect of a booking, not
something the agent decides (same rule as services/notifications).

NO-OP WHEN UNCONFIGURED. With WHATSAPP_ENABLED off, or a missing token /
phone_number_id, `send_text` returns `{"ok": False, "skipped": True}` and the
dispatcher treats the row as a SIMULATOR send — so the text demo and the tests
run unchanged with no credentials, exactly as before this existed.

NEVER RAISES. A transport error or a Meta rejection comes back as a result dict
the dispatcher records and audits; a raised exception would kill the scheduler
tick.

SANDBOX NOTE — FREE-FORM TEXT ONLY. A `text` message is a business-initiated
message and Meta only delivers it inside the recipient's open 24-hour
customer-service window (opened when they message the business number first).
Outside that window Meta returns error 131047/131051 and the dispatcher retries
until NOTIFY_MAX_ATTEMPTS. Production uses approved message TEMPLATES instead —
see the Meta setup notes; this module would grow a `send_template` sibling then.

⚠️ D1 PDPL CHECKLIST: this is customer data (name, mobile, booking ref) leaving
the region to a Meta US/global endpoint. Sandbox / fictitious-data demo only.
"""

from __future__ import annotations

import logging

import httpx

from app import config

log = logging.getLogger("app.services.whatsapp")

_TIMEOUT_SECONDS = 15.0


def is_configured() -> bool:
    """True only when a real send can be attempted. The dispatcher branches on
    this to fall back to simulator behaviour rather than erroring."""
    return bool(
        config.WHATSAPP_ENABLED
        and config.WHATSAPP_ACCESS_TOKEN
        and config.WHATSAPP_PHONE_NUMBER_ID
    )


def _to_msisdn(mobile: str) -> str:
    """Meta wants the number as digits with the country code, no leading '+'."""
    return (mobile or "").strip().lstrip("+")


def send_text(to: str, body: str) -> dict:
    """Send one free-form WhatsApp text. Returns a result dict, never raises.

    {"ok": True, "message_id": "wamid..."}                    — delivered to Meta
    {"ok": False, "skipped": True, "reason": ...}             — not configured
    {"ok": False, "reason": "transport_error"|"api_error", ...} — a real failure
    """
    if not is_configured():
        return {"ok": False, "skipped": True, "reason": "whatsapp_not_configured"}

    url = (
        f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}"
        f"/{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _to_msisdn(to),
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a scheduler tick must not die here
        log.warning("whatsapp send failed (transport) to %s: %s", _to_msisdn(to), exc)
        return {"ok": False, "reason": "transport_error", "detail": str(exc)}

    if resp.status_code // 100 == 2:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        message_id = None
        messages = data.get("messages") if isinstance(data, dict) else None
        if isinstance(messages, list) and messages:
            message_id = messages[0].get("id")
        return {"ok": True, "message_id": message_id}

    # Meta returns a structured error object — keep it for the audit trail so a
    # closed 24h window / unverified recipient is visible in the console.
    try:
        error = resp.json().get("error", {})
    except Exception:  # noqa: BLE001
        error = {"message": resp.text}
    log.warning("whatsapp send rejected (%s) to %s: %s", resp.status_code, _to_msisdn(to), error)
    return {"ok": False, "reason": "api_error", "status": resp.status_code, "error": error}


# --------------------------------------------------------------------------- #
# Interactive LIST messages — the feedback survey's tappable 1-5 (2026-07-26)
# --------------------------------------------------------------------------- #
# WHY A LIST AND NOT BUTTONS: Meta caps interactive REPLY BUTTONS at three, so a
# 1-5 scale cannot be buttons at all. A list message allows ten rows across ten
# sections, which fits five comfortably. This is a hard platform limit, not a
# design preference — see the feedback survey notes in CLAUDE.md.
#
# WHY THE REPLY IS AN ID, NOT TEXT: the customer taps a row and Meta sends back
# `interactive.list_reply.id` verbatim. We set that id ourselves ("q1:5"), so the
# answer arrives already parsed and already bound to the question it answers.
# There is no "was 'four' a 4?" and no chance of a stale tap being filed against
# whichever question happens to be current — the id carries both halves.
#
# 24-HOUR WINDOW APPLIES, exactly as it does to send_text: an interactive message
# is still a free-form message, so Meta only delivers it inside the recipient's
# open customer-service window. The survey's opening is the one message that
# risks falling outside it; every question after that is a reply within a live
# conversation, so the window is open by definition.
_ROW_TITLE_MAX = 24  # Meta's limit; a longer title is rejected outright
_ROW_DESC_MAX = 72


def send_interactive_list(
    to: str,
    *,
    body: str,
    button: str,
    rows: list[dict],
    header: str | None = None,
    footer: str | None = None,
) -> dict:
    """Send one interactive LIST message. Returns a result dict, never raises.

    `rows` is [{"id": str, "title": str, "description": str|None}, ...] — at most
    ten, per Meta. Titles and descriptions are TRUNCATED rather than allowed to
    fail the send: a survey question that arrives with a clipped label is a small
    cosmetic problem, whereas a 400 from Meta means the customer receives nothing
    at all and the dispatcher burns a retry attempt on a fault no retry can fix.

    Same contract as send_text (`ok` / `skipped` / a reason), so the dispatcher
    and the survey service branch on results identically regardless of which
    kind of message went out.
    """
    if not is_configured():
        return {"ok": False, "skipped": True, "reason": "whatsapp_not_configured"}

    if not rows:
        # Guarded because Meta rejects an empty section and the caller would
        # otherwise learn about it as an opaque 400 mid-survey.
        return {"ok": False, "reason": "no_rows"}

    url = (
        f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}"
        f"/{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    section_rows = []
    for row in rows[:10]:
        entry = {
            "id": str(row["id"])[:200],
            "title": str(row["title"])[:_ROW_TITLE_MAX],
        }
        description = row.get("description")
        if description:
            entry["description"] = str(description)[:_ROW_DESC_MAX]
        section_rows.append(entry)

    interactive: dict = {
        "type": "list",
        "body": {"text": body},
        "action": {
            "button": button[:_ROW_TITLE_MAX],
            "sections": [{"rows": section_rows}],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _to_msisdn(to),
        "type": "interactive",
        "interactive": interactive,
    }
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a scheduler tick must not die here
        log.warning("whatsapp list send failed (transport) to %s: %s", _to_msisdn(to), exc)
        return {"ok": False, "reason": "transport_error", "detail": str(exc)}

    if resp.status_code // 100 == 2:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        message_id = None
        messages = data.get("messages") if isinstance(data, dict) else None
        if isinstance(messages, list) and messages:
            message_id = messages[0].get("id")
        return {"ok": True, "message_id": message_id}

    try:
        error = resp.json().get("error", {})
    except Exception:  # noqa: BLE001
        error = {"message": resp.text}
    log.warning(
        "whatsapp list send rejected (%s) to %s: %s", resp.status_code, _to_msisdn(to), error
    )
    return {"ok": False, "reason": "api_error", "status": resp.status_code, "error": error}


__all__ = ["is_configured", "send_text", "send_interactive_list"]
