// One chat bubble. Direction is per-BUBBLE (AD from CLAUDE.md): dir="rtl" and
// right-alignment come from the bubble's OWN text, so an Arabic reply renders RTL
// with native glyphs even mid-conversation — independent of dialogue quality.
//
// A SPOKEN turn (voice notes, 2026-07-21) is the SAME bubble with a player above
// the transcript, not a second message: the customer said one thing, and the
// recording and its transcription are two views of it. When nothing could be
// transcribed the text is empty and the player stands alone — the honest record
// that a recording exists and no words were recovered from it. Aisha then
// answers that in her own voice, which is why there is no canned line here.
//
// READ-ALOUD (2026-07-21) is the mirror control, on AISHA's bubbles only: a
// speaker that reads that message in the language it is written in. Playback,
// never a spoken agent — she still writes, and nothing is ever read unless the
// customer asks for it.

import { dirFor } from '../rtl'
import { VOICE_LABELS } from '../i18n'
import type { TextMessage } from '../useChat'
import { VoicePlayer } from './VoicePlayer'

// Turn bare URLs in a reply into clickable links. A Google Maps DIRECTIONS link
// (the nearest-showroom feature) renders as a compact "Get directions" chip
// instead of a long raw URL; any other link renders inline, underlined. Trailing
// sentence punctuation is peeled off the href so it does not become part of it.
const URL_SPLIT = /(https?:\/\/[^\s]+)/g

function linkify(text: string) {
  if (!text) return text
  return text.split(URL_SPLIT).map((part, i) => {
    if (!/^https?:\/\//.test(part)) return <span key={i}>{part}</span>
    const m = part.match(/^(.*?)([.,;:!?)\]]*)$/)
    const url = m ? m[1] : part
    const tail = m ? m[2] : ''
    const isDirections = /google\.[^/]*\/maps\/dir/.test(url)
    return (
      <span key={i}>
        {isDirections ? (
          <a href={url} target="_blank" rel="noopener noreferrer" className="dir-link">
            {'\u{1F9ED}'} Get directions
          </a>
        ) : (
          <a href={url} target="_blank" rel="noopener noreferrer" className="msg-link">
            {url}
          </a>
        )}
        {tail}
      </span>
    )
  })
}

export function MessageBubble({
  message,
  speaking = false,
  loading = false,
  onSpeak,
}: {
  message: TextMessage
  /** This message is the one currently being read aloud. */
  speaking?: boolean
  /** Its audio is being synthesised. Only the FIRST play of a message waits —
   *  the server caches the clip, so every replay after it starts immediately. */
  loading?: boolean
  /** Absent when read-aloud is unavailable for this message — the device has no
   *  voice for its language, or it is not an agent message. The CONTROL IS THEN
   *  NOT RENDERED: an absent affordance is honest, while one that does nothing
   *  reads as broken and one that reads Arabic in an English voice is worse. */
  onSpeak?: () => void
}) {
  // A voice note with no transcript has no text to sniff, so fall back to the
  // turn's own language rather than defaulting silently to LTR.
  const dir = message.text ? dirFor(message.text) : message.lang === 'ar' ? 'rtl' : 'ltr'
  const blinking = message.streaming && message.text.length === 0 && !message.audioSrc
  const labels = VOICE_LABELS[message.lang]
  return (
    <div className={`row row--${message.role}`}>
      <div
        className={`bubble bubble--${message.role} bubble--${dir}`}
        dir={dir}
        lang={message.lang}
      >
        {message.audioSrc && (
          <VoicePlayer
            src={message.audioSrc}
            durationMs={message.durationMs}
            lang={message.lang}
          />
        )}
        {linkify(message.text)}
        {message.streaming && message.text.length > 0 && <span className="caret" aria-hidden="true" />}
        {blinking && <span className="dots" aria-label="typing"><i /><i /><i /></span>}
        {onSpeak && (
          <button
            type="button"
            className={`speak-btn${speaking ? ' speak-btn--on' : ''}${loading ? ' speak-btn--loading' : ''}`}
            disabled={loading}
            onClick={onSpeak}
            aria-label={speaking ? labels.stopReading : labels.readAloud}
            title={speaking ? labels.stopReading : labels.readAloud}
          >
            {loading ? (
              // Synthesising. A ring rather than the stop square, so "waiting"
              // and "playing" are never mistaken for each other mid-demo.
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <circle
                  cx="8" cy="8" r="5.5"
                  stroke="currentColor" strokeWidth="1.8" fill="none"
                  strokeDasharray="26" strokeDashoffset="8" strokeLinecap="round"
                />
              </svg>
            ) : speaking ? (
              // Speaking: a stop square. Deliberately NOT a second speaker with
              // a slash — at this size the two speaker glyphs are hard to tell
              // apart at a glance, and the control is a toggle, so its state has
              // to be unmistakable.
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <rect x="3.5" y="3.5" width="9" height="9" rx="1.5" fill="currentColor" />
              </svg>
            ) : (
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <path d="M8.5 2.2 5 5H3a.8.8 0 0 0-.8.8v4.4A.8.8 0 0 0 3 11h2l3.5 2.8a.5.5 0 0 0 .8-.4V2.6a.5.5 0 0 0-.8-.4z" fill="currentColor" />
                <path
                  d="M11.4 5.6a3.3 3.3 0 0 1 0 4.8M13.2 3.8a5.8 5.8 0 0 1 0 8.4"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinecap="round"
                  fill="none"
                />
              </svg>
            )}
          </button>
        )}
      </div>
    </div>
  )
}
