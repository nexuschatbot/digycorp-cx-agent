// The voice call (voice phase — see the CLAUDE.md scope override and AD-C
// amendment). A hands-free loop around the SAME conversation: mic audio up a
// WebSocket, the server transcribes and runs the ordinary agent turn, and the
// reply comes back as text events (rendered into the thread by useChat) plus
// synthesised audio played here.
//
// THIS HOOK OWNS TRANSPORT AND AUDIO, NOTHING ELSE. What the turns MEAN — the
// bubbles, cards, receipts, close — belongs to useChat, which renders call
// turns through the same machinery as typed ones (createAgentTurn). The split
// mirrors useVoice: capture primitives here, conversation there.
//
// CAPTURE IS WEB AUDIO, NOT MediaRecorder. A call needs raw PCM frames it can
// stream continuously and meter for silence; MediaRecorder produces opaque
// containers on its own schedule and cannot do either. Everything is
// downsampled to 16 kHz PCM16 mono on this side, so the server never guesses.
//
// TURN-TAKING v0 IS CLIENT-SIDE SILENCE DETECTION (RMS): after speech is
// heard, ~1 s below the threshold ends the turn. P3 moves this server-side
// (vendor VAD); the client detector then remains as the fallback.
//
// ECHO DISCIPLINE: nothing is sent while Aisha's reply is playing. v0 has no
// barge-in (P4), and streaming her own voice back into the transcriber would
// be worse than a missed interruption.
//
// HTTPS: getUserMedia is blocked on plain HTTP except localhost — same blocker
// as voice notes, surfaced the same way (`supported` false, stated reason).

import { useCallback, useEffect, useRef, useState } from 'react'

export type CallState =
  | 'idle'
  | 'connecting' // ws + mic permission both in flight
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'ended'

export interface CallUtterance {
  audioId: string | null
  text: string
  lang: 'en' | 'ar' | null
  durationMs: number | null
  failed: boolean
}

/** A run_turn event forwarded verbatim off the wire (token, tool_event,
 *  booking, text_break, closed, done, error) — payload shapes exactly as the
 *  SSE contract, plus the discriminating `type`. */
export type CallStreamEvent = { type: string } & Record<string, unknown>

export interface UseCallOptions {
  /** OPTIONAL since the overlay decision (2026-07-27): the call renders no
   *  live thread, so most consumers want only onCallEnd. The events still
   *  fire for any future caption UI. */
  onUtterance?: (u: CallUtterance) => void
  onStreamEvent?: (e: CallStreamEvent) => void
  /** The call is over (any reason, including refusals) — refresh the thread
   *  and re-arm the idle ladder. */
  onCallEnd: (reason: string) => void
}

export interface UseCall {
  state: CallState
  /** True from start() until the call ends — the composer swaps to the call
   *  bar and the idle ladder treats the customer as present. */
  active: boolean
  muted: boolean
  elapsedMs: number
  /** Why the last call ended (server's call_ended reason, or a local one:
   *  mic_denied | connection_failed | connection_lost). Null until then. */
  endReason: string | null
  supported: boolean
  /** Smoothed mic loudness (0..1), read live by the overlay's ring animation.
   *  A getter over a ref — it never triggers a re-render. */
  getLevel: () => number
  start: () => void
  hangup: () => void
  toggleMute: () => void
}

// The wire format the gateway expects — 24 kHz because that is the ONLY rate
// the vendor's realtime transcription accepts (P3), and one rate end-to-end
// beats resampling twice. The server still resamples defensively if a client
// ever negotiates something else.
const TARGET_SAMPLE_RATE = 24_000
// ~256 ms of 16 kHz PCM16 per WS frame: small enough to feel live, large
// enough not to spray hundreds of frames a second.
const SEND_CHUNK_SAMPLES = 4_096
// v0 silence detection. RMS above SPEECH_RMS arms the turn; QUIET_MS below
// QUIET_RMS after that ends it. Mirrors the server's CALL_VAD_SILENCE_MS
// (900 ms) closely enough to hand over cleanly in P3.
const SPEECH_RMS = 0.02
const QUIET_RMS = 0.012
const QUIET_MS = 1_000
// P4 barge-in: sustained speech at a HIGHER bar than turn-taking, because the
// phone's own speaker is playing Aisha at the same moment. Browser echo
// cancellation removes most of her from the mic, but "most" is device-luck —
// the margin (higher threshold + a quarter second sustained) is what stands
// between interruption and Aisha cutting HERSELF off. ⚠️ Tune on real
// hardware; a laptop's AEC and a phone's differ audibly.
const BARGE_RMS = 0.03
const BARGE_MS = 180
// Echo self-defence (2026-07-27, after a live call where Aisha barged in on
// HERSELF): her own voice through the speakers leaks into the mic at levels
// device AEC does not always kill. Two guards: the detector only ARMS after
// the reply has been playing a moment (onset transients are the loudest echo),
// and speech must stand clearly ABOVE the echo's rolling level, not just above
// a fixed bar — the customer's voice spikes over their own speaker bleed.
const BARGE_ARM_DELAY_MS = 300
// 1.4x, walked down from 3x -> 2x -> here across live calls (2026-07-28): the
// echo EMA is the guard now, so the margin above it can be thin — a voice at
// the mic beats speaker bleed by well over 40%, while 2x still missed real
// interruptions. If she ever self-interrupts again, step this back UP first.
const BARGE_OVER_ECHO = 1.4
// The uplink noise gate (realtime mode, UNROUTED FALLBACK ONLY — see the echo
// route below): below this, the room is "silence" and is SENT AS SILENCE —
// zeroed samples, stream unbroken — so the vendor's VAD hears a clean
// end-of-turn instead of background hiss that keeps the turn open forever
// ("she keeps listening instead of answering", live 2026-07-28). With the
// echo route up, noise handling belongs to the browser's noiseSuppression and
// the vendor's near_field noise_reduction — real DSP, not an RMS floor.
const NOISE_GATE_RMS = 0.012
const GATE_HANGOVER_MS = 400
// Natural speech dips between syllables — at 128-frame granularity (~3 ms) a
// "sustained" condition that resets on ANY quiet block can never be met (the
// bug that made barge-in unreachable on first live test). Dips shorter than
// the hangover keep the barge clock running.
const BARGE_HANGOVER_MS = 150

function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof AudioContext !== 'undefined' &&
    typeof AudioWorkletNode !== 'undefined' &&
    // Undefined on an insecure origin — the HTTP blocker, same as useVoice.
    !!navigator.mediaDevices?.getUserMedia
  )
}

