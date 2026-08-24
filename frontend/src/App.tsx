// Chat widget — Stage 3 (transport rendering). Consumes the /api/chat SSE stream
// (AD-B) via useChat: tokens grow the last agent bubble live, tool_events show a
// subtle "working…" affordance, the booking event drops an inline TD-ref receipt,
// done sets the bubble language, errors render inline. Stop is wired to the
// AbortController that cancels generation server-side (AD-E). Native fetch (AD-K).

import { Fragment, useEffect, useRef, useState } from 'react'

import { BookingCards } from './components/BookingCard'
import { BookingReceipt } from './components/BookingReceipt'
import { FeedbackCard } from './components/FeedbackCard'
import { MessageBubble } from './components/MessageBubble'
import { ShowroomCards } from './components/ShowroomCards'
import { SlotChips } from './components/SlotChips'
import { OptionChips } from './components/OptionChips'
import { CompareCard } from './components/CompareCard'
import { VehicleCards } from './components/VehicleCards'
import VehicleDialog from './components/VehicleDialog'
import type { VehicleCard } from './cards'
import { CALL_LABELS, VOICE_LABELS } from './i18n'
import { useCall } from './useCall'
import { useChat } from './useChat'
import { useSpeech } from './useSpeech'
import { MAX_RECORDING_MS, useVoice } from './useVoice'

/**
 * Time-of-day greeting for the empty state, read from the DEVICE'S OWN CLOCK —
 * the greeting is about the person holding the phone, not about the showroom.
 * QR attendees are standing in Dubai anyway, so the two agree in the case that
 * matters; where they differ, the customer's own morning wins.
 * English chrome, like every other string here.
 */
