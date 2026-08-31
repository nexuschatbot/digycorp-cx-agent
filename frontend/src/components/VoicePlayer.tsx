// The customer's own voice note, replayable in the thread (voice notes,
// 2026-07-21). Sits ABOVE the transcript inside the same bubble — one turn, not
// two — so what was said and what we heard read as a single message.
//
// A PROGRESS BAR, NOT A REAL WAVEFORM. A true waveform means decoding the audio
// client-side to sample its amplitudes; that is half a day of work and a
// noticeable CPU spike on a mid-range phone, to draw something decorative. The
// bar carries the information that matters (how long, how far through).
//
// LAZY: the <audio> element has no src until the first tap. Rebuilding a thread
// with several voice notes must not fetch every recording on load — especially
// on the conference data connection the QR attendees are on — and the backend
// serves these no-store, so nothing would be cached anyway.
//
// A 404 REMOVES THE PLAYER, never renders it broken. That is the same rule the
// vehicle and booking cards follow (FR-CX-07): an expired or erased recording
// leaves the transcript text standing on its own, which is honest, rather than a
// dead play button that lies about being playable.

import { useEffect, useRef, useState } from 'react'

import type { Lang } from '../i18n'
import { VOICE_LABELS } from '../i18n'

function clock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

export function VoicePlayer({
  src,
  durationMs,
  lang,
}: {
  src: string
  /** Known at record time; unknown (0) after a reload until metadata loads. */
  durationMs?: number
  lang: Lang
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState(false)
  const [positionMs, setPositionMs] = useState(0)
  const [totalMs, setTotalMs] = useState(durationMs ?? 0)
  const [gone, setGone] = useState(false)
  const labels = VOICE_LABELS[lang]

  useEffect(() => {
    // A recording that has expired or been erased: drop the player entirely.
    setGone(false)
  }, [src])

  if (gone) return null

  const toggle = () => {
    const el = audioRef.current
    if (!el) return
    if (playing) {
      el.pause()
      return
    }
    if (!el.src) el.src = src // lazy — see the header note
    void el.play().catch(() => setGone(true))
  }

  const pct = totalMs > 0 ? Math.min(100, (positionMs / totalMs) * 100) : 0
  const shown = playing || positionMs > 0 ? positionMs : totalMs

  return (
    <div className="voice-player">
      <button
        type="button"
        className="voice-play"
        onClick={toggle}
        aria-label={playing ? labels.pause : labels.play}
      >
        {playing ? (
          <svg viewBox="0 0 16 16" aria-hidden="true" width="14" height="14">
            <rect x="3" y="2" width="4" height="12" rx="1" fill="currentColor" />
            <rect x="9" y="2" width="4" height="12" rx="1" fill="currentColor" />
          </svg>
        ) : (
          <svg viewBox="0 0 16 16" aria-hidden="true" width="14" height="14">
            <path d="M4 2.5v11l9-5.5-9-5.5z" fill="currentColor" />
          </svg>
        )}
      </button>

      <div className="voice-track" aria-hidden="true">
        <div className="voice-progress" style={{ width: `${pct}%` }} />
      </div>

      <span className="voice-time">{clock(shown)}</span>

      <audio
        ref={audioRef}
        preload="none"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          setPlaying(false)
          setPositionMs(0)
        }}
        onTimeUpdate={(e) => setPositionMs(e.currentTarget.currentTime * 1000)}
        onLoadedMetadata={(e) => {
          const d = e.currentTarget.duration
          // WebM/Opus from MediaRecorder often reports Infinity for duration —
          // a known quirk of streamed containers with no seek index. Keep the
          // recorded value in that case rather than rendering "Infinity:NaN".
          if (Number.isFinite(d) && d > 0) setTotalMs(d * 1000)
        }}
        // The recording is gone (expired, erased, or never ours): remove the
        // player rather than leave a control that cannot work.
        onError={() => setGone(true)}
      />
    </div>
  )
}
