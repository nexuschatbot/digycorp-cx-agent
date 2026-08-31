// Read one of Aisha's messages aloud, on request (read-aloud, 2026-07-21).
//
// PLAYBACK, NOT A SPOKEN AGENT. Aisha still writes; nothing is ever spoken
// unless a customer taps the speaker on a specific message. No automatic speech,
// no modality mirroring — a voice note still gets a text reply.
//
// TWO ENGINES, IN THIS ORDER, AND THE FALLBACK IS THE POINT
// ---------------------------------------------------------
//   1. SERVER (GET /api/speech/{turn_id}) — one warm Gulf-register voice per
//      language, identical on every attendee's phone. This is the engine the
//      feature is for.
//   2. BROWSER (window.speechSynthesis) — whatever voices the device happens to
//      have. Used when the turn is not yet stored, when the vendor is down, or
//      when TTS is unconfigured.
//
// The browser engine started as the whole feature and was demoted, not deleted,
// because it degrades exactly where a demo needs it to: a vendor outage or a
// flat conference network costs QUALITY, never the control. An icon that stops
// working mid-demo is worse than one that sounds worse.
//
// It is also why the device's own voices still matter. On the demo hardware the
// English ones were 1990s novelty voices (Boing, Zarvox, Fred) and Arabic had
// exactly one — so `pickVoice` ranks by name and refuses the joke voices rather
// than taking the first match, which is what made the original sound broken.
//
// THE CLIENT NEVER SENDS AISHA'S WORDS BACK. It names a turn; the server reads
// the transcript itself. Nothing can put speech in her mouth she did not write,
// and the endpoint cannot be used as a free text-to-speech proxy.

import { useCallback, useEffect, useRef, useState } from 'react'

import { speechUrl } from './api'
import type { Lang } from './i18n'
import { forSpeech } from './speech-text'

export interface UseSpeech {
  /** The message being read, or null. */
  speakingId: string | null
  /** The message whose audio is being fetched — the first play of a message
   *  waits on synthesis (~1s); every replay after it is served from cache. */
  loadingId: string | null
  speak: (id: string, text: string, lang: Lang, turnId?: number) => void
  stop: () => void
  /** Can this message be read at all? True whenever the server can do it (the
   *  turn is stored), or the device has a voice for the language. False -> the
   *  UI renders NO control, rather than one that does nothing. */
  canSpeak: (lang: Lang, turnId?: number) => boolean
}

const synthSupported = typeof window !== 'undefined' && 'speechSynthesis' in window

// macOS ships ~180 voices, most of them sound effects. Taking "the first English
// voice" lands on Albert or Bad News as easily as on Samantha — which is what
// made the on-device engine sound broken before it was ranked.
const JOKE_VOICES = new Set([
  'Albert', 'Bad News', 'Bahh', 'Bells', 'Boing', 'Bubbles', 'Cellos', 'Wobble',
  'Good News', 'Jester', 'Organ', 'Superstar', 'Trinoids', 'Whisper', 'Zarvox',
  'Grandma', 'Grandpa', 'Junior', 'Ralph', 'Fred', 'Kathy', 'Deranged',
  'Hysterical', 'Pipe Organ', 'Princess', 'Bruce', 'Agnes', 'Victoria',
])

// Best first. Named explicitly because "natural-sounding" is not a property any
// browser exposes — there is no quality flag on SpeechSynthesisVoice, so the
// only way to prefer a good voice is to know its name.
const PREFERRED: Record<Lang, string[]> = {
  en: ['Google US English', 'Google UK English Female', 'Samantha', 'Daniel', 'Karen', 'Moira'],
  ar: ['Google العربية', 'Majed', 'Maged', 'Tarik'],
}

function pickVoice(voices: SpeechSynthesisVoice[], lang: Lang): SpeechSynthesisVoice | null {
  const matches = voices.filter((v) => v.lang?.toLowerCase().startsWith(lang))
  for (const name of PREFERRED[lang]) {
    const hit = matches.find((v) => v.name === name || v.name.includes(name))
    if (hit) return hit
  }
  // No known-good voice: take any that is not a sound effect. NOTE there is no
  // `localService` preference here — the earlier version preferred local voices,
  // which actively rejected Chrome's remote Google voices, the best ones present.
  return matches.find((v) => !JOKE_VOICES.has(v.name)) ?? null
}