function greeting(now = new Date()): string {
  const hour = now.getHours()
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

/** m:ss for the live recording timer. */
function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

// The call avatar's LIVE ring: a glow that grows with the caller's real mic
// level while Aisha is listening — the "it's hearing you" feedback a still
// screen can't give. Driven by a requestAnimationFrame loop that reads the
// getLevel ref and writes a CSS variable, so the ~380/s audio cadence never
// re-renders React. `active` gates it to the listening state (the other states
// carry their own canned motion); when inactive it parks the level at 0.
function CallPulse({ getLevel, active }: { getLevel: () => number; active: boolean }) {
  const ref = useRef<HTMLSpanElement | null>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (!active) {
      el.style.setProperty('--call-level', '0')
      return
    }
    let raf = 0
    const loop = () => {
      el.style.setProperty('--call-level', getLevel().toFixed(3))
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [active, getLevel])
  return <span ref={ref} className="call-overlay-pulse" aria-hidden="true" />
}

// Aisha's face — a flat, STYLISED illustration (deliberately not photorealistic:
// she is an assistant, not a real person, and an illustration keeps that honest
// while adding warmth). A warm, professional Gulf persona for the UAE brand.
// Inline SVG so it ships with no asset request and scales crisply; a draft to
// tweak (colours, headscarf, features) with brand sign-off.
function AishaAvatar({ className }: { className?: string }) {
  // The brand portrait (public/aisha.png). If it is absent or fails to load,
  // fall through to the inline illustration below — so the UI never shows a
  // broken image, and works before the asset is dropped in.
  const [failed, setFailed] = useState(false)
  if (!failed) {
    return (
      <img
        src="/aisha.png"
        alt="Aisha"
        className={className}
        draggable={false}
        onError={() => setFailed(true)}
      />
    )
  }
  return (
    <svg className={className} viewBox="0 0 96 96" xmlns="http://www.w3.org/2000/svg"
         role="img" aria-label="Aisha">
      <defs>
        <linearGradient id="aiBg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#e9eeff" />
          <stop offset="1" stopColor="#cfe0ff" />
        </linearGradient>
        <clipPath id="aiClip"><circle cx="48" cy="48" r="48" /></clipPath>
      </defs>
      <g clipPath="url(#aiClip)">
        <circle cx="48" cy="48" r="48" fill="url(#aiBg)" />
        {/* headscarf + shoulder drape, one piece */}
        <path d="M19 96 C18 44 29 13 48 13 C67 13 78 44 77 96 Z" fill="#0d9488" />
        <path d="M48 13 C67 13 78 44 77 96 L61 96 C65 60 60 30 48 20 Z" fill="#0b8178" opacity="0.55" />
        {/* neck */}
        <rect x="42" y="52" width="12" height="14" rx="6" fill="#e8b183" />
        {/* face */}
        <ellipse cx="48" cy="43" rx="13.5" ry="15.5" fill="#f1c49d" />
        {/* headscarf edge over the forehead (the hijab line) */}
        <path d="M34 40 C34 28 40 22 48 22 C56 22 62 28 62 40 C57 33 53 31 48 31 C43 31 39 33 34 40 Z" fill="#0f766e" />
        {/* brows */}
        <path d="M40.5 39.5 q3 -1.8 6 0" stroke="#5b4636" strokeWidth="1.4" fill="none" strokeLinecap="round" />
        <path d="M49.5 39.5 q3 -1.8 6 0" stroke="#5b4636" strokeWidth="1.4" fill="none" strokeLinecap="round" />
        {/* eyes */}
        <circle cx="43.5" cy="44" r="1.8" fill="#40332a" />
        <circle cx="52.5" cy="44" r="1.8" fill="#40332a" />
        {/* nose + smile */}
        <path d="M48 46 l0 3" stroke="#d9a878" strokeWidth="1.3" fill="none" strokeLinecap="round" />
        <path d="M44 51.5 q4 3 8 0" stroke="#b26a46" strokeWidth="1.6" fill="none" strokeLinecap="round" />
      </g>
    </svg>
  )
}

// Does this agent message ask the customer where they are? If so we offer a
// one-tap "use my current location" button beneath it — in ANY flow (nearest
// showroom, or a booking asking "whereabouts are you?"). Keyed to phrases that
// only appear in a location QUESTION, never in the answer ("your nearest showroom
// is …"), so it doesn't false-fire on results.
const LOCATION_ASK_RE =
  /whereabouts|where are you|which area|\blandmark\b|your (current )?location|specific spot|pinpoint|pin down|point you to|share (your )?(current )?location/i

function asksForLocation(text: string): boolean {
  return !!text && LOCATION_ASK_RE.test(text)
}

export default function App() {
  const {
    messages,
    streaming,
    working,
    activeSlotsId,
    activeOptionsId,
    send,
    activeShowroomsId,
    selectShowroom,
    selectOption,
    stop,
    status,
    newChat,
    hydrating,
    sendVoice,
    transcribing,
    setRecording,
    setSpeaking,
    sessionId,
    getSessionId,
    callEnded,
    setInCall,
  } = useChat()
  const closed = status === 'closed'
  const lastMsgId = messages[messages.length - 1]?.id
  const [draft, setDraft] = useState('')
  // Recording is a plain capture primitive: it hands back a blob and knows
  // nothing about chat. useChat owns what happens next (upload, transcribe,
  // send), which is why the whole voice feature needed no change to the stream.
  const voice = useVoice(sendVoice)
  const recording = voice.state === 'recording' || voice.state === 'requesting'
  // Mirror the mic state into useChat, which owns the idle ladder. An open mic
  // has to count as presence, or a 60-second voice note started late in a quiet
  // stretch gets nudged mid-sentence and then closed out from under the customer
  // — releasing the slot and discarding the recording on stop.
  useEffect(() => {
    setRecording(recording)
  }, [recording, setRecording])

  // Read-aloud (2026-07-21). Owned here rather than per-bubble so there is ONE
  // voice at a time across the whole thread — two messages talking over each
  // other are unintelligible, and the state has to live above them to enforce it.
  const speech = useSpeech(sessionId)
  // Listening is presence, exactly as an open mic is. A 40-second booking recap
  // read aloud is silence as far as the idle ladder can tell, so without this
  // the customer gets nudged mid-sentence and closed a minute later — the same
  // bug the microphone had.
  useEffect(() => {
    setSpeaking(speech.speakingId != null)
  }, [speech.speakingId, setSpeaking])

  // The voice call (voice phase). AUDIO-FIRST since the overlay decision
  // (2026-07-27): nothing renders into the thread during the call — the
  // overlay owns the screen, the server writes the transcript, and callEnded
  // repaints the thread from it once when the call closes.
  // The GETTER, not the snapshot (2026-07-29): the call resolves the session
  // id at the moment it dials, so it can never connect to a session that was
  // rotated away in the same tick — "New chat" always gives the next call a
  // fresh session, structurally.
  const call = useCall(getSessionId, { onCallEnd: callEnded })
  // Mirror the call into the idle ladder, exactly as the mic and the speaker
  // are mirrored: a customer on a call is present the whole time.
  useEffect(() => {
    setInCall(call.active)
  }, [call.active, setInCall])

  // A chat that has ended must not keep talking. `closed` arrives from the
  // agent's own sign-off or the idle close, both of which can land while a
  // long message is still being read — or while a call is live (the agent's
  // sign-off ends the call server-side; this is the client's half).
  useEffect(() => {
    if (closed) {
      speech.stop()
      call.hangup()
    }
  }, [closed, speech, call])

  // "New chat" must SILENCE THE ROOM before it rotates the session.
  //
  // useChat cannot do this itself: the microphone lives in useVoice and the
  // voice in useSpeech, and neither is visible from there. So the wrapper lives
  // here, where all three are in scope, and every New chat button calls THIS
  // rather than newChat directly.
  //
  // Two things would otherwise outlive the conversation they belong to:
  //   SPEECH — audio already fetched keeps playing, so the new, empty thread
  //   sits there while Aisha reads a message the customer can no longer see.
  //   THE MICROPHONE — worse than untidy. An open mic survives the rotation and
  //   its recording lands in the NEW session on stop: a voice note answering a
  //   question nobody in this conversation asked, with the previous customer's
  //   words in it. On a shared demo phone that is the wrong person's audio in
  //   the wrong person's chat.
  // cancel(), not stop(), for exactly that reason — the recording is discarded,
  // never uploaded.
  const startNewChat = () => {
    speech.stop()
    voice.cancel()
    // A live call must not outlive its conversation, for the same reason as
    // the open microphone above — its next utterance would land in the NEW
    // session, in the previous customer's voice.
    call.hangup()
    newChat()
  }

  // Starting a call silences the room the same way "New chat" does: one voice
  // at a time (read-aloud off), one microphone owner (any half-made voice note
  // is discarded, never sent).
  const startCall = () => {
    if (streaming || transcribing) return
    speech.stop()
    voice.cancel()
    // A call is a FIRST MESSAGE (rule 19): on a closed conversation the call
    // button rotates to a fresh session and dials that — a silently dead
    // button was the old behaviour, and dead buttons read as broken. Works in
    // one tick because the call resolves its session id at dial time.
    if (closed) newChat()
    call.start()
  }
  // Chrome is English for now, like every other label in this shell — but the
  // Arabic strings ship already, so switching this to the conversation's own
  // language in the persona phase is a one-line change rather than a translation
  // pass (LOC-01).
  const voiceLabels = VOICE_LABELS.en
  // Call chrome is English like the rest of the shell; the Arabic strings ship
  // in CALL_LABELS already, same one-line switch as everything else (LOC-01).
  const callLabels = CALL_LABELS.en
  // The car whose dialog is open, or null. Holding the whole card (not just an
  // id) keeps the language decision local: `dir` came from the bubble that
  // rendered the cards, so the dialog opens in the conversation's language
  // without asking useChat for it.
  // THE WHOLE ROW, NOT ONE CARD. Choosing between cars means looking at each in
  // turn, and the panel used to hold a single vehicle — so comparing three meant
  // opening and closing three times and losing your place on every hop. Keeping
  // the row plus an index lets the panel step along it in place.
  const [detailCard, setDetailCard] = useState<{
    cards: VehicleCard[]
    index: number
    dir: 'rtl' | 'ltr'
  } | null>(
    null,
  )
  const listRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  // Keep the newest bubble in view as tokens stream in.
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, working])

  // Empty-state cards PREFILL the composer — they never send. The customer
  // reads/edits the starter and taps send themselves, so nothing is committed
  // on their behalf and the input stays their words. Focus + caret-to-end so
  // they can keep typing straight away.
  const prefill = (text: string) => {
    setDraft(text)
    const el = inputRef.current
    if (el) {
      el.focus()
      const n = text.length
      requestAnimationFrame(() => el.setSelectionRange(n, n))
    }
  }

  const submit = () => {
    if (!draft.trim() || streaming || closed) return
    send(draft)
    setDraft('')
  }

  // Post-chat feedback on the agent: rating + optional comment -> /api/feedback.
  // The booking reference (if this chat booked) lets the backend attach the
  // client's name & mobile to the Salesforce feedback record.
  const submitFeedback = async (stars: number, comment: string) => {
    // The booking made in THIS chat can be either a 'booking' row (carries ref)
    // or a 'bookings' row (a freshly-created booking renders as a one-card list),
    // so check both — newest first — to capture its reference for Salesforce.
    let bookingRef: string | null = null
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.kind === 'booking' && m.ref) {
        bookingRef = m.ref
        break
      }
      if (m.kind === 'bookings' && m.bookings.length > 0) {
        bookingRef = m.bookings[m.bookings.length - 1].ref
        break
      }
    }
    const res = await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: getSessionId(),
        stars,
        comment: comment || null,
        booking_ref: bookingRef,
      }),
    })
    if (!res.ok) throw new Error('feedback failed')
  }

  // "Share my location" — one tap gets the device GPS fix and hands it to Aisha,
  // who finds the nearest showroom (nearest_showroom tool). If the browser has no
  // geolocation, or the customer denies/times out, we fall back to the same
  // question WITHOUT coordinates, so Aisha simply asks for a nearby landmark.
  const shareLocation = () => {
    if (streaming || closed || transcribing) return
    const ask = 'Which showroom is nearest to me?'
    if (!('geolocation' in navigator)) {
      send(ask)
      return
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const { latitude, longitude } = pos.coords
        send(
          `\u{1F4CD} My current location (GPS): ${latitude.toFixed(6)}, ${longitude.toFixed(6)} — ` +
            'which showroom is nearest, and how do I get there?',
        )
      },
      () => send(ask),
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 },
    )
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="chat">
      <header className="chat-header">
        <div className="brand">
          {/* Aisha's mark — the persona gets a face, with a live status dot. "A"
              is decorative (the h1 beside it names her), so the avatar is hidden
              from assistive tech. */}
          <span className="brand-avatar" aria-hidden="true">
            <AishaAvatar className="brand-face" />
            <span className="brand-online" />
          </span>
          <div className="brand-text">
            <h1 className="brand-title">Aisha</h1>
            <p className="brand-sub">Elite Group</p>
          </div>
        </div>
        {/* Always visible, not only once a chat has ended: the conversation now
            survives a reload, so a shared device (or a phone handed to the next
            attendee) needs a permanent way to start clean. English chrome for
            now, like every other string here — flagged for the persona phase. */}
        <button className="btn btn--ghost btn--newchat" onClick={startNewChat} type="button">
          <svg viewBox="0 0 14 14" width="12" height="12" aria-hidden="true">
            <path d="M7 1v12M1 7h12" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
          </svg>
          New chat
        </button>
      </header>

      <div className="chat-thread" ref={listRef}>
        {/* Empty state: a warm greeting (rule 14 tone; English chrome like
            every other UI string) over two starter cards. The cards PREFILL
            the composer — they do NOT send (a tap chooses a path, the customer
            still confirms in their own words). "New test drive" opens booking;
            "My booking" opens the reschedule/cancel flow, where Aisha then
            asks for name or TD-ref (rules 10/11). */}
        {messages.length === 0 && !hydrating && (
          <div className="empty">
            <span className="empty-avatar" aria-hidden="true">
              <AishaAvatar className="empty-avatar-face" />
            </span>
            {/* Time-of-day, not a fixed "Marhaba" — the same screen greets a
                7pm QR attendee and a 9am rehearsal correctly. Computed per
                render off the Dubai clock; there is deliberately no ticker,
                since the empty state disappears the moment anyone speaks. */}
            <h2 className="empty-hello">{greeting()}.<br />I’m Aisha.</h2>
            <p className="empty-sub">Ask me about any Jetour model, or book a test drive in under a minute.</p>
            <div className="starter-list">
              <button
                type="button"
                className="starter-btn starter-btn--primary"
                onClick={() => prefill('I’d like to book a test drive.')}
              >
                <span>New test drive</span>
                <svg className="starter-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
              </button>
              <button
                type="button"
                className="starter-btn starter-btn--secondary"
                onClick={() => prefill('I’d like to reschedule or cancel my booking.')}
              >
                <span>My booking</span>
                <svg className="starter-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
              </button>
            </div>
          </div>
        )}
        {messages.map((m) =>
          m.kind === 'cards' ? (
            <VehicleCards
              key={m.id}
              cards={m.cards}
              dir={m.dir}
              variant={m.variant}
              // BROWSE, not commit: "View more" opens the detail dialog, which
              // fetches /api/vehicles/{id} directly — no chat turn, no model, no
              // tokens. It must NOT settle or disable the other cards: the
              // customer browses T2 -> X70 -> back freely. Only `streaming`
              // gates it (don't tap mid-response), never a prior tap. Contrast
              // slot chips below (commit). Never merge the two behaviours.
              onViewMore={(card) =>
                setDetailCard({
                  cards: m.cards,
                  index: Math.max(0, m.cards.findIndex((c) => c.id === card.id)),
                  dir: m.dir,
                })
              }
              // A closed chat sends nothing (rule 19) — the cards stay readable
              // in the transcript, but they are no longer a way back in.
              disabled={streaming || closed}
            />
          ) : m.kind === 'booking' ? (
            <BookingReceipt key={m.id} bookingRef={m.ref} dir={m.dir} />
          ) : m.kind === 'bookings' ? (
            // READ-ONLY (see BookingCard's header): no `disabled` prop, because
            // nothing on this card commits — both links leave the page. A closed
            // chat therefore leaves it fully usable, unlike the cards and chips
            // above, which are ways back into a conversation that has ended.
            <BookingCards
              key={m.id}
              bookings={m.bookings}
              dir={m.dir}
              // Overtaken by a reschedule, or cancelled: still readable, but
              // muted and stripped of its calendar link.
              superseded={m.superseded}
            />
          ) : m.kind === 'showrooms' ? (
            <ShowroomCards
              key={m.id}
              showrooms={m.showrooms}
              dir={m.dir}
              variant={m.variant}
              // COMMIT, like the slot chips below and unlike the car cards
              // above: a tap sends "I'd like to visit Jetour Deira" as a normal
              // turn and settles this row. Only the newest offer stays live, so
              // a branch cannot be picked out of a conversation that has moved on.
              selectedShowroomId={m.selectedShowroomId}
              disabled={streaming || closed || m.id !== activeShowroomsId}
              onSelect={(s) => selectShowroom(s, m.id)}
            />
          ) : m.kind === 'compare' ? (
            <CompareCard key={m.id} data={m.data} dir={m.dir} />
          ) : m.kind === 'options' ? (
            <OptionChips
              key={m.id}
              options={m.options}
              dir={m.dir}
              // COMMIT, like the slot chips below: a tap sends the label as a
              // normal turn and settles the row. Only the newest question stays
              // live, so a "Yes" from three turns ago can never be sent into a
              // conversation that has moved on.
              selectedLabel={m.selectedLabel}
              disabled={streaming || closed || m.id !== activeOptionsId}
              onPick={(label) => selectOption(label, m.id)}
            />
          ) : m.kind === 'slots' ? (
            <SlotChips
              key={m.id}
              slots={m.slots}
              dir={m.dir}
              // COMMIT, not browse: a tap SENDS A CHOICE. It goes out as a clean
              // prose bubble + the hidden slot_id (backend pins lock_slot), and it
              // SETTLES the row — the chosen chip shows selected, the rest die
              // (handled in `send`, keyed on the row id passed here). Contrast car
              // cards above (browse). Never merge the two behaviours.
              onPick={(text, slotId) => send(text, slotId, m.id)}
              // The picked chip, so this row renders one selected + the rest dead,
              // not all-dead-identical.
              selectedSlotId={m.selectedSlotId}
              // Only the newest offer is tappable (Fix 4): a superseded slot row
              // stays visible but inert, so a stale chip can't pin an old slot_id.
              disabled={streaming || closed || m.id !== activeSlotsId}
            />
          ) : (
            <Fragment key={m.id}>
              <MessageBubble
                message={m}
              speaking={speech.speakingId === m.id}
              loading={speech.loadingId === m.id}
              // Offered ONLY on Aisha's finished messages, and only when the
              // device can actually speak that language. Not on the customer's
              // own turns (they know what they said), not on system lines (we
              // wrote those, not her), and not mid-stream — a half-written
              // sentence read aloud is worse than waiting a second for it.
              onSpeak={
                m.role === 'agent' &&
                !m.streaming &&
                m.text.trim() &&
                speech.canSpeak(m.lang, m.turnId)
                  ? () => speech.speak(m.id, m.text, m.lang, m.turnId)
                  : undefined
              }
              />
              {m.role === 'agent' &&
                !m.streaming &&
                m.id === lastMsgId &&
                asksForLocation(m.text) && (
                  <div className={`shrs shrs--${m.lang === 'ar' ? 'rtl' : 'ltr'}`}>
                    <button
                      type="button"
                      className="useloc-btn"
                      disabled={streaming || closed}
                      onClick={shareLocation}
                    >
                      {'\u{1F4CD}'}{' '}
                      {m.lang === 'ar' ? 'استخدم موقعي الحالي' : 'Use my current location'}
                    </button>
                  </div>
                )}
            </Fragment>
          ),
        )}
        {working && (
          <div className="row row--agent">
            <div className="working" aria-live="polite">
              <span className="working-dots"><i /><i /><i /></span>
              <span className="working-label">{working}</span>
            </div>
          </div>
        )}
        {/* Aligned to the USER's side, unlike the agent's "working…" above: this
            is the customer's own message being processed, and it stands where
            their bubble is about to appear. */}
        {transcribing && (
          <div className="row row--user">
            <div className="working" aria-live="polite">
              <span className="working-dots"><i /><i /><i /></span>
              <span className="working-label">{voiceLabels.transcribing}</span>
            </div>
          </div>
        )}
      </div>

      {/* One docked panel holds the composer AND its disclaimer, so the caveat
          shares the composer's surface/border instead of floating below it. */}
      {/* THE CALL OVERLAY (2026-07-27): a call is a mode, not a widget. While
          it is live the whole screen is the call — translucent glass over the
          chat, audio-first, no live captions (the thread repaints from the
          server's transcript when the call ends). Controls mirror a phone:
          mute on the left, hang up on the right. */}
      {call.active && (
        <div className="call-overlay" role="dialog" aria-label="Voice call">
          <div className="call-overlay-card">
            <span className="call-overlay-avatar" aria-hidden="true">
              {/* Behind the avatar: the mic-reactive glow (listening only) and
                  the state ring (colour + motion per state). */}
              <CallPulse getLevel={call.getLevel} active={call.state === 'listening' && !call.muted} />
              <span className={`call-overlay-ring call-overlay-ring--${call.state}`} />
              <AishaAvatar className="call-overlay-face" />
            </span>
            <h2 className="call-overlay-name">Aisha</h2>
            {/* Only states the caller must ACT on are worded (muted, connecting);
                the listening/thinking/speaking narration was dropped 2026-08-03
                (product decision) — a phone call doesn't caption itself. The
                empty line keeps the layout from jumping. */}
            <p className="call-overlay-state" aria-live="polite">
              {call.muted
                ? callLabels.muted
                : call.state === 'connecting'
                  ? callLabels.connecting
                  : ' '}
            </p>
            <p className="call-overlay-elapsed">{formatElapsed(call.elapsedMs)}</p>
            <div className="call-overlay-controls">
              <button
                className={`call-overlay-btn call-overlay-btn--mute${call.muted ? ' is-muted' : ''}`}
                onClick={call.toggleMute}
                type="button"
                aria-label={call.muted ? callLabels.unmute : callLabels.mute}
                title={call.muted ? callLabels.unmute : callLabels.mute}
                aria-pressed={call.muted}
              >
                <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none">
                  <path
                    d="M12 15.5a3.5 3.5 0 0 0 3.5-3.5V6a3.5 3.5 0 1 0-7 0v6a3.5 3.5 0 0 0 3.5 3.5Z"
                    stroke="currentColor"
                    strokeWidth="1.7"
                  />
                  <path
                    d="M19 11.5a7 7 0 0 1-14 0M12 18.5V22"
                    stroke="currentColor"
                    strokeWidth="1.7"
                    strokeLinecap="round"
                  />
                  {call.muted && (
                    <path d="M4 3 20 21" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
                  )}
                </svg>
              </button>
              <button
                className="call-overlay-btn call-overlay-btn--hangup"
                onClick={call.hangup}
                type="button"
                aria-label={callLabels.hangUp}
                title={callLabels.hangUp}
              >
                <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" fill="none">
                  <path
                    d="M3.5 14.5c-.8-.8-.7-2.2.3-3.1a11.8 11.8 0 0 1 16.4 0c1 .9 1.1 2.3.3 3.1l-1.6 1.6c-.6.6-1.6.7-2.4.2l-2-1.4a1.9 1.9 0 0 1-.8-1.8l.1-1a9 9 0 0 0-3.6 0l.1 1c.1.7-.2 1.4-.8 1.8l-2 1.4c-.8.5-1.8.4-2.4-.2l-1.6-1.6Z"
                    fill="currentColor"
                  />
                </svg>
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="composer-dock">
      {/* Rule 19: a closed chat is VISUALLY ENDED. The composer is replaced
          outright rather than merely disabled — a greyed-out textarea still
          invites a click, and on stage the audience should be able to see at a
          glance that the conversation finished cleanly rather than broke.
          English chrome for now (like every other string here); the agent's own
          farewell above it is bilingual, which is the part that matters. */}
      {closed ? (
        <>
          <FeedbackCard onSubmit={submitFeedback} />
          <div className="composer composer--closed">
            <p className="composer-ended">This chat has ended.</p>
            <button className="btn btn--send" onClick={startNewChat} type="button">
              Start new chat
            </button>
          </div>
        </>
      ) : recording ? (
        /* RECORDING REPLACES THE COMPOSER, rather than sitting beside it. While
           the mic is open there is exactly one thing to decide — keep it or bin
           it — and a live textarea next to a running timer invites typing into a
           message that is being spoken. Cancel is on the LEFT, away from the
           thumb's resting position, so the destructive action is the harder tap. */
        <div className="composer composer--recording">
          {/* ICONS, NOT WORDS. Two controls flanking a running timer read at a
              glance and stay legible on a narrow phone, where "Cancel recording"
              and "Stop recording" wrap and start competing with each other. The
              labels survive as aria-label and title — this is a visual change,
              not an accessibility one, and a screen reader still hears the full
              phrase in the conversation's language. */}
          <button
            className="voice-icon voice-icon--cancel"
            onClick={voice.cancel}
            type="button"
            aria-label={voiceLabels.cancel}
            title={voiceLabels.cancel}
          >
            <svg viewBox="0 0 16 16" width="17" height="17" aria-hidden="true">
              <path
                d="M6.5 1h3a1 1 0 0 1 1 1v1H13a.5.5 0 0 1 0 1h-.62l-.6 9.07A2 2 0 0 1 9.79 15H6.21a2 2 0 0 1-2-1.93L3.62 4H3a.5.5 0 0 1 0-1h2.5V2a1 1 0 0 1 1-1zm0 2h3V2h-3v1z"
                fill="currentColor"
              />
              <path
                d="M6.8 6v6.2M9.2 6v6.2"
                stroke="currentColor"
                strokeWidth="1"
                strokeLinecap="round"
                fill="none"
              />
            </svg>
          </button>

          <div className="voice-live" aria-live="polite">
            <span className="voice-dot" aria-hidden="true" />
            <span className="voice-elapsed">{formatElapsed(voice.elapsedMs)}</span>
            <span className="voice-remaining">
              / {formatElapsed(MAX_RECORDING_MS)}
            </span>
          </div>

          {/* AN UP ARROW, NOT A RIGHT ARROW. WhatsApp sends with a right-pointing
              arrow, but "forward" is not a fixed direction here: this app renders
              RTL per bubble (LOC-01), and a right arrow would point BACKWARDS in
              an Arabic conversation unless it were mirrored. Up means send in
              either direction and needs no RTL rule at all. */}
          <button
            className="voice-icon voice-icon--stop"
            onClick={voice.stop}
            type="button"
            aria-label={voiceLabels.stop}
            title={voiceLabels.stop}
          >
            <svg viewBox="0 0 16 16" width="17" height="17" aria-hidden="true">
              <path
                d="M8 13.5V3.2M8 2.5 3.6 6.9M8 2.5l4.4 4.4"
                stroke="currentColor"
                strokeWidth="1.9"
                strokeLinecap="round"
                strokeLinejoin="round"
                fill="none"
              />
            </svg>
          </button>
        </div>
      ) : (
        <div className="composer">
          {/* ATTACH — PRESENT AND PERMANENTLY DISABLED (product decision
              2026-07-25). There is no attachment path in this system at all: no
              upload endpoint, no storage, no retention or rule-12 delete route
              for a file, and no way for the agent to receive one (it reads text
              and tool results; /api/voice/transcribe takes audio and nothing
              else). So this cannot be wired up as a UI change — it is a feature.

              It is rendered anyway, at the developer's explicit request, and the
              ONLY honest way to render it is disabled: greyed, not focusable,
              and saying so on hover. It must never open a file picker, because a
              picker that accepts a file and then discards it is a failure the
              customer performs on themselves in front of an audience.

              `disabled` also keeps it out of the tab order, so keyboard users
              are not walked onto a dead control on the way to the input. */}
          <button
            className="btn btn--attach"
            type="button"
            disabled
            aria-label="Attachments are not available"
            title="Attachments are not available"
          >
            <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none">
              <path
                d="M20 11.5 12.2 19.3a4.6 4.6 0 0 1-6.5-6.5l7.8-7.8a3 3 0 1 1 4.3 4.3l-7.8 7.8a1.5 1.5 0 0 1-2.1-2.1l7.1-7.1"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
          {/* MIC ON THE LEADING EDGE, and ALWAYS VISIBLE while it can work
              (2026-07-25). This reverses the 2026-07-23 decision to hide it once
              the field had text — deliberately, because that decision's stated
              reason was that the mic "crowds the control they actually want" by
              sitting next to Send on a narrow phone. It no longer sits next to
              Send; it sits at the far end of the row. Hiding it there would buy
              nothing and cost a layout jump, because the input would widen and
              slide left on the first keystroke.

              Still hidden outright when recording can never work here (no
              MediaRecorder, or an insecure origin — see useVoice), because a
              button that cannot function is worse than no button on stage. And
              still hidden while streaming, when it would race the reply. */}
          {!streaming && voice.supported && (
            <button
              className="btn btn--mic"
              onClick={voice.start}
              type="button"
              disabled={transcribing || voice.state === 'requesting'}
              aria-label={voiceLabels.record}
              title={voiceLabels.record}
            >
              <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none">
                <path
                  d="M12 15.5a3.5 3.5 0 0 0 3.5-3.5V6a3.5 3.5 0 1 0-7 0v6a3.5 3.5 0 0 0 3.5 3.5Z"
                  stroke="currentColor"
                  strokeWidth="1.7"
                />
                <path
                  d="M19 11.5a7 7 0 0 1-14 0M12 18.5V22"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          )}
          {/* CALL — beside the mic: same secondary-control placement, same
              hide-when-it-cannot-work rule (no Web Audio, insecure origin; see
              useCall.isSupported). The backend gate (CALL_ENABLED) answers on
              connect with a stated reason rather than a dead line. */}
          {!streaming && call.supported && (
            <button
              className="btn btn--mic btn--call"
              onClick={startCall}
              type="button"
              disabled={transcribing || recording}
              aria-label={callLabels.call}
              title={callLabels.call}
            >
              <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none">
                <path
                  d="M6.6 3.8 8.4 3a1.8 1.8 0 0 1 2.3.8l1.1 2.2c.4.7.2 1.6-.4 2.1l-1.2 1a12.7 12.7 0 0 0 4.7 4.7l1-1.2c.5-.6 1.4-.8 2.1-.4l2.2 1.1c.8.4 1.1 1.4.8 2.3l-.8 1.8c-.4.9-1.3 1.5-2.3 1.4C10.9 18.9 5.1 13.1 4.2 6.1c-.1-1 .5-1.9 1.4-2.3Z"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
          {/* The composer location pin was removed in favour of a contextual
              "Use my current location" button that appears exactly when Aisha
              asks where the customer is (see the 'askloc' row above). */}
          <textarea
            ref={inputRef}
            className="composer-input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Write a message…"
            rows={1}
            dir="auto"
            disabled={transcribing}
          />
          {streaming ? (
            /* Stop shares the round send slot, so it takes the round icon shape
               (a stop square) rather than staying a text pill that would clash
               beside the new circular Send. Same behaviour — aborts generation. */
            <button
              className="btn btn--stop-icon"
              onClick={stop}
              type="button"
              aria-label="Stop"
              title="Stop"
            >
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                <rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor" />
              </svg>
            </button>
          ) : (
            /* Always rendered, disabled until there is something to send.
               `transcribing` is part of that: a recording already on its way must
               not be raced by a typed turn.

               A PAPER PLANE, replacing the up arrow (2026-07-25). The arrow was
               chosen because "up" reads as send in both scripts where a
               right-pointing glyph would point backwards in Arabic — that
               concern does not reach here, because the composer chrome is
               LTR-only (its placeholder and every label around it are English)
               and is not mirrored by the conversation's language. The VOICE-NOTE
               send still uses the arrow, and should: it sits inside the
               recording strip, not in this fixed row. */
            <button
              className="btn btn--send-icon"
              onClick={submit}
              type="button"
              disabled={!draft.trim() || transcribing}
              aria-label="Send"
              title="Send"
            >
              {/* The plane is FILLED, with its fold drawn in the button's own
                  background colour rather than a translucent white — on a solid
                  disc a semi-transparent seam muddies to grey. The fold reads as
                  a cut in the shape because it literally is the disc showing
                  through. `var()` works in an inline SVG paint attribute, so the
                  seam can never drift from the fill it sits on. */}
              <svg viewBox="0 0 24 24" width="21" height="21" aria-hidden="true">
                <path
                  d="M21.6 2.4 2.9 9.6c-.9.3-.9 1.6.02 1.9l7 2.2 2.2 7c.3.9 1.6.9 1.9.02L21.6 2.4Z"
                  fill="currentColor"
                />
                <path
                  d="M21.6 2.4 9.9 13.7"
                  stroke="var(--color-send)"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  fill="none"
                />
              </svg>
            </button>
          )}
        </div>
      )}

      {/* Why the mic is unavailable, stated plainly. `unsupported` is almost
          always the HTTPS blocker (getUserMedia is blocked on plain HTTP outside
          localhost), and a silent missing button would read on stage as a bug
          rather than as a deployment fact. Muted system styling, not Aisha's
          voice — she did not say this, we did. */}

      {/* Small, muted disclaimer INSIDE the composer dock (same container as the
          composer above). English chrome like every other UI string here; the
          conversation itself stays bilingual. Not a persona AI-disclosure (that
          was overridden 2026-07-20) — it is the ordinary "check what the
          assistant tells you" footer, so it lives in the UI shell, not in
          Aisha's voice. Hidden on a closed chat, where there is no live composer
          to caveat. */}
      {!closed && (
        <p className="composer-disclaimer">AI can make mistakes. Please double-check important details.</p>
      )}
      </div>

      {/* Detail dialog (rule 18). Browsing costs nothing: it fetches
          /api/vehicles/{id} directly, so Aisha stays silent while it is open.
          "Book test drive" is the one COMMIT here — it closes the dialog and
          sends a normal turn, because starting a booking is a conversation.
          Phrased in the customer's language so the transcript stays coherent in
          an Arabic chat (LOC-01). */}
      {detailCard?.cards[detailCard.index]?.vehicleId != null && (
        <VehicleDialog
          key={detailCard.cards[detailCard.index].id}
          vehicleId={detailCard.cards[detailCard.index].vehicleId!}
          lang={detailCard.dir === 'rtl' ? 'ar' : 'en'}
          siblings={detailCard.cards}
          index={detailCard.index}
          onIndex={(i) => setDetailCard({ ...detailCard, index: i })}
          onClose={() => setDetailCard(null)}
          onBook={(vehicle) => {
            setDetailCard(null)
            send(
              detailCard.dir === 'rtl'
                ? `أرغب في حجز تجربة قيادة لسيارة ${vehicle.name}`
                : `I'd like to book a test drive for the ${vehicle.name}`,
            )
          }}
        />
      )}
    </div>
  )
}
