"""One httpx client shape for every pooled Anthropic client in the process.

WHY THIS EXISTS (2026-09-03). Three places build an `anthropic.Anthropic`
client — Aisha's stream in agent/graph, the specialists' pooled client (which
reuses graph._build_client) and the router/judge in agent/router/classifier —
and every one of them inherited the SDK's httpx default of a 5-second idle
keep-alive. A customer takes longer than five seconds to type, so the first
model call of almost every turn re-did the TCP and TLS handshake: 150ms RTT
from UAE North plus the handshake, ~300ms measured with curl from the VM, and
the journal's first-call TTFT sat 263ms above the later calls in the same
turn. Longer keep-alive is safe because httpcore checks an idle socket for a
server-side close before handing it out again.

The pooled clients themselves stay where they are; this only decides what
they are built on, so the setting is read in one place.
"""
from __future__ import annotations

from app import config


def pooled_http_client():
    """An `anthropic.DefaultHttpxClient` with the configured idle keep-alive.

    Returns None when ANTHROPIC_KEEPALIVE_SECONDS is 0 (or negative), which
    hands the SDK its own default client — the exact pre-change behaviour.
    Imported lazily so scripted tests that never build a client never need
    the SDK at import time, same rule as graph._build_client.
    """
    seconds = int(config.ANTHROPIC_KEEPALIVE_SECONDS or 0)
    if seconds <= 0:
        return None
    import httpx
    from anthropic import DefaultHttpxClient

    return DefaultHttpxClient(
        limits=httpx.Limits(
            max_connections=1000,
            max_keepalive_connections=100,
            keepalive_expiry=float(seconds),
        ),
    )