// The capture worklet, inlined as a Blob so no build/config change is needed.
// It forwards each 128-frame block with its RMS; resampling stays on the main
// thread where the code is debuggable.
const WORKLET_SOURCE = `
class CallCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch && ch.length) {
      let sum = 0
      for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i]
      this.port.postMessage(
        { samples: ch.slice(0), rms: Math.sqrt(sum / ch.length) },
        [],
      )
    }
    return true
  }
}
registerProcessor('call-capture', CallCaptureProcessor)
`

/** Linear resample to 16 kHz then PCM16. Fidelity is ample for speech STT and
 *  the point is predictability: the server always receives one format. */
function toPcm16k(input: Float32Array, inputRate: number): Int16Array {
  const ratio = inputRate / TARGET_SAMPLE_RATE
  const outLength = Math.floor(input.length / ratio)
  const out = new Int16Array(outLength)
  for (let i = 0; i < outLength; i++) {
    const pos = i * ratio
    const i0 = Math.floor(pos)
    const i1 = Math.min(i0 + 1, input.length - 1)
    const frac = pos - i0
    const sample = input[i0] * (1 - frac) + input[i1] * frac
    const clamped = Math.max(-1, Math.min(1, sample))
    out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff
  }
  return out
}

// --------------------------------------------------------------- echo route
// THE ROOT FIX for every echo problem this file ever fought (2026-07-29).
// Chrome's echo canceller (AEC3 — the Google Meet one) subtracts KNOWN
// PLAYBACK from the mic, but its reference signal is WebRTC playout only:
// audio scheduled straight onto an AudioContext is invisible to it, so the
// mic heard Aisha and we compensated with thresholds (noise gate, echo EMA,
// barge bars) that could never be right for every room. The fix is the
// established loopback trick: Aisha's voice goes AudioContext ->
// MediaStreamDestination -> a local RTCPeerConnection pair -> an <audio>
// element playing the "remote" stream. To the AEC that is a phone call's
// far end — it erases her from the mic at DSP level, on any device, no
// tuning. The pair negotiates entirely on-device (host candidates, no
// network), typically in well under a second.
interface EchoRoute {
  node: MediaStreamAudioDestinationNode
  close: () => void
}

async function openEchoRoute(ctx: AudioContext): Promise<EchoRoute | null> {
  if (typeof RTCPeerConnection === 'undefined') return null
  const node = ctx.createMediaStreamDestination()
  const pc1 = new RTCPeerConnection()
  const pc2 = new RTCPeerConnection()
  const el = new Audio()
  el.autoplay = true
  const close = () => {
    el.pause()
    el.srcObject = null
    pc1.close()
    pc2.close()
  }
  try {
    pc1.onicecandidate = (e) => {
      if (e.candidate) void pc2.addIceCandidate(e.candidate).catch(() => undefined)
    }
    pc2.onicecandidate = (e) => {
      if (e.candidate) void pc1.addIceCandidate(e.candidate).catch(() => undefined)
    }
    const playing = new Promise<void>((resolve, reject) => {
      pc2.ontrack = (e) => {
        el.srcObject = e.streams[0] ?? new MediaStream([e.track])
        // Autoplay is permitted here: the call button was a user gesture and
        // the mic capture is live. A refusal rejects -> unrouted fallback.
        el.play().then(resolve, reject)
      }
    })
    for (const track of node.stream.getTracks()) pc1.addTrack(track, node.stream)
    const offer = await pc1.createOffer()
    await pc1.setLocalDescription(offer)
    await pc2.setRemoteDescription(offer)
    const answer = await pc2.createAnswer()
    await pc2.setLocalDescription(answer)
    await pc1.setRemoteDescription(answer)
    // A hung local negotiation must not stall the call: fall back, unrouted.
    await Promise.race([
      playing,
      new Promise((_, reject) => setTimeout(() => reject(new Error('echo route timeout')), 2000)),
    ])
    return { node, close }
  } catch {
    close()
    return null
  }
}

