"""The WebRTC audio transport — offline where possible, loopback where not.

The frame maths, the outbound track's pacing/pause/clear levers and the
gateway's protocol handling are tested pure. The one end-to-end test drives a
REAL aiortc peer pair over loopback UDP inside this process — host candidates
on 127.0.0.1, no STUN, no internet, no vendor — because an SDP/ICE/DTLS
handshake is precisely the kind of thing a fake would prove nothing about.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct

import pytest

from app.services import rtc_transport as rtc

# The candidate summariser, bound once — it is a staticmethod on the transport
# but needs no instance, and the tests read better without the ceremony.
RtcTransportCandidates = rtc.RtcTransport._describe_candidates


@contextlib.contextmanager
def capture_log(name: str):
    """Collect one logger's records directly.

    `caplog` cannot see these: app/main.py sets `propagate = False` on the "app"
    logger (so uvicorn does not double-print), and pytest's capture hangs off
    the ROOT logger. Attaching here is immune to that — and to whether
    app.main has been imported yet, which otherwise makes it test-ORDER
    dependent and passes or fails by accident.
    """
    import logging

    lines: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    log_obj = logging.getLogger(name)
    handler = _Collect()
    log_obj.addHandler(handler)
    previous = log_obj.level
    log_obj.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        log_obj.removeHandler(handler)
        log_obj.setLevel(previous)


def test_upsample_doubles_and_preserves_shape():
    pcm = struct.pack("<4h", 0, 1000, -1000, 500)
    out = rtc.upsample_24k_to_48k(pcm)
    vals = struct.unpack("<8h", out)
    assert vals[0] == 0 and vals[2] == 1000 and vals[4] == -1000 and vals[6] == 500
    assert vals[1] == 500  # interpolated midpoints
    assert rtc.upsample_24k_to_48k(b"") == b""


def test_outbound_track_paces_pauses_and_clears():
    track = rtc._build_track()
    loud = struct.pack("<h", 8000) * 960  # 40 ms @24k -> 80 ms @48k

    async def scenario() -> None:
        track.play(loud)
        frame = await track.recv()
        assert frame.sample_rate == 48_000 and frame.samples == 960
        assert any(b != 0 for b in bytes(frame.planes[0]))

        # pause(): silence flows, the reply is HELD, the clock never stops.
        track.pause()
        held = track.queued_ms
        assert held > 0
        frame = await track.recv()
        assert bytes(frame.planes[0]) == b"\x00" * len(bytes(frame.planes[0]))
        assert track.queued_ms == held

        # resume(): the held reply plays on, nothing lost.
        track.resume()
        frame = await track.recv()
        assert any(b != 0 for b in bytes(frame.planes[0]))

        # clear(): the rest of the reply is dropped (committed-turn supersede).
        track.play(loud)
        track.clear()
        assert track.queued_ms == 0
        frame = await track.recv()
        assert bytes(frame.planes[0]) == b"\x00" * len(bytes(frame.planes[0]))

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_no_ice_config_offers_no_servers():
    """The localhost case: host candidates alone, exactly as before TURN
    existed. Empty config must never invent a server."""
    assert rtc.ice_server_specs() == []


def test_stun_alone_is_offered_without_credentials():
    assert rtc.ice_server_specs(stun_url="stun:stun.example.org:3478") == [
        {"urls": "stun:stun.example.org:3478"}
    ]


def test_turn_carries_its_credentials_and_follows_stun():
    """Order matters only in that both are offered; the credentials are the
    point — a relay without them cannot allocate."""
    specs = rtc.ice_server_specs(
        stun_url="stun:stun.example.org:3478",
        turn_url="turn:10.0.0.4:3478",
        turn_username="elite",
        turn_credential="s3cret",
    )
    assert specs == [
        {"urls": "stun:stun.example.org:3478"},
        {
            "urls": "turn:10.0.0.4:3478",
            "username": "elite",
            "credential": "s3cret",
        },
    ]


def test_turn_works_as_the_only_server():
    """The deployed shape: no STUN, relay only — the browser reaches our
    public relay candidate, which is all the call needs."""
    assert rtc.ice_server_specs(
        turn_url="turn:10.0.0.4:3478",
        turn_username="elite",
        turn_credential="s3cret",
    ) == [
        {
            "urls": "turn:10.0.0.4:3478",
            "username": "elite",
            "credential": "s3cret",
        }
    ]


@pytest.mark.parametrize(
    "username,credential",
    [("", "s3cret"), ("elite", ""), ("", "")],
)
def test_half_configured_turn_is_dropped_not_offered(username, credential):
    """coturn runs with --lt-cred-mech, so an uncredentialed relay refuses the
    allocation. Offering it anyway would burn the whole ICE timeout failing
    against a server that was never going to answer — strictly worse than
    falling straight back to WS-PCM. It is a deployment mistake, so it warns."""
    with capture_log("app.services.rtc_transport") as lines:
        specs = rtc.ice_server_specs(
            turn_url="turn:10.0.0.4:3478",
            turn_username=username,
            turn_credential=credential,
        )
    assert specs == []
    assert any("ignoring the relay" in line for line in lines)


def test_candidate_summary_names_types_and_redacts_addresses():
    """The candidate TYPE is what diagnoses a failed negotiation; the
    customer's full public IP is personal data and stays out of the log. An
    mDNS name is reported as such because it IS the failure mode — aiortc
    cannot resolve one, so a browser offering only those leaves our side with
    no usable address at all."""
    sdp = "\n".join([
        "a=candidate:1 1 udp 2130706431 192.168.1.77 5000 typ host",
        "a=candidate:2 1 udp 1694498815 203.0.113.44 5001 typ srflx raddr 192.168.1.77",
        "a=candidate:3 1 udp 16777215 20.216.16.132 49176 typ relay raddr 172.18.0.3",
        "a=candidate:4 1 udp 2130706431 abc-def.local 5002 typ host",
        "a=end-of-candidates",
    ])
    summary = RtcTransportCandidates(sdp)
    assert "host/udp/192.168.1.x" in summary
    assert "srflx/udp/203.0.113.x" in summary
    assert "relay/udp/20.216.16.x" in summary
    assert "host/udp/mdns" in summary
    # No full address survives.
    for full in ("192.168.1.77", "203.0.113.44", "20.216.16.132", "abc-def.local"):
        assert full not in summary


def test_candidate_summary_says_NONE_when_there_are_no_candidates():
    """An offer with nothing in it must read as an explicit NONE, not as an
    empty string that looks like a logging bug."""
    assert RtcTransportCandidates("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n") == "NONE"


def test_a_browser_that_rejects_our_answer_says_so(db, monkeypatch):
    """The client reports a failed setRemoteDescription instead of swallowing
    it. A browser that cannot apply the answer never starts ICE, so the server
    sees its own checks go out and nothing come back — from the outside,
    identical to a network dropping packets. Log-only: WS-PCM keeps the call."""
    import json
    from unittest.mock import MagicMock

    from app.api import call_realtime as rt_module
    from tests.test_call_gateway import SESSION

    call = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    call._send_json = sent.append  # type: ignore[method-assign]

    with capture_log("app.api.call_realtime") as lines:
        asyncio.run(call._on_text(json.dumps({
            "type": "rtc_error",
            "detail": "InvalidAccessError: Failed to set remote answer sdp",
        })))
    joined = "\n".join(lines)
    assert "rejected by the browser" in joined
    assert "Failed to set remote answer sdp" in joined
    # It is a REPORT, not a command: nothing is sent back and the call stands.
    assert sent == [] and call.ended is False


def test_gateway_refuses_rtc_gracefully(db, monkeypatch):
    """No aiortc / transport off -> rtc_unavailable, and the WS path carries
    the call exactly as before the upgrade existed."""
    import json
    from unittest.mock import MagicMock

    from app.api import call_realtime as rt_module
    from tests.test_call_gateway import SESSION

    call = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    call._send_json = sent.append  # type: ignore[method-assign]

    monkeypatch.setattr(rt_module, "make_rtc_transport", lambda **kw: None)
    asyncio.run(call._on_text(json.dumps({"type": "rtc_offer", "sdp": "v=0..."})))
    assert sent == [{"type": "rtc_unavailable"}]

    sent.clear()
    monkeypatch.setattr(rt_module, "CALL_TRANSPORT", "ws")
    asyncio.run(call._on_text(json.dumps({"type": "rtc_offer", "sdp": "v=0..."})))
    assert sent == [{"type": "rtc_unavailable"}]


def test_gateway_routes_audio_and_barge_levers_through_the_track(db):
    """rtc active: her audio goes to the track (never WS bytes); a speech
    HINT pauses playback; a COMMITTED turn clears it."""
    from unittest.mock import MagicMock

    from app.api import call_realtime as rt_module
    from tests.test_call_gateway import SESSION

    class FakeTrack:
        def __init__(self) -> None:
            self.ops: list[str] = []
            self.played = b""

        def play(self, pcm: bytes) -> None:
            self.played += pcm

        def pause(self) -> None:
            self.ops.append("pause")

        def resume(self) -> None:
            self.ops.append("resume")

        def clear(self) -> None:
            self.ops.append("clear")

    class FakeRtc:
        def __init__(self) -> None:
            self.track = FakeTrack()

    call = rt_module.RealtimeCallSession(MagicMock(), SESSION)
    sent: list[dict] = []
    call._send_json = sent.append  # type: ignore[method-assign]
    call.rtc = FakeRtc()
    call.rtc_active = True

    call._on_agent_event({"type": "audio", "data": b"\x01\x02" * 480})
    assert call.rtc.track.played == b"\x01\x02" * 480
    assert call.audio_open is False  # no pcm_start / WS bytes in rtc mode

    async def scenario() -> None:
        call._on_agent_event({"type": "user_speech_started"})
        assert call.rtc.track.ops[-1] == "pause"
        # A blip-commit (no real speech counted) RESUMES the held reply
        # rather than clearing it — nothing was superseded.
        call._on_agent_event({"type": "user_speech_stopped"})
        await asyncio.sleep(0)
        assert call.rtc.track.ops[-1] == "resume"
        assert "clear" not in call.rtc.track.ops
        # Real WORDS over her reply clear it (the transcript decides).
        call.response_active = True
        call._on_agent_event({"type": "user_speech_started"})
        call.utterance_bytes = 50_000
        call._on_agent_event({"type": "user_speech_stopped"})
        call._on_agent_event(
            {"type": "user_transcript", "text": "hold on, can I change the day"}
        )
        await asyncio.sleep(0)
        assert "clear" in call.rtc.track.ops

    asyncio.run(scenario())


def test_real_peer_pair_over_loopback(db):
    """A REAL negotiation: browser-side simulated by a second aiortc peer.
    Audio flows both ways over loopback UDP — mic frames reach on_pcm at
    24 kHz mono, and the server track's frames reach the far side."""
    pytest.importorskip("aiortc")
    from aiortc import RTCPeerConnection
    from aiortc.mediastreams import AudioStreamTrack

    received_pcm = bytearray()

    async def scenario() -> None:
        got_connected = asyncio.Event()
        far_frames: list = []

        async def on_pcm(pcm: bytes) -> None:
            received_pcm.extend(pcm)

        transport = rtc.RtcTransport(
            on_pcm=on_pcm, on_connected=got_connected.set, on_failed=lambda: None
        )

        client = RTCPeerConnection()
        client.addTrack(AudioStreamTrack())  # silent, but real, mic

        @client.on("track")
        def _on_track(track) -> None:
            async def pull() -> None:
                try:
                    while len(far_frames) < 5:
                        far_frames.append(await track.recv())
                except Exception:  # noqa: BLE001
                    pass

            asyncio.get_running_loop().create_task(pull())

        offer = await client.createOffer()
        await client.setLocalDescription(offer)
        answer_sdp = await transport.accept_offer(client.localDescription.sdp)
        from aiortc import RTCSessionDescription

        await client.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type="answer")
        )

        await asyncio.wait_for(got_connected.wait(), timeout=10)
        transport.track.play(struct.pack("<h", 5000) * 24_000)  # 1 s of tone

        for _ in range(100):  # up to ~10 s for media both ways
            if len(received_pcm) > 4800 and len(far_frames) >= 5:
                break
            await asyncio.sleep(0.1)

        await transport.close()
        await client.close()

        assert transport.connected is True
        assert len(received_pcm) > 4800  # mic audio landed as 24k PCM
        assert len(far_frames) >= 5  # her track reached the far side

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_turn_credentials_are_time_limited_and_verifiable(monkeypatch):
    """The TURN REST API scheme, which coturn validates independently: the
    username IS the expiry timestamp and the password is its HMAC-SHA1 under a
    secret that never leaves the server. Recomputed here exactly as coturn
    does — if this drifts, every allocation fails with 401 and the call
    silently falls back to WS-PCM."""
    import base64
    import hashlib
    import hmac
    import time

    from app import config

    monkeypatch.setattr(config, "RTC_TURN_SECRET", "top-secret")
    monkeypatch.setattr(config, "RTC_TURN_TTL_SECONDS", 600)

    username, credential = config.turn_credentials()
    expiry = int(username)
    assert time.time() < expiry <= time.time() + 600

    expected = base64.b64encode(
        hmac.new(b"top-secret", username.encode(), hashlib.sha1).digest()
    ).decode()
    assert credential == expected


