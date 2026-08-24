"""WebRTC audio transport for the voice call (2026-07-30, developer decision).

WHY. The call's audio used to travel as raw PCM over the WebSocket and play
through WebAudio — a path the browser's echo canceller was never designed to
watch, which is why echo needed a loopback trick and a hand-tuned RMS gate,
and why her own voice kept re-entering the microphone as "customer speech".
Over WebRTC the browser treats Aisha as a proper remote call party — the same
stack as a Meet call — so AEC3 echo cancellation, noise suppression and jitter
handling engage NATIVELY on every device, and Opus cuts uplink+downlink
bandwidth ~10x (conference Wi-Fi). This module is the server's half: one
aiortc RTCPeerConnection per call.

WHAT IT IS NOT: a transport REPLACEMENT. Signaling (offer/answer), every JSON
event (state, utterance, token, tool_event, done, call_ended) and the whole
protocol stay on the existing WS — only the AUDIO moves. The WS-PCM path
remains intact as the fallback: a browser that cannot negotiate (old WebView,
UDP-hostile network) streams PCM exactly as before, and nothing downstream can
tell. The gateway (call_realtime) stays the single owner of what audio MEANS;
this module only moves bytes:

    mic track (Opus, 48k) ──decode/resample──► on_pcm(PCM16 mono 24k)
    play(PCM16 mono 24k)  ──upsample/pace───► outbound track (Opus)

BARGE-IN LEVERS. The outbound track can `pause()` (hold the remaining reply,
emit silence — the instant reaction to a speech-started HINT) and `clear()`
(drop it — the committed-turn supersede). Server-side playback is what makes
interruption honest here: the queue lives in this process, so stopping it
stops her within one 20 ms frame plus network latency.

OFFLINE-TESTABLE: frame conversion and the track's pacing/pause/clear are pure
and tested directly; ICE/DTLS handshakes are exercised only by live use, like
every transport handshake in this system.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("app.services.rtc_transport")

PCM_RATE = 24_000  # the vendor/gateway rate — this module's public dialect
_OUT_RATE = 48_000  # Opus native; aiortc encodes 48k without a second resample
_FRAME_SAMPLES = 960  # 20 ms @48k
_FRAME_BYTES = _FRAME_SAMPLES * 2


def upsample_24k_to_48k(pcm: bytes) -> bytes:
    """PCM16 mono 24k -> 48k by linear interpolation (numpy — already a dep
    via the speech gate)."""
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
    if len(x) == 0:
        return b""
    mid = (x[:-1] + x[1:]) // 2
    out = np.empty(len(x) * 2, dtype=np.int16)
    out[0::2] = x.astype(np.int16)
    out[1:-1:2] = mid.astype(np.int16)
    out[-1] = x[-1]
    return out.tobytes()


def _build_track():
    """The outbound audio track: a paced reader over a byte queue. Built
    lazily against aiortc's MediaStreamTrack so this module imports cleanly
    where aiortc is absent — the fallback path must never pay for the
    upgrade's dependency."""
    from aiortc.mediastreams import MediaStreamTrack
    from av import AudioFrame

    class _AishaTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._buf = bytearray()  # 48k mono s16
            self._pts = 0
            self._t0: float | None = None
            self._paused = False

        def play(self, pcm_24k: bytes) -> None:
            self._buf.extend(upsample_24k_to_48k(pcm_24k))

        def pause(self) -> None:
            """Hold the reply (emit silence, keep the queue) — the instant
            reaction to a speech-started hint that may yet be nothing."""
            self._paused = True

        def resume(self) -> None:
            self._paused = False

        def clear(self) -> None:
            """Drop the rest of the reply — the committed-turn supersede. The
            words are never taken back (text_break rule); only the unheard
            audio is."""
            self._buf.clear()
            self._paused = False

        @property
        def queued_ms(self) -> int:
            return len(self._buf) * 1000 // (_OUT_RATE * 2)

        async def recv(self):
            # Real-time pacing: one 20 ms frame per 20 ms of wall clock.
            if self._t0 is None:
                self._t0 = time.monotonic()
            due = self._t0 + self._pts / _OUT_RATE
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._paused or len(self._buf) < _FRAME_BYTES:
                chunk = b"\x00" * _FRAME_BYTES  # silence keeps the clock alive
            else:
                chunk = bytes(self._buf[:_FRAME_BYTES])
                del self._buf[:_FRAME_BYTES]
            frame = AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
            frame.planes[0].update(chunk)
            frame.sample_rate = _OUT_RATE
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, _OUT_RATE)
            self._pts += _FRAME_SAMPLES
            return frame

    return _AishaTrack()


def ice_server_specs(
    *,
    stun_url: str = "",
    turn_url: str = "",
    turn_username: str = "",
    turn_credential: str = "",
) -> list[dict[str, str]]:
    """The ICE servers this call offers, as PLAIN DICTS.

    Deliberately not `RTCIceServer` objects: aiortc is an optional-at-test-time
    import (the loopback test `importorskip`s it), and which servers we offer
    with which credentials is exactly the logic worth pinning in a test that
    ALWAYS runs. `accept_offer` does the one-line conversion.

    A TURN entry needs all three of url, username and credential. A partial one
    is DROPPED rather than offered, because coturn runs with `--lt-cred-mech`
    and would refuse the allocation — leaving the call to spend its whole ICE
    timeout failing against a relay that was never going to answer, which is
    strictly worse than never offering it. Half-configured is a deployment
    mistake, so it is logged loudly and then treated as "no TURN".
    """
    specs: list[dict[str, str]] = []
    if stun_url:
        specs.append({"urls": stun_url})
    if turn_url:
        if turn_username and turn_credential:
            specs.append(
                {
                    "urls": turn_url,
                    "username": turn_username,
                    "credential": turn_credential,
                }
            )
        else:
            log.warning(
                "RTC_TURN_URL is set but username/credential are not — "
                "ignoring the relay (calls fall back to WS-PCM)"
            )
    return specs


