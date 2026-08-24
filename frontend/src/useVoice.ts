// Microphone capture for voice notes (input only — see the 2026-07-21 scope
// override in CLAUDE.md). Records, caps, and hands back a blob. It does NOT
// upload, transcribe or know about chat: that belongs to useChat, so this stays
// a plain "give me audio" primitive.
//
// TAP TO START, TAP TO STOP — not WhatsApp's hold-to-talk. Chosen deliberately
// for a live demo on strangers' phones: a long-press fights mobile Safari's own
// text-selection and callout gestures, and a finger slipping mid-sentence sends
// a truncated note with no way to tell it happened. A tap cannot slip.
//
// FORMAT IS NEGOTIATED, NEVER ASSUMED, and nothing is transcoded. Chrome and
// Android produce webm/opus; Safari — macOS and iOS alike — produces mp4/aac.
// The backend accepts both and passes the container through to the vendor, so
// the browser difference costs nothing and needs no iOS-specific branch.
//
// HTTPS IS REQUIRED and this is the one failure no code can fix: getUserMedia is
// blocked on plain HTTP everywhere except localhost. Dev is fine; QR attendees on
// http://<ip>:5173 will find `supported` false. Surfaced as an explicit state
// rather than a dead button, so the cause is visible on stage instead of looking
// like a bug. See the demo-day blocker in CLAUDE.md.

import { useCallback, useEffect, useRef, useState } from 'react'

/** Hard ceiling, mirrored server-side (config.VOICE_MAX_SECONDS). The browser
 *  cap is an affordance — it stops a pocket recording running forever — and the
 *  server's is the actual limit. Both exist on purpose. */
export const MAX_RECORDING_MS = 30_000

/** Preference order. The first supported entry wins; opus first because it is
 *  markedly smaller per second, mp4 next for the WebKit family. */
const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
  'audio/ogg',
]

export type VoiceState =
  | 'idle'
  | 'requesting' // permission prompt is on screen
  | 'recording'
  | 'denied' // customer said no, or no microphone exists
  | 'unsupported' // no MediaRecorder, or an insecure origin (HTTP)

export interface UseVoice {
  state: VoiceState
  /** Milliseconds recorded so far — drives the timer and the progress ring. */
  elapsedMs: number
  /** False when recording can never work here. Check BEFORE drawing the button. */
  supported: boolean
  start: () => void
  /** Finish and emit the blob through onRecorded. */
  stop: () => void
  /** Discard. Nothing is emitted, nothing is uploaded. */
  cancel: () => void
}

function pickMime(): string | null {
  if (typeof MediaRecorder === 'undefined') return null
  for (const m of MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(m)) return m
  }
  // Some Safari builds report support for nothing yet still record with the
  // implementation default. An empty string tells MediaRecorder to choose.
  return ''
}

function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof MediaRecorder !== 'undefined' &&
    // `mediaDevices` is UNDEFINED on an insecure origin — this is exactly how
    // the HTTP blocker shows up, so it is not a paranoid check.
    !!navigator.mediaDevices?.getUserMedia
  )
}

export function useVoice(
  onRecorded: (blob: Blob, mime: string, durationMs: number) => void,
): UseVoice {
  const supported = isSupported()
  const [state, setState] = useState<VoiceState>(supported ? 'idle' : 'unsupported')
  const [elapsedMs, setElapsedMs] = useState(0)

  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<BlobPart[]>([])
  const startedAtRef = useRef(0)
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const capRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Set by cancel(), read in onstop. The recorder fires onstop either way, so
  // the discard decision has to survive the trip across that callback.
  const discardRef = useRef(false)
  // Latest callback without re-subscribing the recorder on every render.
  const onRecordedRef = useRef(onRecorded)
  onRecordedRef.current = onRecorded

  const clearTimers = () => {
    if (tickRef.current) clearInterval(tickRef.current)
    if (capRef.current) clearTimeout(capRef.current)
    tickRef.current = null
    capRef.current = null
  }

  /** Release the microphone. NOT optional: an un-stopped track leaves the
   *  browser's recording indicator lit, which on a phone handed round an
   *  audience reads as "this site is still listening" — and would be true. */
  const releaseTracks = (recorder: MediaRecorder | null) => {
    recorder?.stream.getTracks().forEach((t) => t.stop())
  }

  const finish = useCallback((discard: boolean) => {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') return
    discardRef.current = discard
    // `stop()` flushes a final dataavailable before onstop, so the tail of the
    // recording is not lost.
    recorder.stop()
  }, [])

  const start = useCallback(() => {
    if (!supported || recorderRef.current) return
    setState('requesting')
    setElapsedMs(0)
    chunksRef.current = []
    discardRef.current = false

    navigator.mediaDevices
      .getUserMedia({
        audio: {
          // Speech in a noisy showroom, not music. These are hints; browsers
          // that ignore them simply record as they would have anyway.
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      .then((stream) => {
        const mime = pickMime()
        const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
        recorderRef.current = recorder

        recorder.ondataavailable = (e) => {
          if (e.data.size > 0) chunksRef.current.push(e.data)
        }

        recorder.onstop = () => {
          clearTimers()
          releaseTracks(recorder)
          const chunks = chunksRef.current
          chunksRef.current = []
          recorderRef.current = null
          const durationMs = Date.now() - startedAtRef.current
          setState('idle')
          setElapsedMs(0)
          if (discardRef.current) return
          // `recorder.mimeType` rather than our requested `mime`: the browser
          // has the last word on what it actually produced, and the backend
          // needs the truth to name the file for the vendor.
          const type = recorder.mimeType || mime || 'audio/webm'
          const blob = new Blob(chunks, { type })
          if (blob.size === 0) return
          onRecordedRef.current(blob, type.split(';')[0], durationMs)
        }

        startedAtRef.current = Date.now()
        recorder.start()
        setState('recording')

        tickRef.current = setInterval(
          () => setElapsedMs(Date.now() - startedAtRef.current),
          100,
        )
        // Auto-stop at the cap and KEEP what was recorded — the customer was
        // mid-sentence, and throwing away 60 seconds of their speech to punish
        // them for running long would be the worse failure.
        capRef.current = setTimeout(() => finish(false), MAX_RECORDING_MS)
      })
      .catch(() => {
        // Denied, dismissed, or no input device. All one state: the button
        // becomes unavailable and typing carries on working.
        recorderRef.current = null
        setState('denied')
      })
  }, [finish, supported])

  const stop = useCallback(() => finish(false), [finish])
  const cancel = useCallback(() => finish(true), [finish])

  // Unmount mid-recording (a reload, a "New chat") must not leave the mic open.
  useEffect(() => {
    return () => {
      clearTimers()
      const recorder = recorderRef.current
      if (recorder && recorder.state !== 'inactive') {
        discardRef.current = true
        recorder.stop()
      }
      releaseTracks(recorder)
    }
  }, [])

  return { state, elapsedMs, supported, start, stop, cancel }
}