function wsUrl(sessionId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/api/call/${sessionId}`
}

// `getSessionId` is a GETTER, not a string (2026-07-29): the id is resolved at
// the moment the socket dials, so a call can never connect to a session that
// was rotated away in the same tick ("New chat" must always give the next call
// a fresh session — the customer's requirement, made structural).
export function useCall(getSessionId: () => string, opts: UseCallOptions): UseCall {
  const supported = isSupported()
  const [state, setState] = useState<CallState>('idle')
  const [active, setActive] = useState(false)
  const [muted, setMuted] = useState(false)
  const [elapsedMs, setElapsedMs] = useState(0)
  const [endReason, setEndReason] = useState<string | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const nodeRef = useRef<AudioWorkletNode | null>(null)
  const workletUrlRef = useRef<string | null>(null)
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const startedAtRef = useRef(0)

  // Everything per-call and read from audio callbacks lives in refs — the
  // worklet message handler fires ~380×/s and must never see stale state.
  const stateRef = useRef<CallState>('idle')
  const mutedRef = useRef(false)
  // Smoothed mic loudness (0..1) for the overlay's LIVE ring — a ref, not
  // state, because the capture worklet posts ~380×/s and React must never
  // re-render at that cadence. The overlay reads it in a requestAnimationFrame
  // loop (see App's CallPulse). Fast attack, gentle decay for a VU-meter feel;
  // a muted track posts near-silence, so it falls back to 0 on its own.
  const levelRef = useRef(0)
  const pendingRef = useRef<Int16Array[]>([]) // resampled, not yet sent
  const speechSeenRef = useRef(false)
  const quietSinceRef = useRef<number | null>(null)
  const playChunksRef = useRef<BlobPart[]>([]) // the segment currently arriving
  const playMimeRef = useRef('audio/mpeg')
  const playerRef = useRef<HTMLAudioElement | null>(null)
  const playUrlRef = useRef<string | null>(null)
  // P4: one segment PER SENTENCE now — a queue, played gaplessly in order,
  // filling while earlier segments still play (the server synthesises ahead).
  const playQueueRef = useRef<Blob[]>([])
  const playingRef = useRef(false)
  // The server flips to `listening` the moment the LAST segment is sent —
  // seconds before the customer stops hearing it. Park that transition here
  // and apply it when the queue actually drains.
  const pendingListenRef = useRef(false)
  const playedThisTurnRef = useRef(false)
  const bargeSinceRef = useRef<number | null>(null)
  const bargeLastLoudRef = useRef(0)
  const bargeArmAtRef = useRef(0)
  const echoLevelRef = useRef(0)
  const gateOpenUntilRef = useRef(0)
  // Streaming playback (2026-07-27): when MediaSource can take raw mpeg, the
  // HEAD-OF-QUEUE segment plays AS ITS BYTES ARRIVE off the wire instead of
  // waiting for audio_end — first sound within a beat of the vendor starting
  // to synthesise. Backlogged segments still accumulate to blobs: their bytes
  // land long before their turn to play, so streaming buys them nothing.
  const mseRef = useRef<{
    ms: MediaSource
    sb: SourceBuffer | null
    el: HTMLAudioElement
    url: string
    pending: ArrayBuffer[]
    ended: boolean
  } | null>(null)
  // The realtime engine (speech-to-speech) speaks raw PCM in a rolling stream
  // (pcm_start / frames / pcm_end) — scheduled gaplessly on the AudioContext
  // clock rather than played as segments.
  const pcmActiveRef = useRef(false)
  const pcmRateRef = useRef(24_000)
  const pcmNextTimeRef = useRef(0)
  const pcmSourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set())
  const pcmDrainTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The reply's text+lang, accumulated for the on-device fallback: a TTS
  // outage costs the VOICE QUALITY, never the call (the read-aloud rule).
  const replyTextRef = useRef('')
  const replyLangRef = useRef<'en' | 'ar'>('en')
  // P3: true while the SERVER's VAD owns turn-taking (the `vad` event says).
  // The local silence detector then stands down — two detectors racing would
  // end turns twice. Flips back to false if the relay dies mid-call.
  const serverVadRef = useRef(false)
  // "pipeline" gates the mic on the local state machine; "realtime" streams it
  // CONTINUOUSLY — the vendor's VAD owns turn-taking AND barge-in, and a
  // client-side gate can only ever add deafness bugs (it did, live,
  // 2026-07-28: a missed state flip left the mic gated shut mid-call).
  const engineRef = useRef<'pipeline' | 'realtime'>('pipeline')
  // Dial-until-ready (2026-08-03): true once the call is actually picked up —
  // call_ready in realtime mode, call_started in pipeline mode. Until then
  // the ring plays and server state events are ignored (the server stamps an
  // initial "listening" before the greeting has settled the transport).
  const callReadyRef = useRef(false)
  // The AEC loopback (openEchoRoute). Non-null = Aisha's voice is invisible
  // to the mic, so the realtime uplink streams unconditionally and every
  // client-side echo heuristic stands down. Null = the old gated fallback.
  const echoRouteRef = useRef<EchoRoute | null>(null)
  // The WebRTC audio upgrade (2026-07-30). When the server invites it
  // (call_started.rtc) the mic track and Aisha's voice move to a peer
  // connection: echo cancellation becomes the browser's NATIVE AEC3 (Aisha is
  // a real remote party, like a Meet call), Opus replaces raw PCM, and every
  // loopback/gating heuristic above becomes the FALLBACK it always deserved
  // to be. Until rtc_active arrives — and again if rtc_failed ever does — the
  // WS-PCM path carries the call, so the upgrade can never cost it.
  const rtcRef = useRef<RTCPeerConnection | null>(null)
  // ICE servers, SENT BY THE SERVER in call_started (2026-08-04). Without them
  // the browser offers only host candidates — which Chrome publishes as mDNS
  // `.local` names that aiortc cannot resolve — so the server has no usable
  // address for us and the negotiation fails no matter how good ITS candidate
  // is (measured: a proven-working public relay candidate on the server side
  // still never reached `connected`). A STUN server here is what makes the
  // browser discover and offer its own PUBLIC address.
  //
  // Server-supplied rather than a constant so the relay can move without a
  // frontend rebuild, and so localhost gets [] — where host candidates already
  // work and dialling an absent STUN host would only add latency.
  const iceServersRef = useRef<RTCIceServer[]>([])
  const rtcActiveRef = useRef(false)
  const rtcAudioElRef = useRef<HTMLAudioElement | null>(null)
  const ringTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // Call generation. endCall defers its teardown ~350 ms so the hangup cue can
  // be heard — but a NEW call started inside that window must not have its
  // fresh resources torn down by the old call's timer. start() bumps the
  // generation; the deferred teardown fires only if it still owns it.
  const generationRef = useRef(0)
  const endedRef = useRef(false)

  const optsRef = useRef(opts)
  optsRef.current = opts

  /** Local oscillator cues — the call's body language (ack, ring, pickup,
   *  hangup). Generated on-device so they cost nothing and work when every
   *  vendor is down; routed through the echo route when it exists, so the
   *  mic never hears our own chrome. */
  const playTones = useCallback(
    (notes: ReadonlyArray<readonly [number, number]>, gain = 0.06, dur = 0.12) => {
      const ctx = ctxRef.current
      if (!ctx || ctx.state !== 'running') return
      try {
        const out = echoRouteRef.current?.node ?? ctx.destination
        const now = ctx.currentTime
        for (const [freq, at] of notes) {
          const osc = ctx.createOscillator()
          const g = ctx.createGain()
          osc.type = 'sine'
          osc.frequency.value = freq
          g.gain.setValueAtTime(0, now + at)
          g.gain.linearRampToValueAtTime(gain, now + at + 0.02)
          g.gain.exponentialRampToValueAtTime(0.001, now + at + dur)
          osc.connect(g).connect(out)
          osc.start(now + at)
          osc.stop(now + at + dur + 0.02)
        }
      } catch {
        // A missing cue is nothing; a crashed call over a beep would be absurd.
      }
    },
    [],
  )

  /** A gentle telephone ring while the call is being set up — answering a
   *  phone, not waiting for a page to load. Repeats until call_started. */
  const playRingBurst = useCallback(
    () => playTones([[400, 0], [450, 0], [400, 0.6], [450, 0.6]], 0.035, 0.4),
    [playTones],
  )

  const stopRing = useCallback(() => {
    if (ringTimerRef.current) clearInterval(ringTimerRef.current)
    ringTimerRef.current = null
  }, [])

  const setCallState = useCallback((s: CallState) => {
    stateRef.current = s
    setState(s)
    if (s !== 'speaking') {
      // Fresh echo baseline per reply — yesterday's level is not today's.
      bargeArmAtRef.current = 0
      echoLevelRef.current = 0
      bargeSinceRef.current = null
    }
    // No turn cue (2026-08-03, product decision): the state change makes no
    // sound — the ring, pickup and hangup cues are call chrome and stay.
  }, [])

  const stopPlayback = useCallback(() => {
    stopPcm()
    const seg = mseRef.current
    if (seg) {
      mseRef.current = null
      seg.el.onended = null
      seg.el.onerror = null
      seg.el.pause()
      try {
        if (seg.ms.readyState === 'open') seg.ms.endOfStream()
      } catch {
        // already closed
      }
      URL.revokeObjectURL(seg.url)
    }
    const el = playerRef.current
    if (el) {
      el.onended = null
      el.onerror = null
      el.pause()
    }
    playerRef.current = null
    playingRef.current = false
    if (playUrlRef.current) {
      URL.revokeObjectURL(playUrlRef.current)
      playUrlRef.current = null
    }
    playChunksRef.current = []
    playQueueRef.current = []
    window.speechSynthesis?.cancel()
  }, [])

  const teardown = useCallback(() => {
    stopPlayback()
    stopRing()
    try {
      rtcRef.current?.close()
    } catch {
      // already closed
    }
    rtcRef.current = null
    rtcActiveRef.current = false
    if (rtcAudioElRef.current) {
      rtcAudioElRef.current.pause()
      rtcAudioElRef.current.srcObject = null
      rtcAudioElRef.current = null
    }
    echoRouteRef.current?.close()
    echoRouteRef.current = null
    if (tickRef.current) clearInterval(tickRef.current)
    tickRef.current = null
    nodeRef.current?.port.close()
    nodeRef.current?.disconnect()
    nodeRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    void ctxRef.current?.close().catch(() => undefined)
    ctxRef.current = null
    if (workletUrlRef.current) {
      URL.revokeObjectURL(workletUrlRef.current)
      workletUrlRef.current = null
    }
    const ws = wsRef.current
    wsRef.current = null
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close()
    pendingRef.current = []
    speechSeenRef.current = false
    quietSinceRef.current = null
  }, [stopPlayback, stopRing])

  const endCall = useCallback(
    (reason: string) => {
      if (endedRef.current) return
      endedRef.current = true
      // Hang up like a phone: silence her, click, then tear down — the 350 ms
      // grace exists only so the cue can be heard before its context closes.
      // Generation-guarded: a call restarted inside the grace keeps its
      // resources (start() bumps the generation and does its own teardown).
      stopPlayback()
      stopRing()
      playTones([[660, 0], [440, 0.12]], 0.05)
      const generation = generationRef.current
      window.setTimeout(() => {
        if (generationRef.current === generation) teardown()
      }, 350)
      setEndReason(reason)
      setCallState('ended')
      setActive(false)
      setMuted(false)
      mutedRef.current = false
      optsRef.current.onCallEnd(reason)
    },
    [playTones, setCallState, stopPlayback, stopRing, teardown],
  )

  // ------------------------------------------------------------------ uplink
  const flushPending = useCallback(() => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    const chunks = pendingRef.current
    pendingRef.current = []
    for (const chunk of chunks) {
      ws.send(chunk.buffer as ArrayBuffer)
    }
  }, [])

  const sendJson = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload))
  }, [])

  const endTurn = useCallback(() => {
    flushPending()
    sendJson({ type: 'end_turn' })
    speechSeenRef.current = false
    quietSinceRef.current = null
    // Optimistic: the server confirms with its own state event; showing
    // thinking now keeps the UI honest about the mic being closed.
    setCallState('thinking')
  }, [flushPending, sendJson, setCallState])

  // P4: the customer talks over Aisha. The CLIENT detects it — it owns
  // playback — kills the audio instantly (~a frame), and tells the server,
  // which abandons the reply's remaining speech and reopens the mic. The
  // TEXT is never taken back; interruption is playback-level.
  const doBargeIn = useCallback(() => {
    bargeSinceRef.current = null
    pendingListenRef.current = false
    stopPlayback()
    sendJson({ type: 'barge_in' })
    // They are mid-word right now: arm the fallback turn detector as if
    // speech was already heard, so their interruption can end normally.
    speechSeenRef.current = true
    quietSinceRef.current = null
    setCallState('listening')
  }, [sendJson, setCallState, stopPlayback])

  const onAudioBlock = useCallback(
    (samples: Float32Array, rms: number, inputRate: number) => {
      // Live-ring loudness, updated every block regardless of engine/state so
      // the overlay ring reacts to the caller's voice the instant they speak.
      // 0.18 ≈ a firm speaking level → full ring; the decay term lets it fall.
      levelRef.current = Math.max(Math.min(1, rms / 0.18), levelRef.current * 0.86)
      // REALTIME ENGINE: stream everything, always (except mute). The vendor
      // hears continuously and decides who is talking; every local gate below
      // is pipeline machinery.
      if (engineRef.current === 'realtime') {
        if (mutedRef.current || endedRef.current) return
        // WebRTC active: the mic travels on the peer connection — the worklet
        // capture stands down entirely, and with it every echo heuristic
        // below (the browser's AEC3 owns echo now).
        if (rtcActiveRef.current) return
        // ECHO-ROUTED (the normal case): Aisha's voice goes through the AEC
        // loopback, so the mic stream is MOSTLY clean of her — but "mostly"
        // proved not enough on live hardware (2026-07-29: five consecutive
        // replies cancelled `turn_detected` — her residual echo read as the
        // customer interrupting, over and over, and every cancelled reply
        // bills its full input). So: stream EVERYTHING while she is silent,
        // and while her audio is PHYSICALLY playing forward only sound that
        // clearly beats the echo's own rolling level — everything under the
        // bar is sent as SILENCE (zeros, stream unbroken), so the vendor's
        // VAD hears a quiet room instead of her ghost, while a real
        // interruption punches through and native barge-in still works.
        if (echoRouteRef.current) {
          const converted = toPcm16k(samples, inputRate)
          const routedCtx = ctxRef.current
          const playingNow =
            playingRef.current ||
            pcmActiveRef.current ||
            (routedCtx != null && pcmNextTimeRef.current > routedCtx.currentTime + 0.05)
          if (playingNow) {
            const now = Date.now()
            echoLevelRef.current =
              echoLevelRef.current === 0 ? rms : echoLevelRef.current * 0.95 + rms * 0.05
            const bar = Math.max(BARGE_RMS, echoLevelRef.current * BARGE_OVER_ECHO)
            // Hangover keeps the whole interrupting utterance flowing once it
            // has cleared the bar — syllable dips must not chop it back down.
            if (rms >= bar) {
              gateOpenUntilRef.current = now + GATE_HANGOVER_MS
              // LOCAL BARGE, kept even with the server's stop_audio (which
              // covers it too, 2026-07-29): the client owns playback, so
              // killing it here lands within a frame instead of a round-trip
              // — sustained voice over the bar IS an interruption.
              bargeLastLoudRef.current = now
              if (bargeSinceRef.current == null) bargeSinceRef.current = now
              else if (now - bargeSinceRef.current >= BARGE_MS) doBargeIn()
            } else {
              if (
                bargeSinceRef.current != null &&
                now - bargeLastLoudRef.current > BARGE_HANGOVER_MS
              ) {
                bargeSinceRef.current = null
              }
              if (now > gateOpenUntilRef.current) converted.fill(0)
            }
          } else {
            echoLevelRef.current = 0 // fresh baseline per reply
            bargeSinceRef.current = null
          }
          pendingRef.current.push(converted)
          const buffered = pendingRef.current.reduce((n, c) => n + c.length, 0)
          if (buffered >= SEND_CHUNK_SAMPLES) flushPending()
          return
        }
        // UNROUTED FALLBACK (no RTCPeerConnection / autoplay refusal): the
        // pre-route behaviour, kept whole — gate, echo EMA, detector barge.
        // Stream ALWAYS — except while her reply is AUDIBLY PLAYING here.
        // Speakers feed the mic, and an open uplink then makes the vendor
        // hear — and TRANSCRIBE — Aisha as the customer (measured live
        // 2026-07-28: her showroom list landed in the thread as a customer
        // bubble), and its VAD cancels her for talking over herself. The gate
        // is the PHYSICAL playback — player objects and the PCM schedule —
        // not a state machine, so it cannot jam shut the way the state gate
        // did: no players, no schedule, the mic streams. Interruption during
        // playback stays with the echo-guarded detector below.
        const ctx = ctxRef.current
        const playingNow =
          playingRef.current ||
          pcmActiveRef.current ||
          (ctx != null && pcmNextTimeRef.current > ctx.currentTime + 0.05)
        if (!playingNow) {
          const now = Date.now()
          if (rms >= NOISE_GATE_RMS) gateOpenUntilRef.current = now + GATE_HANGOVER_MS
          const converted = toPcm16k(samples, inputRate)
          pendingRef.current.push(
            now <= gateOpenUntilRef.current ? converted : new Int16Array(converted.length),
          )
          const buffered = pendingRef.current.reduce((n, c) => n + c.length, 0)
          if (buffered >= SEND_CHUNK_SAMPLES) flushPending()
          return
        }
        const now = Date.now()
        echoLevelRef.current =
          echoLevelRef.current === 0 ? rms : echoLevelRef.current * 0.95 + rms * 0.05
        if (bargeArmAtRef.current === 0) bargeArmAtRef.current = now + BARGE_ARM_DELAY_MS
        if (now >= bargeArmAtRef.current) {
          const bar = Math.max(BARGE_RMS, echoLevelRef.current * BARGE_OVER_ECHO)
          if (rms >= bar) {
            bargeLastLoudRef.current = now
            if (bargeSinceRef.current == null) bargeSinceRef.current = now
            else if (now - bargeSinceRef.current >= BARGE_MS) doBargeIn()
          } else if (
            bargeSinceRef.current != null &&
            now - bargeLastLoudRef.current > BARGE_HANGOVER_MS
          ) {
            bargeSinceRef.current = null
          }
        }
        return
      }
      // While Aisha speaks, the mic listens for exactly one thing: sustained
      // loud speech = a barge-in (thresholds above; echo-cancellation caveat
      // there too). Nothing is streamed until the barge flips the state.
      if (stateRef.current === 'speaking' && !mutedRef.current) {
        const now = Date.now()
        // Track the echo's rolling level the whole time she speaks…
        echoLevelRef.current =
          echoLevelRef.current === 0 ? rms : echoLevelRef.current * 0.95 + rms * 0.05
        // …but stay disarmed through the reply's onset.
        if (bargeArmAtRef.current === 0) bargeArmAtRef.current = now + BARGE_ARM_DELAY_MS
        if (now < bargeArmAtRef.current) return
        const bar = Math.max(BARGE_RMS, echoLevelRef.current * BARGE_OVER_ECHO)
        if (rms >= bar) {
          bargeLastLoudRef.current = now
          if (bargeSinceRef.current == null) bargeSinceRef.current = now
          else if (now - bargeSinceRef.current >= BARGE_MS) doBargeIn()
        } else if (
          bargeSinceRef.current != null &&
          now - bargeLastLoudRef.current > BARGE_HANGOVER_MS
        ) {
          // Real silence, not a syllable gap — disarm.
          bargeSinceRef.current = null
        }
        return
      }
      // Only a listening, unmuted call streams.
      if (stateRef.current !== 'listening' || mutedRef.current) return
      pendingRef.current.push(toPcm16k(samples, inputRate))
      const buffered = pendingRef.current.reduce((n, c) => n + c.length, 0)
      if (buffered >= SEND_CHUNK_SAMPLES) flushPending()

      // Local silence detection — the FALLBACK detector. Stands down while
      // the server's VAD owns turn-taking (P3); re-arms the moment the relay
      // dies and the `vad` event flips back.
      if (serverVadRef.current) return
      const now = Date.now()
      if (rms >= SPEECH_RMS) {
        speechSeenRef.current = true
        quietSinceRef.current = null
      } else if (speechSeenRef.current && rms < QUIET_RMS) {
        if (quietSinceRef.current == null) quietSinceRef.current = now
        else if (now - quietSinceRef.current >= QUIET_MS) endTurn()
      }
    },
    [doBargeIn, endTurn, flushPending],
  )

  // ---------------------------------------------------------------- downlink
  /** Realtime engine audio: each PCM frame becomes an AudioBuffer scheduled
   *  back-to-back on the context clock — gapless by construction, and
   *  stoppable to the frame for barge-in. */
  const schedulePcm = (data: ArrayBuffer) => {
    const ctx = ctxRef.current
    if (!ctx || endedRef.current) return
    const ints = new Int16Array(data.slice(0, data.byteLength - (data.byteLength % 2)))
    if (ints.length === 0) return
    const floats = new Float32Array(ints.length)
    for (let i = 0; i < ints.length; i++) floats[i] = ints[i] / 0x8000
    const buffer = ctx.createBuffer(1, floats.length, pcmRateRef.current)
    buffer.copyToChannel(floats, 0)
    const source = ctx.createBufferSource()
    source.buffer = buffer
    // Through the echo route when it exists — which is what lets the AEC
    // erase her voice from the mic (see openEchoRoute).
    source.connect(echoRouteRef.current?.node ?? ctx.destination)
    const startAt = Math.max(pcmNextTimeRef.current, ctx.currentTime + 0.03)
    pcmNextTimeRef.current = startAt + buffer.duration
    pcmSourcesRef.current.add(source)
    source.onended = () => pcmSourcesRef.current.delete(source)
    source.start(startAt)
    playedThisTurnRef.current = true
    if (stateRef.current !== 'speaking') setCallState('speaking')
  }

  const stopPcm = () => {
    pcmActiveRef.current = false
    if (pcmDrainTimerRef.current) clearTimeout(pcmDrainTimerRef.current)
    pcmDrainTimerRef.current = null
    for (const source of pcmSourcesRef.current) {
      try {
        source.onended = null
        source.stop()
      } catch {
        // already ended
      }
    }
    pcmSourcesRef.current.clear()
    pcmNextTimeRef.current = 0
  }

  /** After pcm_end: flip to listening only once the SCHEDULED audio has been
   *  heard out — the server is seconds ahead of the customer's ears. */
  const armPcmDrain = () => {
    const ctx = ctxRef.current
    if (!ctx) return
    const remaining = Math.max(0, pcmNextTimeRef.current - ctx.currentTime)
    if (pcmDrainTimerRef.current) clearTimeout(pcmDrainTimerRef.current)
    pcmDrainTimerRef.current = setTimeout(() => {
      pcmDrainTimerRef.current = null
      pcmNextTimeRef.current = 0
      if (!endedRef.current && pendingListenRef.current) {
        pendingListenRef.current = false
        setCallState('listening')
      }
    }, remaining * 1000 + 80)
  }

  /** Feed the live MediaSource segment: one append at a time (SourceBuffer
   *  rejects concurrent appends), then close the stream once the server said
   *  audio_end and the pipeline drained. Plain function — called from the WS
   *  handler and from updateend, both outside React's schedule. */
  const pumpLive = (seg: NonNullable<typeof mseRef.current>) => {
    const sb = seg.sb
    if (!sb || sb.updating) return
    const next = seg.pending.shift()
    if (next !== undefined) {
      try {
        sb.appendBuffer(next)
      } catch {
        // Quota or a closed source — the segment ends where it got to.
      }
      return
    }
    if (seg.ended && seg.ms.readyState === 'open') {
      try {
        seg.ms.endOfStream()
      } catch {
        // Already closed — playback is past caring.
      }
    }
  }

  // P4: segments queue and play back-to-back. The queue usually stays a step
  // behind the server (synthesis runs ahead of the voice), which is exactly
  // the pipelining that makes Aisha's first words land while her later
  // sentences are still being written.
  const maybePlayNext = useCallback(() => {
    if (playingRef.current || endedRef.current) return
    const next = playQueueRef.current.shift()
    if (!next) {
      // Drained. If the server already ended the turn, NOW the customer has
      // actually finished hearing it — the parked transition applies.
      if (pendingListenRef.current) {
        pendingListenRef.current = false
        setCallState('listening')
      }
      return
    }
    const url = URL.createObjectURL(next)
    playUrlRef.current = url
    const el = new Audio(url)
    playerRef.current = el
    playingRef.current = true
    playedThisTurnRef.current = true
    if (stateRef.current !== 'speaking') setCallState('speaking')
    const donePlaying = () => {
      el.onended = null
      el.onerror = null
      playerRef.current = null
      playingRef.current = false
      if (playUrlRef.current) {
        URL.revokeObjectURL(playUrlRef.current)
        playUrlRef.current = null
      }
      if (!endedRef.current) maybePlayNext()
    }
    el.onended = donePlaying
    el.onerror = donePlaying
    el.play().catch(donePlaying)
  }, [setCallState])

  /** Open a MediaSource-backed segment and start playing IMMEDIATELY — bytes
   *  append as they arrive (pumpLive). Only ever for the head of the queue
   *  while nothing else is playing; everything queued behind it stays on the
   *  blob path. */
  const startLiveSegment = useCallback(() => {
    const ms = new MediaSource()
    const url = URL.createObjectURL(ms)
    const el = new Audio(url)
    const seg = { ms, sb: null as SourceBuffer | null, el, url, pending: [] as ArrayBuffer[], ended: false }
    mseRef.current = seg
    playerRef.current = el
    playingRef.current = true
    playedThisTurnRef.current = true
    if (stateRef.current !== 'speaking') setCallState('speaking')
    const finish = () => {
      if (mseRef.current === seg) mseRef.current = null
      el.onended = null
      el.onerror = null
      if (playerRef.current === el) playerRef.current = null
      playingRef.current = false
      URL.revokeObjectURL(url)
      if (!endedRef.current) maybePlayNext()
    }
    el.onended = finish
    el.onerror = finish
    ms.addEventListener('sourceopen', () => {
      if (mseRef.current !== seg) return
      try {
        const sb = ms.addSourceBuffer('audio/mpeg')
        seg.sb = sb
        sb.addEventListener('updateend', () => pumpLive(seg))
        pumpLive(seg)
      } catch {
        finish()
      }
    })
    void el.play().catch(finish)
  }, [maybePlayNext, setCallState])

  /** On-device voice when the vendor cannot speak — quality degrades, the
   *  call does not (the useSpeech fallback rule, minus its voice ranking:
   *  intelligible beats absent mid-call). */
  const speakLocally = useCallback(() => {
    const text = replyTextRef.current.trim()
    if (!text || typeof window.speechSynthesis === 'undefined') {
      setCallState('listening')
      return
    }
    const utterance = new SpeechSynthesisUtterance(text)
    utterance.lang = replyLangRef.current === 'ar' ? 'ar' : 'en'
    utterance.rate = 0.95
    setCallState('speaking')
    const donePlaying = () => {
      if (!endedRef.current) setCallState('listening')
    }
    utterance.onend = donePlaying
    utterance.onerror = donePlaying
    window.speechSynthesis.speak(utterance)
  }, [setCallState])

  /** Offer the server a peer connection for this call's audio. Best-effort by
   *  design: any failure, at any step, simply leaves the WS-PCM path carrying
   *  the call — the upgrade can improve a call, never break one. Non-trickle:
   *  ICE gathering is awaited (bounded) so the offer carries every candidate
   *  and no trickle protocol is needed over the WS. */
  const tryRtcUpgrade = useCallback(async () => {
    const stream = streamRef.current
    const ws = wsRef.current
    if (
      typeof RTCPeerConnection === 'undefined' ||
      !stream ||
      !ws ||
      ws.readyState !== WebSocket.OPEN ||
      endedRef.current ||
      rtcRef.current
    ) {
      return
    }
    try {
      const pc = new RTCPeerConnection({ iceServers: iceServersRef.current })
      rtcRef.current = pc
      // The mic goes up AEC'd at the source (getUserMedia constraints), and —
      // the whole point — her voice comes DOWN as a remote track, which is
      // exactly the reference signal the browser's echo canceller was built
      // around. No loopback trick, no RMS gate: a plain phone call.
      stream.getAudioTracks().forEach((t) => pc.addTrack(t, stream))
      pc.ontrack = (e) => {
        const el = rtcAudioElRef.current ?? new Audio()
        rtcAudioElRef.current = el
        el.autoplay = true
        el.srcObject = e.streams[0]
        void el.play().catch(() => undefined)
      }
      pc.onconnectionstatechange = () => {
        if (
          (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') &&
          rtcActiveRef.current
        ) {
          // The server notices too (rtc_failed) — flipping locally as well
          // just re-opens the WS uplink a round-trip sooner.
          rtcActiveRef.current = false
          console.warn('[call] webrtc lost — ws audio fallback')
        }
      }
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === 'complete') return resolve()
        const timer = window.setTimeout(resolve, 2000)
        pc.onicegatheringstatechange = () => {
          if (pc.iceGatheringState === 'complete') {
            clearTimeout(timer)
            resolve()
          }
        }
      })
      if (endedRef.current || wsRef.current !== ws) return
      const sdp = pc.localDescription?.sdp
      if (sdp) sendJson({ type: 'rtc_offer', sdp })
    } catch {
      // Stay on the WS path; nothing to clean beyond the ref.
      try {
        rtcRef.current?.close()
      } catch {
        // already closed
      }
      rtcRef.current = null
    }
  }, [sendJson])

  const onWsMessage = useCallback(
    (event: MessageEvent) => {
      if (typeof event.data !== 'string') {
        // Binary = TTS audio between audio_start and audio_end. A LIVE
        // segment (not yet closed by its audio_end) eats bytes directly;
        // anything else accumulates for blob playback in queue order.
        if (pcmActiveRef.current) {
          schedulePcm(event.data as ArrayBuffer)
          return
        }
        const seg = mseRef.current
        if (seg && !seg.ended) {
          seg.pending.push(event.data as ArrayBuffer)
          pumpLive(seg)
        } else {
          playChunksRef.current.push(event.data as ArrayBuffer)
        }
        return
      }
      let msg: CallStreamEvent
      try {
        msg = JSON.parse(event.data)
      } catch {
        return
      }
      switch (msg.type) {
        case 'call_started':
          engineRef.current = msg.engine === 'realtime' ? 'realtime' : 'pipeline'
          // Captured BEFORE tryRtcUpgrade() below, which builds the peer
          // connection from it on the very next line — an upgrade that
          // negotiated without these would gather only unusable candidates.
          iceServersRef.current = Array.isArray(msg.ice_servers)
            ? (msg.ice_servers as RTCIceServer[])
            : []
          if (engineRef.current === 'realtime') {
            // Dial-until-ready (2026-08-03): the server holds the greeting
            // until the audio upgrade settles, so keep RINGING — honestly
            // "connecting", not "listening" — until call_ready says picked
            // up. The upgrade negotiates under the ring.
            setCallState('connecting')
            if (msg.rtc === true) void tryRtcUpgrade()
          } else {
            // Pipeline greets nothing and is ready at once: picked up — the
            // ring stops and a short rising cue marks the connection.
            callReadyRef.current = true
            stopRing()
            playTones([[600, 0], [900, 0.08]], 0.05)
          }
          break
        case 'call_ready':
          // Picked up: the ring stops and a short rising cue marks the
          // connection — Aisha's greeting follows on its heels.
          callReadyRef.current = true
          stopRing()
          playTones([[600, 0], [900, 0.08]], 0.05)
          if (!endedRef.current) setCallState('listening')
          break
        case 'rtc_answer':
          void rtcRef.current
            ?.setRemoteDescription({ type: 'answer', sdp: (msg.sdp as string) ?? '' })
            .catch((err: unknown) => {
              // A REJECTED ANSWER USED TO VANISH HERE (`.catch(() => undefined)`,
              // fixed 2026-08-04) and it cost days. A browser that cannot apply
              // the answer never starts ICE, so the server sees its own
              // connectivity checks go out and NOTHING come back — identical,
              // from the outside, to a network that drops our packets. We chased
              // the network three times.
              //
              // Reported to the SERVER, not just the console: the person
              // debugging a call is reading server logs, and asking a customer
              // on a phone to open devtools is not a diagnostic path. The call
              // itself is unaffected — WS-PCM was already carrying it and keeps
              // doing so; this only makes the downgrade explain itself.
              const detail =
                err instanceof Error ? `${err.name}: ${err.message}` : String(err)
              console.error('[call] webrtc answer rejected —', detail)
              sendJson({ type: 'rtc_error', detail })
            })
          break
        case 'rtc_active':
          // ICE connected server-side: audio has MOVED. The worklet uplink
          // and every echo heuristic stand down; the browser's native AEC3
          // owns echo from here.
          rtcActiveRef.current = true
          console.info('[call] webrtc audio active — native AEC')
          break
        case 'rtc_unavailable':
          console.info('[call] webrtc unavailable — ws audio path')
          break
        case 'rtc_failed':
          // Mid-call transport death: the WS uplink resumes on the next audio
          // block, the server routes her voice back over WS bytes. A
          // transport failure costs QUALITY, never the call.
          rtcActiveRef.current = false
          console.warn('[call] webrtc lost — ws audio fallback')
          break
        case 'stop_audio':
          // The vendor's VAD heard the customer over Aisha (native barge-in):
          // her audio stops NOW, mid-frame.
          stopPlayback()
          pendingListenRef.current = false
          if (!endedRef.current) setCallState('listening')
          break
        case 'vad':
          serverVadRef.current = Boolean(msg.server)
          break
        case 'state': {
          // Still ringing: the server stamps an initial "listening" before
          // the transport has settled — the overlay stays "connecting" until
          // call_ready says the call is real (dial-until-ready, 2026-08-03).
          if (engineRef.current === 'realtime' && !callReadyRef.current) break
          const value = msg.value as CallState
          if (value === 'listening') {
            // Applied only once the customer has actually HEARD the reply —
            // the queue (mp3 path) or the schedule (PCM path) owns the moment.
            const ctx = ctxRef.current
            const pcmAhead =
              pcmActiveRef.current ||
              (ctx != null && pcmNextTimeRef.current > ctx.currentTime + 0.05)
            if (playingRef.current || playQueueRef.current.length > 0 || pcmAhead) {
              pendingListenRef.current = true
              if (pcmAhead && !pcmActiveRef.current) armPcmDrain()
            } else {
              setCallState('listening')
            }
          } else if (value === 'thinking') {
            setCallState(value)
          }
          break
        }
        case 'utterance':
          replyTextRef.current = ''
          playedThisTurnRef.current = false
          pendingListenRef.current = false
          bargeSinceRef.current = null
          optsRef.current.onUtterance?.({
            audioId: (msg.audio_id as string) ?? null,
            text: (msg.text as string) ?? '',
            lang: (msg.lang as 'en' | 'ar') ?? null,
            durationMs: (msg.duration_ms as number) ?? null,
            failed: Boolean(msg.failed),
          })
          break
        case 'utterance_failed':
          // Nothing was heard and no turn will run — back to listening. Not
          // surfaced as an error: a breath should not draw an apology.
          setCallState('listening')
          break
        case 'token':
          replyTextRef.current += (msg.text as string) ?? ''
          optsRef.current.onStreamEvent?.(msg)
          break
        case 'done':
          replyLangRef.current = (msg.lang as 'en' | 'ar') ?? 'en'
          optsRef.current.onStreamEvent?.(msg)
          break
        case 'tool_event':
        case 'booking':
        case 'text_break':
        case 'closed':
        case 'error':
          optsRef.current.onStreamEvent?.(msg)
          break
        case 'pcm_start':
          pcmActiveRef.current = true
          pcmRateRef.current = (msg.rate as number) || 24_000
          break
        case 'pcm_end':
          pcmActiveRef.current = false
          armPcmDrain()
          break
        case 'audio_start':
          playMimeRef.current = (msg.mime as string) || 'audio/mpeg'
          playChunksRef.current = []
          if (
            playMimeRef.current === 'audio/mpeg' &&
            typeof MediaSource !== 'undefined' &&
            MediaSource.isTypeSupported('audio/mpeg') &&
            !playingRef.current &&
            playQueueRef.current.length === 0 &&
            !endedRef.current
          ) {
            startLiveSegment()
          }
          break
        case 'audio_end': {
          const seg = mseRef.current
          if (seg && !seg.ended) {
            seg.ended = true
            pumpLive(seg)
            break
          }
          const chunks = playChunksRef.current
          playChunksRef.current = []
          if (chunks.length > 0) {
            playQueueRef.current.push(new Blob(chunks, { type: playMimeRef.current }))
            maybePlayNext()
          }
          break
        }
        case 'tts_failed':
          // Sentence-level now: if part of the reply was already spoken, a
          // repeat from the top would be worse than the gap — the text is on
          // screen. Only a reply with NO audio at all gets the on-device
          // voice.
          if (
            !playedThisTurnRef.current &&
            !playingRef.current &&
            playQueueRef.current.length === 0
          ) {
            speakLocally()
          }
          break
        case 'call_ended':
          endCall((msg.reason as string) || 'ended')
          break
      }
    },
    [endCall, maybePlayNext, playTones, setCallState, speakLocally, stopRing, tryRtcUpgrade],
  )

  // ------------------------------------------------------------------- start
  const start = useCallback(() => {
    if (!supported || active) return
    // Claim the generation (disarms any pending deferred teardown from the
    // previous call) and clear that call's leftovers ourselves, now.
    generationRef.current += 1
    teardown()
    endedRef.current = false
    serverVadRef.current = false // client-owned until the server says otherwise
    callReadyRef.current = false // ringing until call_ready (realtime) / call_started (pipeline)
    setEndReason(null)
    setActive(true)
    setCallState('connecting')
    startedAtRef.current = Date.now()
    setElapsedMs(0)
    levelRef.current = 0 // a fresh call's ring starts calm

    void (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        })
        if (endedRef.current) {
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        streamRef.current = stream

        const ctx = new AudioContext()
        ctxRef.current = ctx
        // Ring while everything below connects — answering a phone.
        playRingBurst()
        ringTimerRef.current = setInterval(playRingBurst, 3000)
        const workletUrl = URL.createObjectURL(
          new Blob([WORKLET_SOURCE], { type: 'text/javascript' }),
        )
        workletUrlRef.current = workletUrl
        await ctx.audioWorklet.addModule(workletUrl)
        if (endedRef.current) return

        // The AEC loopback — see openEchoRoute. Awaited BEFORE the socket
        // opens so the first reply is already routed; null means the unrouted
        // fallback (old gating) carries the call instead.
        echoRouteRef.current = await openEchoRoute(ctx)
        // Deliberate console breadcrumb: whether the AEC loopback engaged is
        // invisible on screen but decides which capture path a call ran on —
        // the first thing to check when diagnosing echo or interruption.
        console.info(
          '[call] echo route:',
          echoRouteRef.current ? 'AEC loopback active' : 'unavailable — gated fallback',
        )
        if (endedRef.current) {
          echoRouteRef.current?.close()
          echoRouteRef.current = null
          return
        }

        const source = ctx.createMediaStreamSource(stream)
        const node = new AudioWorkletNode(ctx, 'call-capture')
        nodeRef.current = node
        source.connect(node)
        // Not connected to destination: capture only, no monitoring loop.

        const inputRate = ctx.sampleRate
        node.port.onmessage = (e: MessageEvent) => {
          const { samples, rms } = e.data as { samples: Float32Array; rms: number }
          onAudioBlock(samples, rms, inputRate)
        }

        const ws = new WebSocket(wsUrl(getSessionId()))
        // arraybuffer, not blob: SourceBuffer.appendBuffer wants ArrayBuffer,
        // and Blob construction accepts them as parts for the fallback path.
        ws.binaryType = 'arraybuffer'
        wsRef.current = ws
        ws.onopen = () => {
          sendJson({ type: 'start', sample_rate: TARGET_SAMPLE_RATE })
          // Stay "connecting" — the call is ready when the server says so
          // (call_ready / the pipeline's state event), not when the socket
          // opens; the ring is still playing (dial-until-ready, 2026-08-03).
          tickRef.current = setInterval(
            () => setElapsedMs(Date.now() - startedAtRef.current),
            500,
          )
        }
        ws.onmessage = onWsMessage
        ws.onerror = () => {
          if (!endedRef.current) endCall('connection_failed')
        }
        ws.onclose = () => {
          // A dropped call ENDS — it does not silently resume. The server has
          // already rolled back any in-flight turn (AD-E).
          if (!endedRef.current) endCall('connection_lost')
        }
      } catch {
        endCall('mic_denied')
      }
    })()
  }, [active, endCall, getSessionId, onAudioBlock, onWsMessage, playRingBurst, sendJson, setCallState, supported, teardown])

  const hangup = useCallback(() => {
    if (endedRef.current) return
    // Best effort: say goodbye on the wire, then end locally either way.
    sendJson({ type: 'hangup' })
    endCall('hangup')
  }, [endCall, sendJson])

  const toggleMute = useCallback(() => {
    const next = !mutedRef.current
    mutedRef.current = next
    setMuted(next)
    sendJson({ type: next ? 'mute' : 'unmute' })
    // Belt at the source for the rtc path: a disabled track sends silence
    // frames, so nothing leaves the device (the server honours mute too).
    streamRef.current?.getAudioTracks().forEach((t) => {
      t.enabled = !next
    })
    if (next) {
      // A muted mic must not leave a half-heard utterance armed.
      pendingRef.current = []
      speechSeenRef.current = false
      quietSinceRef.current = null
    }
  }, [sendJson])

  // Unmount (reload, route change) must not leave a mic open or a call billed.
  useEffect(() => {
    return () => {
      endedRef.current = true
      teardown()
    }
  }, [teardown])

  return {
    state, active, muted, elapsedMs, endReason, supported,
    getLevel: () => levelRef.current,
    start, hangup, toggleMute,
  }
}