class RtcTransport:
    """One call's peer connection. `accept_offer` completes the whole
    negotiation (non-trickle: the answer carries every ICE candidate)."""

    def __init__(
        self,
        *,
        on_pcm: Callable[[bytes], Awaitable[None]],
        on_connected: Callable[[], None] | None = None,
        on_failed: Callable[[], None] | None = None,
        stun_url: str = "",
        turn_url: str = "",
        turn_username: str = "",
        turn_credential: str = "",
    ) -> None:
        self._on_pcm = on_pcm
        self._on_connected = on_connected
        self._on_failed = on_failed
        self._stun_url = stun_url
        self._turn_url = turn_url
        self._turn_username = turn_username
        self._turn_credential = turn_credential
        self._pc = None
        self.track = None  # AishaTrack once negotiated
        self._pump: asyncio.Task | None = None
        self.connected = False

    @staticmethod
    def _describe_candidates(sdp: str) -> str:
        """A one-line summary of an SDP's ICE candidates, for the log.

        WHY IT EXISTS: three rounds of this bug were diagnosed by inference
        because nothing recorded what either side actually offered. A call that
        never reaches `connected` is otherwise indistinguishable whether the
        browser sent nothing usable, sent mDNS names, or sent good candidates
        we failed to pair with.

        ADDRESSES ARE REDACTED TO THEIR LAST OCTET. The candidate type is what
        diagnoses this (host vs srflx vs relay); the customer's full public IP
        is personal data and has no business in a log we keep. mDNS `.local`
        names are reported AS `mdns` — they are the failure mode itself, since
        aiortc cannot resolve them.
        """
        out: list[str] = []
        for line in sdp.splitlines():
            line = line.strip()
            if "candidate:" not in line:
                continue
            parts = line.split()
            if len(parts) < 8:
                continue
            addr, typ = parts[4], parts[7]
            if addr.endswith(".local"):
                addr = "mdns"
            elif addr.count(".") == 3:
                addr = addr.rsplit(".", 1)[0] + ".x"
            out.append(f"{typ}/{parts[2]}/{addr}")
        return ", ".join(out) if out else "NONE"

    async def accept_offer(self, sdp: str) -> str:
        from aiortc import (
            RTCConfiguration,
            RTCIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )

        specs = ice_server_specs(
            stun_url=self._stun_url,
            turn_url=self._turn_url,
            turn_username=self._turn_username,
            turn_credential=self._turn_credential,
        )
        servers = [RTCIceServer(**spec) for spec in specs]
        log.info(
            "rtc offer candidates (browser): %s | our ice servers: %s",
            self._describe_candidates(sdp),
            ", ".join(s["urls"] for s in specs) or "NONE",
        )
        self._pc = pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        self.track = _build_track()
        pc.addTrack(self.track)

        @pc.on("track")
        def _on_track(track) -> None:
            if track.kind == "audio" and self._pump is None:
                self._pump = asyncio.get_running_loop().create_task(
                    self._pump_inbound(track)
                )

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            state = pc.connectionState
            log.info("rtc connection state: %s", state)
            if state == "connected" and not self.connected:
                self.connected = True
                if self._on_connected is not None:
                    self._on_connected()
            elif state in ("failed", "closed") and self._on_failed is not None:
                self._on_failed()

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        answer = await pc.createAnswer()
        # aiortc gathers ICE inside setLocalDescription — the returned SDP is
        # complete, so no trickle protocol is needed over the WS.
        await pc.setLocalDescription(answer)
        log.info(
            "rtc answer candidates (ours): %s",
            self._describe_candidates(pc.localDescription.sdp),
        )
        return pc.localDescription.sdp

    async def _pump_inbound(self, track) -> None:
        from aiortc.mediastreams import MediaStreamError
        from av import AudioResampler

        resampler = AudioResampler(format="s16", layout="mono", rate=PCM_RATE)
        try:
            while True:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    data = bytes(out.planes[0])[: out.samples * 2]
                    if data:
                        await self._on_pcm(data)
        except MediaStreamError:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a dead pump ends rtc, not the call
            log.warning("rtc inbound pump died: %s", exc)
            if self._on_failed is not None:
                self._on_failed()

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        pc, self._pc = self._pc, None
        if pc is not None:
            try:
                await pc.close()
            except Exception:  # noqa: BLE001
                pass


def make_rtc_transport(**kwargs) -> RtcTransport | None:
    """The gateway's seam. None = aiortc unavailable — the WS-PCM fallback
    carries the call, exactly as before this module existed."""
    try:
        import aiortc  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        log.warning("webrtc unavailable (%s) — ws audio fallback", exc)
        return None
    return RtcTransport(**kwargs)