def test_static_credentials_still_work_when_no_secret_is_set(monkeypatch):
    """An environment configured the old way must keep working — the fallback
    is what makes the switch safe to roll out."""
    from app import config

    monkeypatch.setattr(config, "RTC_TURN_SECRET", "")
    monkeypatch.setattr(config, "RTC_TURN_USERNAME", "elite")
    monkeypatch.setattr(config, "RTC_TURN_CREDENTIAL", "static-pw")
    assert config.turn_credentials() == ("elite", "static-pw")


def test_the_browser_gets_both_stun_and_a_relay(monkeypatch):
    """A client behind symmetric NAT cannot connect on a reflexive address —
    its external port differs per destination, so the address it advertises is
    not the one its packets arrive from. It must allocate its own relay."""
    from app import config

    monkeypatch.setattr(config, "RTC_CLIENT_STUN_URL", "stun:relay.example.org:3478")
    monkeypatch.setattr(config, "RTC_CLIENT_TURN_URL", "turn:relay.example.org:3478")
    monkeypatch.setattr(config, "RTC_TURN_SECRET", "top-secret")

    servers = config.client_ice_servers()
    assert [s["urls"] for s in servers] == [
        "stun:relay.example.org:3478",
        "turn:relay.example.org:3478",
    ]
    assert servers[0].keys() == {"urls"}  # STUN is unauthenticated
    assert servers[1]["username"] and servers[1]["credential"]