export function useSpeech(sessionId: string): UseSpeech {
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([])
  const [speakingId, setSpeakingId] = useState<string | null>(null)
  const [loadingId, setLoadingId] = useState<string | null>(null)
  const speakingIdRef = useRef<string | null>(null)
  speakingIdRef.current = speakingId
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const keepAliveRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // VOICES LOAD ASYNCHRONOUSLY. getVoices() is commonly EMPTY on the first call
  // and fills in later — reading it once at mount is the classic way to conclude
  // a device has no voices at all.
  useEffect(() => {
    if (!synthSupported) return
    const load = () => setVoices(window.speechSynthesis.getVoices())
    load()
    window.speechSynthesis.addEventListener('voiceschanged', load)
    return () => window.speechSynthesis.removeEventListener('voiceschanged', load)
  }, [])

  const clearKeepAlive = () => {
    if (keepAliveRef.current) clearInterval(keepAliveRef.current)
    keepAliveRef.current = null
  }

  const stop = useCallback(() => {
    clearKeepAlive()
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current.src = ''
      audioRef.current = null
    }
    if (synthSupported) window.speechSynthesis.cancel()
    setSpeakingId(null)
    setLoadingId(null)
  }, [])

  /** The on-device engine. Also the fallback path, hence its own function. */
  const speakLocally = useCallback(
    (id: string, text: string, lang: Lang) => {
      if (!synthSupported) {
        setLoadingId(null)
        return
      }
      const voice = pickVoice(voices, lang)
      // Refuse rather than let a device read Arabic in an English voice —
      // confident nonsense is worse than silence.
      if (!voice) {
        setLoadingId(null)
        return
      }
      const u = new SpeechSynthesisUtterance(forSpeech(text))
      u.voice = voice
      u.lang = voice.lang
      u.rate = 0.95
      const done = () => {
        clearKeepAlive()
        setSpeakingId((cur) => (cur === id ? null : cur))
      }
      u.onend = done
      u.onerror = done
      setLoadingId(null)
      setSpeakingId(id)
      window.speechSynthesis.speak(u)
      // Desktop Chrome silently stops synthesis after ~15s — a long-standing
      // bug that would truncate exactly the long recaps worth listening to.
      keepAliveRef.current = setInterval(() => {
        if (!window.speechSynthesis.speaking) return clearKeepAlive()
        window.speechSynthesis.pause()
        window.speechSynthesis.resume()
      }, 10_000)
    },
    [voices],
  )

  const speak = useCallback(
    (id: string, text: string, lang: Lang, turnId?: number) => {
      // Toggle: tapping the speaker on the message already playing stops it.
      const wasSpeaking = speakingIdRef.current === id || loadingId === id
      stop()
      if (wasSpeaking) return

      if (turnId == null || !sessionId) {
        // Not stored yet (or no session) — nothing for the server to read.
        speakLocally(id, text, lang)
        return
      }

      setLoadingId(id)
      const el = new Audio(speechUrl(turnId, sessionId))
      audioRef.current = el
      el.onplaying = () => {
        setLoadingId(null)
        setSpeakingId(id)
      }
      el.onended = () => {
        audioRef.current = null
        setSpeakingId((cur) => (cur === id ? null : cur))
      }
      // 404 (not ours to read) or 503 (vendor down / unconfigured) both land
      // here. Fall back rather than fail: quality degrades, the control does not.
      el.onerror = () => {
        audioRef.current = null
        speakLocally(id, text, lang)
      }
      void el.play().catch(() => {
        audioRef.current = null
        speakLocally(id, text, lang)
      })
    },
    [loadingId, sessionId, speakLocally, stop],
  )

  const canSpeak = useCallback(
    (lang: Lang, turnId?: number) => {
      if (turnId != null && sessionId) return true // the server can always read it
      return synthSupported && pickVoice(voices, lang) != null
    },
    [sessionId, voices],
  )

  // Never leave a voice talking to an empty room: a reload, a "New chat", or a
  // navigation while a long recap is mid-sentence.
  useEffect(() => {
    return () => {
      clearKeepAlive()
      if (audioRef.current) audioRef.current.pause()
      if (synthSupported) window.speechSynthesis.cancel()
    }
  }, [])

  return { speakingId, loadingId, speak, stop, canSpeak }
}
