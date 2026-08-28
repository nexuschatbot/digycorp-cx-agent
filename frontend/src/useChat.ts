// Conversation state for the chat widget (transport rendering only — no prompt
// or business logic; the agent still speaks the minimal Stage-1 prompt).

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  audioUrl,
  closeConversation,
  lockSlotIntent,
  fetchBooking,
  fetchConversation,
  fetchVehicle,
  redeemHandoff,
  streamChat,
  transcribeVoice,
  type HandoffResult,
  type Lang,
  type StreamHandlers,
  type TranscriptTurn,
} from './api'
import { extractBookingCards, toBookingCard, type BookingCard } from './bookings'
import { cardFromDetail, extractCards, extractSearchCards, type VehicleCard } from './cards'
import type { CardVariant } from './components/VehicleCards'
import { CALL_END_LINES, LIFECYCLE_LINES, VOICE_LABELS, callSummaryLine } from './i18n'
import { isRtl } from './rtl'
import {
  extractNearestShowroomChoices,
  extractShowroomChoices,
  type ShowroomChoice,
} from './showrooms'
import { extractSlotChips, type SlotChip } from './slots'
import { extractOptionChips, type OptionChip } from './options'
import { extractComparison, type Comparison } from './compare'

export type Role = 'user' | 'agent' | 'error' | 'system'

// Rule 19: a conversation is live, or it has ended. Frontend-owned state — the
// backend runs NO server-side timer and holds no session status; it acts only on
// inbound messages. (The close ITSELF is audited server-side via the
// close_conversation tool; this flag is just how the UI reflects it.)
export type ConversationStatus = 'active' | 'closed'

// The two ways a chat ends (rule 19), and the ONLY argument to closeChat:
//   'signoff' — the agent said goodbye and the backend sent `closed`. Its
//               farewell is already the last bubble, so we add NOTHING, and the
//               backend has already closed the session itself.
//   'idle'    — 10 minutes of silence, one minute after an unanswered nudge. We
//               add the canned farewell (nobody is there to say goodbye) AND
//               close the session server-side.
export type CloseReason = 'signoff' | 'idle'


// The idle ladder (rule 19b), both measured from the SAME anchor (lastActivityAt),
// never chained off each other: at 9 minutes we ask whether the customer is still
// there, and if that goes unanswered the chat ends at 10 minutes. Deriving both
// from one anchor is what keeps the nudge from postponing the close — see the
// timer effect. (Was 2/3 minutes; widened to 10 by product decision 2026-07-27,
// keeping the one-minute warning gap between nudge and close.)
const IDLE_NUDGE_MS = 9 * 60 * 1000
const IDLE_CLOSE_MS = 10 * 60 * 1000

// The language of the conversation so far, for the two canned lifecycle lines:
// the last thing ACTUALLY SAID by either party, walking backwards. Cards, chips
// and receipts are skipped (they carry layout direction, not speech), as are
// earlier system lines (they would just echo their own last guess).
//
// Falls back to 'en' only for a thread with no speech in it at all, which the
// `messages.length === 0` guard already prevents from reaching a timer.
function conversationLang(messages: ChatMessage[]): Lang {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.kind === 'text' && (m.role === 'user' || m.role === 'agent')) return m.lang
  }
  return 'en'
}

// --------------------------------------------------------------------------- //
// Session persistence — survive a page reload
// --------------------------------------------------------------------------- //
// sessionStorage, PER TAB (switched from localStorage 2026-07-27). Rotated by
// "New chat" and by the zero-turns guard below.
//
// WHY IT MOVED. localStorage is per-origin-per-BROWSER, so two tabs open on the
// same machine read the same id and post it to /api/chat — and the agent
// checkpointer keys on thread_id = session_id, so both tabs fed ONE agent memory
// while showing two separate threads on screen. Messages interleaved into a
// conversation neither tab displayed. Worse, "New chat" in one tab rotated the
// stored id while the other kept the old one in React state, so the two silently
// drifted apart with nothing on screen to say so.
//
// WHAT THIS COSTS, AND IT IS NOT NOTHING. sessionStorage survives a RELOAD (it is
// scoped to the tab, not the page), so the resume-after-refresh case still works.
// It does NOT survive the tab being CLOSED. An attendee who swipes the tab away
// mid-booking and re-scans the QR now lands in an empty chat instead of resuming
// — the exact case localStorage was originally chosen for. Accepted deliberately:
// a shared agent memory produces WRONG answers, while a lost session produces a
// fresh start, and wrong beats missing in front of an audience.
//
// STILL PER-DEVICE, NOT PER-PERSON, for a phone handed around within one tab:
// which is why "New chat" lives permanently in the header rather than appearing
// only once a chat has closed.
//
// The version that keeps BOTH properties is a BroadcastChannel liveness check —
// adopt a stored id only if no live tab answers for it — which is the right shape
// but is async machinery in the path every conversation starts through. Deferred
// past the demo on purpose.
const SESSION_KEY = 'elite.session_id'

function readStoredSession(): { id: string; existing: boolean } {
  try {
    // Sweep the key the old localStorage build left behind. Nothing reads it any
    // more, but a session id is a pointer to a conversation transcript, and left
    // in PERSISTENT storage it can never be rotated by "New chat" or by the
    // zero-turns guard — so on a demo phone it would outlive every session it
    // ever named. Cheap, idempotent, and inside the same try: a browser that
    // refuses storage falls through to the in-memory session below regardless.
    localStorage.removeItem(SESSION_KEY)

    const stored = sessionStorage.getItem(SESSION_KEY)
    if (stored) return { id: stored, existing: true }
    const id = crypto.randomUUID()
    sessionStorage.setItem(SESSION_KEY, id)
    return { id, existing: false }
  } catch {
    // Private mode / storage disabled / blocked by policy. Degrade to an
    // in-memory session: the chat works perfectly, it just will not survive a
    // reload. Never let storage take the demo down.
    return { id: crypto.randomUUID(), existing: false }
  }
}

function storeSession(id: string): void {
  try {
    sessionStorage.setItem(SESSION_KEY, id)
  } catch {
    /* see readStoredSession — storage is a nice-to-have, never a dependency */
  }
}

// A WhatsApp handoff link is `.../?h=<token>`. Read the token ONCE per page load
// and immediately strip it from the address bar (history.replaceState, no
// reload): the token is single-use and identity-bearing, so it must not linger in
// the URL to be bookmarked, shared, or re-triggered by a refresh. Returns null
// when absent. Cached MODULE-side because the redeem is single-use — under React
// StrictMode's dev mount/unmount/mount the effect runs twice, and re-reading the
// (now stripped) URL the second time would lose the token.
let _handoffChecked = false
let _handoffToken: string | null = null
function takeHandoffToken(): string | null {
  if (_handoffChecked) return _handoffToken
  _handoffChecked = true
  try {
    const url = new URL(window.location.href)
    _handoffToken = url.searchParams.get('h')
    if (_handoffToken) {
      url.searchParams.delete('h')
      window.history.replaceState({}, '', url.toString())
    }
  } catch {
    _handoffToken = null
  }
  return _handoffToken
}

// The redeem, cached MODULE-side so the single-use token is exchanged exactly
// ONCE per page load: StrictMode's double effect-invoke (and the effect re-run
// when `send`'s identity changes) both await this same promise instead of firing
// a second redeem that would come back already_redeemed.
let _handoffRedeem: Promise<HandoffResult> | null = null
function redeemHandoffOnce(token: string): Promise<HandoffResult> {
  if (!_handoffRedeem) _handoffRedeem = redeemHandoff(token)
  return _handoffRedeem
}

// A stored transcript turn -> a thread bubble. Cards ride alongside it (see
// rebuildTurns); SLOT CHIPS are still never replayed — a rehydrated chip pins a
// slot_id that may since have been booked, and they regenerate on the next
// relevant turn.
function turnToMessage(t: TranscriptTurn, sessionId: string): TextMessage {
  return {
    id: crypto.randomUUID(),
    kind: 'text',
    role: t.speaker === 'customer' ? 'user' : 'agent',
    text: t.text,
    streaming: false,
    lang: t.lang,
    // The voice note this turn was spoken as (2026-07-21) — resolved to a URL
    // exactly like the live path, so a reloaded spoken turn is indistinguishable
    // from the one the customer left on screen.
    //
    // NO durationMs HERE, and that is not an omission: the recorded length was a
    // fact of the live session, and the transcript stores a POINTER rather than
    // a copy of it. VoicePlayer reads the real duration from the audio's own
    // metadata on first play, which cannot go stale the way a stored number
    // could. Same reasoning that keeps names and prices out of `cards`.
    //
    // If the recording has since expired (48h) or been erased by "forget me",
    // this URL 404s and the player removes itself, leaving the transcript text —
    // the same "drop it, never render a shell" rule as the cards (FR-CX-07).
    ...(t.audio_id ? { audioSrc: audioUrl(t.audio_id, sessionId) } : {}),
    ...(t.turn_id != null ? { turnId: t.turn_id } : {}),
  }
}

// A stored transcript -> the thread as the customer left it: each turn's bubble,
// followed by whatever vehicle cards that turn rendered (rule 18).
//
// THE IDS ARE RESOLVED, NOT RESTORED. The transcript stores vehicle ids; the real
// card data is re-read from GET /api/vehicles/{id} — the endpoint "View more"
// already uses, so a reloaded card carries live catalogue data rather than a
// snapshot that could have gone stale.
//
// ONE FETCH PER DISTINCT CAR, not per card row: a thread that discussed the T2
// four times resolves it once. All of them go out together and the whole rebuild
// awaits them, so the thread appears complete rather than growing cars in front
// of the customer.
//
// A CAR THAT NO LONGER RESOLVES IS DROPPED, NOT FAKED (FR-CX-07). fetchVehicle
// returns null on a 404 or a network failure; that card is simply absent. The
// text bubble above it still stands on its own, which is the honest degradation —
// far better than a shell card with a blank name.
async function rebuildTurns(turns: TranscriptTurn[], sessionId: string): Promise<ChatMessage[]> {
  const anchors = turns.flatMap((t) => t.cards ?? [])
  // `?? []` on BOTH arms: an anchor carries vehicle_ids OR booking_refs, so each
  // list is absent on the other's rows. Reading them unguarded is the obvious
  // way to break a mixed thread the moment booking cards exist.
  const ids = [...new Set(anchors.flatMap((c) => c.vehicle_ids ?? []))]
  const refs = [...new Set(anchors.flatMap((c) => c.booking_refs ?? []))]

  const resolved = new Map<number, VehicleCard>()
  const resolvedBookings = new Map<string, BookingCard>()
  await Promise.all([
    ...ids.map(async (id) => {
      const detail = await fetchVehicle(id)
      if (detail) resolved.set(id, cardFromDetail(detail))
    }),
    // Re-fetched, never replayed from the transcript — which is what re-applies
    // the PII gate on every reload, and what makes a since-cancelled booking
    // (404) drop out of the thread instead of lingering as a stale card.
    ...refs.map(async (ref) => {
      const detail = await fetchBooking(ref, sessionId)
      if (detail) resolvedBookings.set(ref, toBookingCard(detail))
    }),
  ])

  // ONLY THE LAST CARD ROW PER REFERENCE SURVIVES A RELOAD. A booking that was
  // made and then rescheduled is anchored twice (both turns carry the ref), but
  // GET /api/booking/{ref} returns its CURRENT state — so rebuilding the earlier
  // row would draw today's slot wearing a "superseded" style, which is a lie
  // rather than history. The live thread can grey the old card because it still
  // holds the old data; a reload cannot, so it shows one card, the true one.
  // Keyed on turn INDEX, not `ts` — the transcript's timestamp is nullable, and
  // two turns can share a second anyway.
  const lastRowForRef = new Map<string, number>() // ref -> index of its last turn
  turns.forEach((t, i) => {
    for (const row of t.cards ?? []) {
      for (const ref of row.booking_refs ?? []) lastRowForRef.set(ref, i)
    }
  })

  const out: ChatMessage[] = []
  for (const [turnIndex, t] of turns.entries()) {
    const rows = t.cards ?? []
    // A VOICE NOTE COUNTS AS CONTENT even with no text. A recording we could not
    // transcribe is stored as an empty turn carrying only its audio_id, and the
    // player IS the message — the honest record that the customer spoke and no
    // words were recovered. Testing text alone would silently delete that turn
    // on every reload, so the thread would disagree with itself about whether
    // the customer had said anything.
    if (!t.text.trim() && rows.length === 0 && !t.audio_id) continue // nothing to show
    if (t.speaker === 'system') {
      // A marker turn nobody spoke — today only the voice-call summary
      // ("call:<seconds>", 2026-07-29). The DB stores the FACT; the line is
      // phrased here, per language, and renders muted like the lifecycle
      // lines. An unrecognised marker shows nothing (raw machine text must
      // never reach the customer), but its cards — the call's booking —
      // still render below.
      const call = /^call:(\d+)$/.exec(t.text.trim())
      if (call) {
        out.push({
          id: crypto.randomUUID(),
          kind: 'text',
          role: 'system',
          text: callSummaryLine(t.lang, Number(call[1])),
          streaming: false,
          lang: t.lang,
        })
      }
    } else if (t.text.trim() || t.audio_id) {
      out.push(turnToMessage(t, sessionId))
    }
    for (const row of rows) {
      const dir = t.lang === 'ar' ? 'rtl' : 'ltr'

      if (row.booking_refs) {
        const bookings = row.booking_refs
          .filter((ref) => lastRowForRef.get(ref) === turnIndex)
          .map((ref) => resolvedBookings.get(ref))
          .filter((b): b is BookingCard => b != null)
        if (bookings.length === 0) continue // all cancelled/unresolvable — drop
        out.push({ id: crypto.randomUUID(), kind: 'bookings', bookings, dir })
        continue
      }

      const cards = (row.vehicle_ids ?? [])
        .map((id) => resolved.get(id))
        .filter((c): c is VehicleCard => c != null)
      if (cards.length === 0) continue
      out.push({
        id: crypto.randomUUID(),
        kind: 'cards',
        cards,
        variant: row.variant ?? 'compact',
        // From the turn's OWN language, which is more faithful than the live
        // path's turnDirRef (a ref stamped at send time): a reloaded thread that
        // switched EN->AR mid-conversation gets each row's direction right.
        dir: t.lang === 'ar' ? 'rtl' : 'ltr',
      })
    }
  }
  return out
}

// A thread item is a text bubble, an inline vehicle-card row (rule 18), or a
// booking-confirmation receipt (the TD ref, from the booking SSE event).
export interface TextMessage {
  id: string
  kind: 'text'
  role: Role
  text: string
  streaming: boolean
  lang: Lang
  // Voice notes (2026-07-21). Present only on a customer turn that was SPOKEN —
  // Aisha never speaks, so an agent bubble can never carry these. The player
  // renders ABOVE the transcript inside the SAME bubble: one turn, one message,
  // showing both what was said and what we heard.
  //
  // A resolved URL rather than a bare id, so MessageBubble stays dumb and does
  // not need the session id threaded down to it. Ephemeral UI state either way —
  // what PERSISTS is the id, on the stored turn.
  audioSrc?: string
  // Known exactly at record time; absent after a reload, where the player reads
  // it from the audio metadata instead.
  durationMs?: number
  // The STORED id of this turn (read-aloud, 2026-07-21). Present on agent turns
  // once the stream has finished and the row exists — which is exactly when the
  // speaker icon appears. It is what the client points at to have the message
  // read: the server looks Aisha's words up itself rather than being sent them.
  turnId?: number
}
/** What `send` needs to know about a turn that was SPOKEN rather than typed.
 *  `failed` means the recording stored fine but nothing could be transcribed —
 *  the message is then empty and Aisha is asked to handle it in her own words. */
export interface VoiceTurn {
  audioId?: string
  durationMs?: number
  failed?: boolean
}

export interface CardsMessage {
  id: string
  kind: 'cards'
  cards: VehicleCard[]
  variant: CardVariant // 'full' (get_vehicle_details) | 'compact' (search recommendations)
  dir: 'rtl' | 'ltr' // layout direction, from the conversation turn (not card text)
}
export interface BookingMessage {
  id: string
  kind: 'booking'
  ref: string
  dir: 'rtl' | 'ltr'
}
export interface BookingsMessage {
  id: string
  kind: 'bookings'
  bookings: BookingCard[]
  dir: 'rtl' | 'ltr'
  // This row has been overtaken: a later card for the same reference replaced it
  // (a reschedule), or the booking was cancelled. It stays in the thread — the
  // transcript should still show that the 9 AM booking existed — but renders
  // muted and without its calendar link. Set by supersedeBookingRef.
  superseded?: boolean
}
export interface SlotsMessage {
  id: string
  kind: 'slots'
  slots: SlotChip[]
  dir: 'rtl' | 'ltr'
  // The slot_id the customer PICKED from this row, once they have (commit
  // semantics — see the tap handler in `send`). The chosen chip renders selected;
  // its siblings render dead. Undefined until a pick.
  selectedSlotId?: number
}
export interface ShowroomsMessage {
  id: string
  kind: 'showrooms'
  showrooms: ShowroomChoice[]
  dir: 'rtl' | 'ltr'
  // 'choice' = the get_showrooms pills; 'nearest' = the richer nearest-showroom
  // card (distance · time · directions link · Select).
  variant?: 'choice' | 'nearest'
  // The showroom the customer PICKED from this row (commit semantics, same as
  // SlotsMessage above). The chosen card renders selected; its siblings die.
  selectedShowroomId?: number
}
export interface OptionsMessage {
  id: string
  kind: 'options'
  options: OptionChip[]
  dir: 'rtl' | 'ltr'
  // The label the customer PICKED from this row, once they have. Commit
  // semantics, same as SlotsMessage above: the chosen chip renders selected and
  // its siblings go dead, so a glance back shows what was answered. Undefined
  // until a pick — and it stays undefined if they typed their answer instead,
  // which is a perfectly ordinary thing to do and must not look like an error.
  selectedLabel?: string
}
export interface CompareMessage {
  id: string
  kind: 'compare'
  data: Comparison
  dir: 'rtl' | 'ltr'
}
export type ChatMessage =
  | TextMessage
  | CardsMessage
  | BookingMessage
  | BookingsMessage
  | SlotsMessage
  | ShowroomsMessage
  | OptionsMessage
  | CompareMessage

// Friendly, customer-facing labels for the "working…" affordance. get_vehicle_
// details is deliberately ABSENT — it renders a card instead (rule 18); every
// other tool_event stays invisible behind this subtle label.
// get_vehicle_details -> full cards, search_vehicles -> compact cards, and
// get_available_slots -> slot chips and get_showrooms -> choice cards are all
// ABSENT here: they render UI, not a label. Everything else stays behind this
// subtle "working…" affordance.
const TOOL_LABELS: Record<string, string> = {
  lock_slot: 'Holding your slot…',
  find_or_create_customer: 'Finding your details…',
  record_consent: 'Saving your preferences…',
  create_booking: 'Confirming your booking…',
}

function langOf(text: string): Lang {
  return isRtl(text) ? 'ar' : 'en'
}

// Build a cards row (compact or full), or null if there are no cards. A BUILDER,
// not an append: the caller queues the row and commits it on the first token of
// the answer (see `queueRow` in `send`). Rows are always appended, never mutated,
// so promoting one compact card to a full one leaves the other compact cards
// untouched.
function buildCards(
  cards: VehicleCard[],
  variant: CardVariant,
  dir: 'rtl' | 'ltr',
): CardsMessage | null {
  if (cards.length === 0) return null
  return { id: crypto.randomUUID(), kind: 'cards', cards, variant, dir }
}

// Build a slot-chip row, or null if there are no slots (empty -> nothing; the
// agent says "nothing that day" in prose). The row carries its own id, so the
// caller can mark it the sole active offer (Fix 4) when it commits: only the
// newest slot row stays tappable, so a chip from an earlier turn can never pin a
// superseded slot_id.
function buildSlots(slots: SlotChip[], dir: 'rtl' | 'ltr'): SlotsMessage | null {
  if (slots.length === 0) return null
  return { id: crypto.randomUUID(), kind: 'slots', slots, dir }
}

// Build a booking-card row, or null when the lookup found nothing. AN EMPTY LIST
// RENDERS NOTHING on purpose: "no booking under that number" is Aisha's line to
// say, warmly and with an offer to try the other identifier. An empty card would
// be a worse way to deliver the same news.
// Build an option-chip row, or null when there is nothing to offer. Empty
// renders NOTHING, like every other builder here: the agent asked her question
// in words in the same message, so a chipless question is a normal question, not
// a broken one.
function buildCompare(data: Comparison | null, dir: 'rtl' | 'ltr'): CompareMessage | null {
  if (!data) return null
  return { id: crypto.randomUUID(), kind: 'compare', data, dir }
}

function buildOptions(options: OptionChip[], dir: 'rtl' | 'ltr'): OptionsMessage | null {
  if (options.length === 0) return null
  return { id: crypto.randomUUID(), kind: 'options', options, dir }
}

function buildBookings(bookings: BookingCard[], dir: 'rtl' | 'ltr'): BookingsMessage | null {
  if (bookings.length === 0) return null
  return { id: crypto.randomUUID(), kind: 'bookings', bookings, dir }
}

// Build a showroom-choice row, or null when the lookup found nothing. An EMPTY
// list renders nothing on purpose: "we have nothing in Fujairah" is Aisha's line
// to say honestly, with the nearest real branch offered as the alternative it
// actually is — the get_showrooms description spells that out.
/** The TD reference out of a create/reschedule/cancel result, or null. Mirrors
 *  _card_anchor in the backend's api/chat.py — an `error` envelope carries no
 *  booking and must draw nothing rather than throw inside a live turn. */
function bookingRefOf(output: unknown): string | null {
  if (!output || typeof output !== 'object') return null
  const booking = (output as { booking?: unknown }).booking
  if (!booking || typeof booking !== 'object') return null
  const ref = (booking as { booking_ref?: unknown }).booking_ref
  return typeof ref === 'string' ? ref : null
}

function buildShowrooms(
  showrooms: ShowroomChoice[],
  dir: 'rtl' | 'ltr',
  variant: 'choice' | 'nearest' = 'choice',
): ShowroomsMessage | null {
  if (showrooms.length === 0) return null
  return { id: crypto.randomUUID(), kind: 'showrooms', showrooms, dir, variant }
}

export interface UseChat {
  messages: ChatMessage[]
  streaming: boolean
  working: string | null
  // The id of the ONLY slot-chip row that is still tappable — the newest offer
  // (Fix 4). Null once a slot has been picked. Older slot rows stay visible but
  // inert, so a stale chip can never pin a superseded slot_id. App reads this to
  // disable every slot row but this one.
  activeSlotsId: string | null
  activeOptionsId: string | null
  // slotId (optional): a tapped slot chip pins lock_slot to it via the hidden
  // request field; the visible `text` stays clean human prose. Typed turns omit it.
  // slotsRowId (optional): the chip row that tap came from, so its chosen chip can
  // be marked selected and its siblings settled (commit semantics).
  send: (text: string, slotId?: number, slotsRowId?: string) => void
  // The id of the ONLY showroom row still tappable — the newest one, and null
  // once a branch has been picked. DERIVED from the thread rather than held in
  // state: the newest showrooms row IS the live one, so there is no second copy
  // of that fact to fall out of step with the messages.
  activeShowroomsId: string | null
  // A showroom card tap. Settles the row (commit semantics) and sends the choice
  // as ORDINARY PROSE — there is no showroom tap-intent, because no service
  // records a chosen showroom (see ShowroomCards' header).
  selectShowroom: (showroom: ShowroomChoice, rowId: string) => void
  selectOption: (label: string, rowId: string) => void
  stop: () => void
  // Rule 19. 'closed' disables the composer and reveals "Start new chat".
  status: ConversationStatus
  // Abandon this conversation and begin a fresh one: closes the old session
  // server-side (releasing any held slot), rotates the persisted session_id (so
  // the checkpointer opens a NEW thread rather than resuming the old one),
  // clears the thread, status back to 'active'.
  newChat: () => void
  // True while a persisted conversation is being fetched on load. Only suppresses
  // the empty-state flash — the thread renders as soon as it lands.
  hydrating: boolean
  // A recorded voice note: upload, transcribe, then send as an ordinary turn
  // (voice notes, 2026-07-21). Takes the blob straight from useVoice, which
  // knows nothing about chat — capture and conversation stay separate.
  sendVoice: (blob: Blob, mime: string, durationMs: number) => Promise<void>
  // True while a recording is uploading/transcribing. NOT `streaming`: the agent
  // has not been called yet and there is nothing to abort server-side, but the
  // composer must not accept a second send meanwhile.
  transcribing: boolean
  // Tell the idle ladder the microphone is open. An open mic is the most engaged
  // a customer ever is, and the ladder cannot otherwise see it — without this a
  // long voice note gets nudged mid-sentence and then closed out from under the
  // person recording it. App mirrors useVoice's state into this.
  setRecording: (active: boolean) => void
  // Tell the idle ladder a message is being read aloud (read-aloud,
  // 2026-07-21). Listening is presence for the same reason an open mic is.
  setSpeaking: (active: boolean) => void
  // This conversation's id. Exposed because read-aloud builds a per-session URL
  // (GET /api/speech/{turn}?session_id=), and the clip cache is session-scoped
  // so that a reply quoting someone's name and mobile is never shared.
  sessionId: string
  // The LIVE session id, resolved when invoked (2026-07-29). The call dials
  // with THIS, never the snapshot above: anything that connects after a
  // same-tick rotation (call-on-closed rotates then starts) must see the new
  // id, and a getter cannot go stale the way a captured string can.
  getSessionId: () => string
  // --- voice call (voice phase) ------------------------------------------- #
  // DURING a call the thread is NOT updated (product decision 2026-07-27: the
  // call is a full-screen overlay, audio-first — rendering bubbles mid-call
  // bought nothing anyone was looking at). The server still persists every
  // turn; callEnded refreshes the thread FROM the server once, so the chat
  // catches up the moment the overlay closes.
  // The call is over: refresh the thread, re-arm the idle ladder from a fresh
  // anchor — and for endings the customer did not choose (a refusal, a dropped
  // connection, a watchdog), SAY WHY in a muted system line. A call that
  // vanishes wordlessly reads as a broken button.
  callEnded: (reason?: string) => void
  // Tell the idle ladder a call is live. A customer on a call neither types nor
  // taps — presence the ladder cannot otherwise see (the open-mic rule, writ
  // large). App mirrors useCall's `active` into this.
  setInCall: (active: boolean) => void
}

export function useChat(): UseChat {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState(false)
  // A recording is uploading and being transcribed. Distinct from `streaming`:
  // the agent has not been called yet and there is nothing to abort server-side,
  // but the composer must not accept a second send in the meantime.
  const [transcribing, setTranscribing] = useState(false)
  // The microphone is open. OWNED HERE rather than read from useVoice, because
  // the idle ladder lives here and this is the fact it was missing: an open mic
  // is presence, and without it a long voice note could be closed out from under
  // the customer (see the ladder effect). App mirrors useVoice's state into it.
  const [recording, setRecordingState] = useState(false)
  // A message is being read aloud (read-aloud, 2026-07-21). Same reasoning as
  // `recording`, from the other direction: a customer listening to a 40-second
  // recap is present, and the ladder cannot otherwise tell.
  const [speaking, setSpeakingState] = useState(false)
  // A voice call is live (voice phase). Presence for the same reason as an open
  // mic — MORE so: the whole point of a call is that the customer neither types
  // nor taps, which to an unsuppressed ladder is indistinguishable from having
  // left. App mirrors useCall's `active` into this.
  const [inCall, setInCallState] = useState(false)
  const [working, setWorking] = useState<string | null>(null)
  // Fix 4: which slot offer is live. Moves to each new get_available_slots row;
  // cleared to null the moment the customer picks a slot.
  const [activeSlotsId, setActiveSlotsId] = useState<string | null>(null)
  // Only the NEWEST option row is tappable, for the same reason as activeSlotsId:
  // a "Yes" chip from a question three turns ago would send a bare yes into a
  // conversation that has moved on, and the agent would have to guess what it
  // agreed to. Older rows stay visible and inert.
  const [activeOptionsId, setActiveOptionsId] = useState<string | null>(null)

  // --- rule 19: conversation lifecycle ------------------------------------- #
  const [status, setStatus] = useState<ConversationStatus>('active')
  // The idle clock's anchor: when anything last HAPPENED in this conversation.
  // Stamped on a customer send and on stream end (see `send`), which — because
  // an agent turn always follows a send — makes the effective anchor "the agent
  // finished and is waiting". So a customer who reads for three minutes IS
  // closed, and a 60-second tool loop does not eat their reading time.
  const [lastActivityAt, setLastActivityAt] = useState(() => Date.now())
  // The close guard, in a REF not state: it must read true synchronously, the
  // instant the first close lands. A state read here would still be 'active'
  // inside the same batch (and StrictMode would double-invoke an updater), which
  // is exactly how you end up with the canned line stacked under the agent's
  // farewell. This ref is what makes closeChat idempotent.
  const statusRef = useRef<ConversationStatus>('active')
  // Whether the 2-minute check-in has already been asked on the CURRENT silence.
  // A ref for the same reason as statusRef: appending the nudge re-runs the timer
  // effect (messages.length is a dep), and a state read would still be stale
  // inside that batch — which is how you get the nudge asked twice. Cleared
  // whenever the customer speaks, so a later silence can nudge again.
  const nudgedRef = useRef(false)
  // The id of the check-in line currently on screen, so answering it can remove
  // that exact message rather than matching on its text. Text matching would be
  // wrong twice over: the line is bilingual (a mid-conversation language switch
  // means the text we would search for is not the text we rendered), and the
  // closing line lives in the same LIFECYCLE_LINES table — a loose match could
  // delete the wrong one. Null whenever no check-in is showing.
  const nudgeIdRef = useRef<string | null>(null)

  // ONE session id per conversation, PERSISTED PER TAB (sessionStorage) so a
  // reload resumes the same conversation — same id means the same checkpointer
  // thread, so the agent keeps its full context, not just the visible text.
  const sessionId = useRef<string>('')
  // True only when the id came back from storage, i.e. there may be a prior
  // conversation to rehydrate. A first visit skips the fetch entirely.
  const needsResume = useRef(false)
  // A WhatsApp handoff token, read+stripped from the URL once at load. When
  // present it takes precedence over any stored session: we redeem it for a fresh
  // BOUND session and let Aisha greet, rather than resuming the stored thread.
  const handoffToken = useRef<string | null>(null)
  // Set true the moment the redeem result is applied, so neither StrictMode's
  // second effect-invoke nor a later re-run (when `send` changes identity) can
  // install the session or fire the greeting twice.
  const handoffApplied = useRef(false)
  if (!sessionId.current) {
    handoffToken.current = takeHandoffToken()
    const stored = readStoredSession()
    sessionId.current = stored.id
    // A handoff overrides resume: the redeem effect installs its own session and
    // fires the greeting, so the stored thread must not also rehydrate over it.
    needsResume.current = stored.existing && !handoffToken.current
  }
  // Suppresses the empty-state flash while a prior thread is being fetched — or
  // while a handoff token is being redeemed and Aisha's greeting is on its way.
  const [hydrating, setHydrating] = useState(() => needsResume.current || !!handoffToken.current)

  const abortRef = useRef<AbortController | null>(null)
  const turnDirRef = useRef<'rtl' | 'ltr'>('ltr') // direction of the current turn, for cards/receipt

  // Point this browser at a brand-new conversation. A NEW id is the whole
  // mechanism: it is the checkpointer's thread_id, so a new id means a new
  // thread and the old conversation is never replayed into it.
  const rotateSession = useCallback(() => {
    sessionId.current = crypto.randomUUID()
    storeSession(sessionId.current)
    needsResume.current = false
  }, [])

  // REHYDRATE ON LOAD. Runs once, only when a stored id was found.
  //
  // THE ZERO-TURNS GUARD is the important half. The transcript lives in the app
  // DB but the agent's memory lives in a SEPARATE checkpoint file, so a reset of
  // one without the other (demo morning, a wiped DB, a stale phone) would leave
  // this browser showing an empty thread while the agent still remembered
  // everything — the agent appearing to invent context from nowhere. So an id
  // that yields no turns is treated as unusable and rotated away, keeping what
  // the customer sees and what the model knows in agreement by construction.
  // `reset-demo` clears both together so this rarely fires; it is the braces.
  useEffect(() => {
    if (!needsResume.current) return
    let cancelled = false
    void (async () => {
      const convo = await fetchConversation(sessionId.current)
      if (cancelled) return // StrictMode's double-invoke, or an unmount
      if (!convo || convo.turns.length === 0) {
        rotateSession()
      } else {
        // Resolves the turns' vehicle ids and booking refs into cards before
        // painting, so the thread arrives whole. Re-checked after the await: the
        // fetches take a moment, and an unmount (or StrictMode) in that window
        // must not paint. The session id goes along because booking detail is
        // viewer-scoped — see fetchBooking.
        const rebuilt = await rebuildTurns(convo.turns, sessionId.current)
        if (cancelled) return
        setMessages(rebuilt)
        if (convo.status === 'closed') {
          // Resume a closed conversation VISIBLY: the customer sees what they
          // did and the "New chat" control is right there. Silently discarding
          // it would erase a booking they may want to re-read.
          statusRef.current = 'closed'
          setStatus('closed')
        }
        nudgedRef.current = false
        // The rebuilt thread cannot contain a check-in (they are client-side and
        // never replayed), so any id still held here points at a message that no
        // longer exists. Dropping it keeps the ref honest rather than relying on
        // the filter to no-op.
        nudgeIdRef.current = null
        setLastActivityAt(Date.now()) // the idle ladder starts fresh on resume
        needsResume.current = false
      }
      setHydrating(false)
    })()
    return () => {
      cancelled = true
    }
  }, [rotateSession])

  // THE ONE WAY A CHAT CLOSES (rule 19). Both exits — the agent's sign-off and
  // the idle timer — funnel through here, which is what makes double-closing
  // impossible rather than merely unlikely:
  //   • the ref guard makes the second call a no-op, so if the customer says
  //     "no" at 2:59 the idle timer firing at 3:00 adds nothing;
  //   • setting 'closed' tears the timer effect down, so the usual case is that
  //     the timer never fires at all.
  // Only the idle path appends a line. On a sign-off the agent has ALREADY said
  // goodbye in its own words (and in the customer's language) — appending our
  // canned English line under it would be a second, worse farewell.
  const closeChat = useCallback((reason: CloseReason) => {
    if (statusRef.current === 'closed') return
    statusRef.current = 'closed'
    if (reason === 'idle') {
      setMessages((prev) => {
        const lang = conversationLang(prev)
        return [
          ...prev,
          {
            id: crypto.randomUUID(),
            kind: 'text',
            role: 'system',
            text: LIFECYCLE_LINES[lang].closing,
            streaming: false,
            lang,
          },
        ]
      })
      // AN IDLE CLOSE IS A REAL CLOSE, not just a greyed-out composer. Same
      // service as the agent's close_conversation tool, so an abandoned chat
      // RELEASES any slot it was holding instead of stranding that inventory for
      // the full lock TTL, and the close lands in the audit trail like every
      // other one (rule 15). Fire-and-forget: the customer has already left, so
      // this must never block the UI, and the lock TTL stays the backstop if the
      // request fails.
      //
      // This is also what makes a reload after an idle close rehydrate as
      // 'closed' rather than resurrecting the chat as active — the server now
      // knows. The session_id is deliberately NOT rotated here: the transcript
      // stays readable, and "Start new chat" owns rotation.
      void closeConversation(sessionId.current)
    }
    setStatus('closed')
  }, [])

  // The 2-minute check-in. Deliberately does NOT stamp lastActivityAt: the nudge
  // is us speaking, not the customer, and treating it as activity would restart
  // the clock and silently cancel the very close it exists to precede.
  //
  // Rendered as a 'system' line (centred, muted) rather than as Aisha, because
  // she did not write it — the model is not involved. One-line change to promote
  // it to role: 'agent' if the demo wants it in her voice.
  const nudge = useCallback(() => {
    if (statusRef.current === 'closed' || nudgedRef.current) return
    nudgedRef.current = true
    // Minted out here, not inside the updater, so `send` can find this exact
    // line again to remove it once the customer answers.
    const id = crypto.randomUUID()
    nudgeIdRef.current = id
    setMessages((prev) => {
      const lang = conversationLang(prev)
      return [
        ...prev,
        {
          id,
          kind: 'text',
          role: 'system',
          text: LIFECYCLE_LINES[lang].nudge,
          streaming: false,
          lang,
        },
      ]
    })
  }, [])

  // The idle clock — a two-rung ladder: check in at 2 minutes, close at 3.
  // Keyed on lastActivityAt, NOT fire-and-forget setTimeouts: every stamp re-runs
  // this effect, whose cleanup clears the previous timers, so the countdown
  // genuinely restarts on each message instead of racing an old one to expiry.
  //
  // BOTH DELAYS ARE COMPUTED FROM THE ANCHOR, never chained. This effect re-runs
  // the moment the nudge appends (messages.length is a dep), so a naive
  // `setTimeout(close, IDLE_CLOSE_MS)` would restart the full three minutes and
  // the chat would never close. Subtracting elapsed makes the re-run self-
  // correcting: two minutes have passed, so the close timer re-arms with the one
  // minute that is actually left.
  //
  // Neither rung exists while `streaming` — the agent mid-answer is the opposite
  // of an abandoned chat, and a long tool loop must never close (or interrupt)
  // the window it is about to reply into. When streaming ends, `send`'s finally
  // stamps lastActivityAt and this effect mounts fresh with a full 3 minutes.
  //
  // NOR WHILE RECORDING OR TRANSCRIBING (voice notes, 2026-07-21). A customer
  // speaking into the microphone is the MOST engaged they ever are, and the
  // ladder could not see it: recording is not `streaming`, and the activity
  // stamp only landed once the recording was already on its way. So a 60-second
  // note started late in a quiet stretch would draw "Are you still there?"
  // mid-sentence and then, a minute later, CLOSE THE CHAT — releasing the slot
  // and discarding the recording on `stop`, because sendVoice refuses a closed
  // session. Suppressing both rungs here is what makes an open mic count as
  // presence; the stamp when recording ends then re-arms the full ladder.
  // NOR WHILE A CALL IS LIVE (voice phase): a customer on a call is present the
  // entire time by definition, and the ladder cannot see speech. `callEnded`
  // stamps the anchor, so hanging up re-arms the full ladder rather than
  // counting the call's minutes as silence.
  useEffect(() => {
    if (status === 'closed' || streaming || recording || transcribing || speaking || inCall)
      return
    if (messages.length === 0) return // nothing said yet; nothing to close
    const elapsed = Date.now() - lastActivityAt
    const timers: ReturnType<typeof setTimeout>[] = []
    if (!nudgedRef.current) {
      timers.push(setTimeout(nudge, Math.max(0, IDLE_NUDGE_MS - elapsed)))
    }
    timers.push(setTimeout(() => closeChat('idle'), Math.max(0, IDLE_CLOSE_MS - elapsed)))
    return () => timers.forEach(clearTimeout)
  }, [
    status,
    streaming,
    recording,
    transcribing,
    speaking,
    inCall,
    lastActivityAt,
    messages.length,
    closeChat,
    nudge,
  ])

  // Mark every booking card row for `ref` as overtaken. Called when that booking
  // is rescheduled (a newer card follows) or cancelled (none does). The rows stay
  // — a transcript that silently loses the 9 AM card reads as though it never
  // happened — but they render muted and lose their calendar link.
  const supersedeBookingRef = useCallback((ref: string) => {
    setMessages((prev) =>
      prev.map((m) =>
        m.kind === 'bookings' && m.bookings.some((b) => b.ref === ref)
          ? { ...m, superseded: true }
          : m,
      ),
    )
  }, [])

  // Draw the card for a booking that was just made or moved.
  //
  // FETCHED, NOT READ FROM THE TOOL EVENT, and that is the whole point: the
  // create_booking / reschedule_booking results come from _booking_dict WITHOUT a
  // viewer, so they carry no name and no mobile. GET /api/booking/{ref} resolves
  // the viewer from the checkpoint, and at this moment the session is always
  // identified (create_booking could not have run otherwise) — so this is the one
  // place the card is reliably complete.
  //
  // NOT QUEUED, unlike every other row: queueRow is synchronous by design and
  // this has to await a fetch. The card therefore lands just after the prose
  // rather than with it, which for a confirmation is the right order anyway.
  //
  // FALLS BACK TO THE PILL. If the fetch fails, a booking must never go
  // unacknowledged on screen — the old receipt is the honest minimum.
  const showBookingCard = useCallback(
    async (ref: string, dir: 'rtl' | 'ltr') => {
      const detail = await fetchBooking(ref, sessionId.current)
      const card = detail ? toBookingCard(detail) : null
      setMessages((prev) => {
        // Any earlier card for this ref is now history — including one drawn by a
        // get_booking lookup, which is just as stale after a reschedule.
        const marked = prev.map((m) =>
          m.kind === 'bookings' && m.bookings.some((b) => b.ref === ref)
            ? { ...m, superseded: true }
            : m,
        )
        return [
          ...marked,
          card
            ? { id: crypto.randomUUID(), kind: 'bookings' as const, bookings: [card], dir }
            : { id: crypto.randomUUID(), kind: 'booking' as const, ref, dir },
        ]
      })
    },
    [],
  )

  // ONE implementation of how an agent turn RENDERS — the live bubble that
  // moves across text_breaks, the deferred card rows, the receipt, the close —
  // shared by the SSE chat turn (`send`) and the WS call turn (`callUtterance`),
  // for the same reason the backend shares run_turn between the two channels:
  // two copies of this logic WOULD drift, and a call thread must read exactly
  // like a typed one.
  //
  // Each invocation is ONE turn: it appends the live agent bubble, returns the
  // stream handlers for that turn, and a `finalize` that every terminal path
  // must funnel through (stream close, call `done`, error, hang-up).
  const createAgentTurn = useCallback(() => {
    // THE LIVE BUBBLE, which MOVES. A turn is no longer one agent bubble: a
    // text_break seals the current one and opens the next, so tokens have to
    // follow a target that changes mid-stream (see `breakBubble`).
    //
    // Read into a local BEFORE every updater. `liveId` is reassigned the
    // instant a break happens, while React may run a queued updater later — an
    // updater that closed over the variable itself would patch whichever bubble
    // is live by the time it runs, not the one it was written for.
    let liveId = crypto.randomUUID()
    setMessages((prev) => [
      ...prev,
      { id: liveId, kind: 'text', role: 'agent', text: '', streaming: true, lang: 'en' },
    ])
    setStreaming(true)
    setWorking(null)

    const patchAgent = (fn: (m: TextMessage) => TextMessage) => {
      const target = liveId
      setMessages((prev) =>
        prev.map((m) => (m.id === target && m.kind === 'text' ? fn(m) : m)),
      )
    }

    // Seal a bubble: stop it streaming, or drop it entirely if it never got
    // any text. An empty bubble is not a message — left in place it renders as
    // an empty grey pill with no dots (dots need `streaming`, false by now).
    const seal = (rows: ChatMessage[], id: string): ChatMessage[] =>
      rows.flatMap((m) => {
        if (m.id !== id || m.kind !== 'text') return [m]
        return m.text.trim() ? [{ ...m, streaming: false }] : []
      })

    // Rule 17: the model is about to call a tool, so whatever it just wrote is
    // finished. Seal it and open a fresh bubble for what comes after.
    //
    // NOTHING IS DELETED — that is the entire point of this event, and the
    // reason it replaced `text_reset`, which emptied the bubble instead. Text
    // written before a tool call is not reliably throwaway narration: a booking
    // recap and a farewell each arrived that way, with nothing following to
    // re-say them, and were erased in front of the customer. Two bubbles fix
    // the run-on this guards against without ever taking words back.
    //
    // The new bubble is empty and streaming, so the typing dots reappear while
    // the tool runs — the customer sees the agent still working, not a gap.
    // If the turn ends without another token, finalizeAgent drops it.
    const breakBubble = () => {
      const closing = liveId
      const nextId = crypto.randomUUID()
      liveId = nextId
      setMessages((prev) => [
        ...seal(prev, closing),
        { id: nextId, kind: 'text', role: 'agent', text: '', streaming: true, lang: 'en' },
      ])
    }

    const finalizeAgent = () => {
      const target = liveId
      setMessages((prev) => seal(prev, target))
    }

    // DEFERRED VISUALS. A tool_event arrives a whole model round-trip BEFORE
    // the first token of the answer it feeds, so appending its row on arrival
    // drops cards, chips and receipts onto the screen under an empty bubble
    // that is still showing typing dots — the customer reads the result before
    // the reply. So rows are BUILT on arrival (cheap, and an empty result is
    // detected early) and QUEUED, then committed on the first token, which is
    // the moment the answer starts and the cards read as part of it.
    //
    // While the queue holds rows the "working…" affordance keeps running: the
    // agent IS still working, and rule 17 says she does not narrate that — the
    // dots are the honest signal, not a half-drawn answer.
    //
    // Order is preserved by construction: the agent bubble was appended before
    // the stream opened, so a flush at the tail always lands BELOW it, and
    // rows within one turn commit in tool order.
    const pendingRows: ChatMessage[] = []
    let pendingActiveSlotsId: string | null = null
    let pendingActiveOptionsId: string | null = null
    const queueRow = (row: ChatMessage | null, activeSlots = false) => {
      if (!row) return
      pendingRows.push(row)
      if (activeSlots) pendingActiveSlotsId = row.id
      // An option row is ALWAYS the live one when it lands — offer_options ends
      // the turn, so nothing can arrive after it to supersede it.
      if (row.kind === 'options') pendingActiveOptionsId = row.id
    }
    // MUST run on every terminal path, not just onToken — a tool-only turn, an
    // abort mid-loop or an error would otherwise swallow the rows entirely.
    // Idempotent: draining leaves the queue empty, so extra calls are no-ops.
    const flushRows = () => {
      if (pendingRows.length === 0) return
      const rows = pendingRows.splice(0, pendingRows.length)
      setMessages((prev) => [...prev, ...rows])
      setWorking(null)
      if (pendingActiveSlotsId) {
        setActiveSlotsId(pendingActiveSlotsId)
        pendingActiveSlotsId = null
      }
      if (pendingActiveOptionsId) {
        setActiveOptionsId(pendingActiveOptionsId)
        pendingActiveOptionsId = null
      }
    }

    const handlers: StreamHandlers = {
      onToken: (e) => {
        // The answer has started: commit any queued rows FIRST, so they land
        // below the bubble and appear together with the prose that explains
        // them. No-op on every token after the first.
        flushRows()
        setWorking(null)
        patchAgent((m) => ({ ...m, text: m.text + e.text, lang: langOf(m.text + e.text) }))
      },
      onToolEvent: (e) => {
        // Two customer-facing tool_events render cards (rule 18 / FR-BK-10):
        //   get_vehicle_details -> FULL card(s) ("the one you're looking at")
        //   search_vehicles     -> up to 3 COMPACT cards ("here are options")
        // Every other tool_event stays behind the subtle "working…" label.
        if (e.tool === 'get_vehicle_details') {
          queueRow(buildCards(extractCards(e.output), 'full', turnDirRef.current))
          return
        }
        if (e.tool === 'search_vehicles') {
          queueRow(buildCards(extractSearchCards(e.output), 'compact', turnDirRef.current))
          return
        }
        // A booking that MOVED draws a fresh card and retires the old one.
        // create_booking is not here: it already has the `booking` SSE event,
        // which fires only on a genuine create (never an idempotent replay).
        if (e.tool === 'reschedule_booking') {
          const ref = bookingRefOf(e.output)
          if (ref) void showBookingCard(ref, turnDirRef.current)
          return
        }
        // A cancelled booking draws NO card — there is nothing to show, and
        // GET /api/booking/{ref} 404s it, so a reload would drop it anyway.
        // Retiring the existing card is what makes the live screen agree.
        if (e.tool === 'cancel_booking') {
          const ref = bookingRefOf(e.output)
          if (ref) supersedeBookingRef(ref)
          return
        }
        if (e.tool === 'get_showrooms') {
          // `true` on the second arg would mark it the active SLOT row —
          // showrooms track their own newest row, set at flush below.
          queueRow(buildShowrooms(extractShowroomChoices(e.output), turnDirRef.current))
          return
        }
        if (e.tool === 'nearest_showroom') {
          // The nearest branch(es) render as selectable cards (tap to pick). When
          // the tool asks for a landmark there are no cards; the "use my current
          // location" button is shown by App based on the agent's question text.
          queueRow(buildShowrooms(extractNearestShowroomChoices(e.output), turnDirRef.current, 'nearest'))
          return
        }
        if (e.tool === 'get_booking') {
          // Empty-means-nothing lives in the builder, like the other three —
          // queueRow already drops a null row.
          queueRow(buildBookings(extractBookingCards(e.output), turnDirRef.current))
          return
        }
        if (e.tool === 'compare_vehicles') {
          queueRow(buildCompare(extractComparison(e.output), turnDirRef.current))
          return
        }
        if (e.tool === 'offer_options') {
          // Chips for a question Aisha just asked in words. No 'working…' label:
          // this tool fetches nothing and the question is already on screen, so
          // announcing it would be narrating our own machinery.
          queueRow(buildOptions(extractOptionChips(e.output), turnDirRef.current))
          return
        }
        if (e.tool === 'get_available_slots') {
          // The new offer becomes the sole tappable slot row (Fix 4); any
          // earlier slot row is now inert. Both the row and its tappability
          // land at flush — a chip cannot be tapped before it is on screen.
          queueRow(buildSlots(extractSlotChips(e.output), turnDirRef.current), true)
          return
        }
        setWorking(TOOL_LABELS[e.tool] ?? 'Working…')
      },
      // The booking event is the dependable signal that a booking landed and
      // carries the TD ref. It drops a glanceable receipt into the thread
      // (the agent's prose is the warm confirmation — different job).
      // Flush first: the receipt appends immediately (it is the dependable
      // signal a booking landed, and should not wait on prose), so anything
      // still queued has to commit ahead of it or the turn's rows would
      // render out of tool order.
      onBooking: (e) => {
        flushRows()
        void showBookingCard(e.booking_ref, turnDirRef.current)
      },
      // Rule 17: seal this bubble, start the next. Never empties anything.
      onTextBreak: () => {
        // THE GAP THIS CLOSES (T5.7). onToken clears the working indicator on
        // the FIRST token, and nothing restores it until a tool_event arrives.
        // In a single-agent turn that gap is invisible. In a multi-agent turn
        // Aisha streams her opening line, text_break seals it, and then the
        // sub-agents run for seconds with NO indicator at all — the UI looks
        // finished while the answer is still being gathered.
        //
        // A text_break means "that bubble is done and there is more coming",
        // which is exactly when the indicator should come back.
        breakBubble()
        setWorking('Working…')
      },
      // Rule 19a: the agent said goodbye and closed the chat server-side.
      // No line is appended — its farewell is the last bubble already, in
      // the customer's own language. This also CANCELS the idle timer, so
      // the sign-off path can never be followed by a canned close.
      onClosed: () => closeChat('signoff'),
      // `turn_id` arrives here and nowhere earlier, because it is the id of
      // the STORED row — the backend commits the agent turn and then sends
      // it. That timing is also the rule: a message becomes readable aloud
      // only once it is finished and persisted, which is precisely when the
      // speaker icon should appear.
      onDone: (e) =>
        patchAgent((m) => ({
          ...m,
          streaming: false,
          lang: e.lang,
          ...(e.turn_id != null ? { turnId: e.turn_id } : {}),
        })),
      onError: (e) => {
        // Rows the tools already produced are real results — commit them
        // rather than lose them behind the failure, and keep the error line
        // last.
        flushRows()
        // Targets the CURRENT live bubble: after a text_break that is the
        // fresh empty one, so the error replaces it in place rather than
        // stranding an empty pill above the message. Any bubble sealed
        // earlier in this turn is left exactly as the customer read it.
        const target = liveId
        setMessages((prev) => {
          const partial = prev.find((m) => m.id === target)
          if (partial && partial.kind === 'text' && partial.text.length === 0) {
            return prev.map((m) =>
              m.id === target && m.kind === 'text'
                ? { ...m, role: 'error', text: e.message, streaming: false }
                : m,
            )
          }
          return [
            ...prev.map((m) => (m.id === target && m.kind === 'text' ? { ...m, streaming: false } : m)),
            { id: crypto.randomUUID(), kind: 'text', role: 'error', text: e.message, streaming: false, lang: 'en' },
          ]
        })
      },
      onAborted: finalizeAgent,
    }

    // The backstop for every ending that produced no token: a tool-only turn,
    // an abort mid-tool-loop, a stream that died. Without it those rows would
    // be built and silently dropped. Also where the customer's silence starts:
    // the turn is over NOW, not when they sent — which could have been a
    // minute of tool calls ago.
    const finalize = () => {
      flushRows()
      setStreaming(false)
      setWorking(null)
      finalizeAgent()
      setLastActivityAt(Date.now())
    }

    return { handlers, finalize }
  }, [closeChat, showBookingCard, supersedeBookingRef])

  const send = useCallback(
    (
      raw: string,
      slotId?: number,
      slotsRowId?: string,
      voice?: VoiceTurn,
      // WhatsApp handoff kickoff (2026-07-22): `raw` is a server-authored greeting
      // directive, not the customer's words. Aisha speaks first, so NO user bubble
      // is appended and the backend stores no customer turn (kickoff:true). `lang`
      // sets the turn direction for any card she draws, since the directive itself
      // is always English regardless of the language she is told to reply in.
      kickoffOpts?: { kickoff?: boolean; lang?: 'en' | 'ar' },
    ) => {
      const text = raw.trim()
      const isKickoff = kickoffOpts?.kickoff === true
      // A closed chat sends nothing (rule 19). The composer is already disabled;
      // this is the guard behind it, so a stale chip tap or a queued keypress
      // cannot reopen a conversation the customer ended.
      //
      // A FAILED VOICE NOTE IS THE ONE TURN WITH NO TEXT that may still be sent:
      // the customer really did speak, and Aisha answers the failure herself
      // (the backend shows her a note about it rather than invented words). Every
      // other empty send is still refused.
      if ((!text && !voice?.failed) || streaming || statusRef.current === 'closed') return

      // The customer spoke — restart the idle ladder from rung one. Clearing
      // nudgedRef is what re-arms the check-in, so a customer who answers a nudge
      // and then goes quiet again gets asked again rather than closing silently.
      nudgedRef.current = false
      setLastActivityAt(Date.now())

      // ANSWERED CHECK-IN DISAPPEARS. "Are you still there?" followed by the
      // customer's reply has served its whole purpose; left on screen it reads
      // like Aisha nagged someone who was mid-sentence, and it is not her line
      // to have said (rule 19b — the system lines are ours, not hers).
      //
      // This is NOT a counter-example to "never take back words the customer has
      // read" (the text_break rule). That rule protects ANSWERS — content the
      // customer might act on, written by Aisha, persisted as a turn. This line
      // is none of those: it carries no information once answered, the model
      // never wrote it, and it is client-side only — GET /api/conversation never
      // replays it. So removing it here makes the live thread MATCH what a reload
      // already shows, instead of diverging from it.
      //
      // Only the check-in, never the closing line: that one explains why the
      // chat ended and must survive.
      if (nudgeIdRef.current) {
        const nudgeId = nudgeIdRef.current
        nudgeIdRef.current = null
        setMessages((prev) => prev.filter((m) => m.id !== nudgeId))
      }

      // COMMIT vs BROWSE — the rule for tappable options, do NOT merge these:
      //   • A tap that SENDS A CHOICE (a slot chip) settles its row: the chosen
      //     chip is marked SELECTED and its siblings die. That is here.
      //   • A tap that ASKS A QUESTION (a car card's "View more") is browsing and
      //     must leave its siblings fully live — see VehicleCards. Same visual
      //     pattern, opposite meaning.
      // A chip tap (slotId present) commits: mark the chosen chip selected on its
      // row, then retire the offer so no slot row — not even this one — stays
      // tappable (Fix 4). A new offer this turn re-arms activeSlotsId when its
      // queued row flushes (queueRow's `activeSlots` flag).
      if (slotId != null) {
        if (slotsRowId) {
          setMessages((prev) =>
            prev.map((m) =>
              m.kind === 'slots' && m.id === slotsRowId ? { ...m, selectedSlotId: slotId } : m,
            ),
          )
        }
        setActiveSlotsId(null)
      }

      // A failed voice note has no text to sniff, so keep the direction the
      // conversation already had rather than snapping an Arabic thread to LTR.
      // A kickoff directive is always English, so sniffing it would force LTR even
      // for an Arabic greeting — use the redeem's language for the turn instead.
      if (isKickoff) turnDirRef.current = kickoffOpts?.lang === 'ar' ? 'rtl' : 'ltr'
      else if (text) turnDirRef.current = isRtl(text) ? 'rtl' : 'ltr'

      // NO user bubble on a kickoff: the "message" is a server-authored
      // directive, not the customer's words, so Aisha's greeting is the first
      // thing in the thread (matching the backend, which stores no customer
      // turn for it). Every other turn appends the customer's bubble as before.
      if (!isKickoff) {
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            kind: 'text' as const,
            role: 'user' as const,
            text,
            streaming: false,
            // An empty (failed) voice note has no script to read, so inherit
            // the thread's direction instead of defaulting to English.
            lang: text ? langOf(text) : turnDirRef.current === 'rtl' ? 'ar' : 'en',
            // Spoken turns carry their recording; typed turns leave these
            // undefined and render exactly as before.
            ...(voice?.audioId
              ? {
                  audioSrc: audioUrl(voice.audioId, sessionId.current),
                  durationMs: voice.durationMs,
                }
              : {}),
          },
        ])
      }
      // The agent side of the turn — live bubble, deferred rows, receipt,
      // close — is the shared machinery (createAgentTurn above), so the SSE
      // path and the call path render identically by construction.
      const turn = createAgentTurn()

      const controller = new AbortController()
      abortRef.current = controller

      // A TAPPED chip locks the slot BEFORE the turn goes out (the DIRECT path).
      // The chip already carries slot_id, so the agent does not need a model call
      // to emit lock_slot — the backend locks it, audits it as actor=customer,
      // and writes it into the agent's memory. Aisha still speaks (rule 8's
      // hospitable exchange follows slot acceptance); this removes the tool
      // round-trip inside her turn, not the turn.
      //
      // The bubbles are already on screen, so the customer sees their choice
      // instantly and the await is invisible.
      //
      // BEST EFFORT, AND slotId IS STILL PINNED BELOW. If this fails — or if the
      // model calls lock_slot anyway — the pinned id keeps that call correct. So
      // the worst case is the old behaviour at the old cost, never a wrong slot
      // and never a broken turn.
      //
      // A FAILED LOCK IS NOT SWALLOWED: {locked:false, slot_no_longer_available}
      // lands in the agent's history, which is exactly what it needs to say the
      // slot just went and re-offer (FR-SL-01).
      const tapped = slotId != null ? lockSlotIntent(sessionId.current, slotId) : Promise.resolve(null)

      void tapped.then(() => streamChat(
        sessionId.current,
        text,
        turn.handlers,
        controller.signal,
        {
          ...(slotId != null ? { slotId } : {}),
          // Stamped onto the stored turn so a reload rebuilds the player. Sent
          // even when transcription failed — the recording exists either way,
          // and it is the only record of what was actually said.
          ...(voice?.audioId ? { audioId: voice.audioId } : {}),
          ...(voice?.failed ? { voiceFailed: true } : {}),
          // Handoff kickoff: backend stores no customer turn for this directive.
          ...(isKickoff ? { kickoff: true } : {}),
        },
      )).finally(() => {
        // Every terminal path — normal close, abort, stream death — funnels
        // through the turn's own finalize (rows flushed, bubble sealed,
        // activity stamped). Only the abort handle is send's to clear.
        turn.finalize()
        abortRef.current = null
      })
    },
    [streaming, createAgentTurn],
  )

  // REDEEM A WHATSAPP HANDOFF LINK. Runs once, only when the URL carried `?h=`.
  //
  // The token was already stripped from the address bar at load; here we exchange
  // it for a FRESH session the backend has bound to the customer (name, consent,
  // memory all in the agent checkpoint), then fire the server-authored greeting
  // as a kickoff so Aisha speaks FIRST — no customer bubble, because the customer
  // said their piece on WhatsApp, not here.
  //
  // A stale/spent/invalid token degrades to a normal fresh chat rather than an
  // error: redeemHandoff never throws, and on !ok we simply drop the hydrating
  // veil and let the empty composer stand.
  useEffect(() => {
    const token = handoffToken.current
    if (!token || handoffApplied.current) return
    let cancelled = false
    void (async () => {
      const result: HandoffResult = await redeemHandoffOnce(token)
      // `cancelled` drops StrictMode's FIRST (torn-down) invoke; `handoffApplied`
      // drops any later re-run once the live invoke has already applied. Together
      // they mean the session install + greeting happen exactly once.
      if (cancelled || handoffApplied.current) return
      handoffApplied.current = true
      if (result.ok && result.session_id) {
        sessionId.current = result.session_id
        storeSession(result.session_id)
        needsResume.current = false
        setHydrating(false)
        if (result.kickoff) {
          send(result.kickoff, undefined, undefined, undefined, {
            kickoff: true,
            lang: result.lang,
          })
        }
      } else {
        // Bad/stale link: start fresh. The session_id already in place is a
        // perfectly good new one; just clear the veil so the composer is usable.
        setHydrating(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [send])

  // A showroom card tap. Settle the row, then say it in words.
  //
  // ORDER MATTERS: the same guards `send` applies are checked FIRST, because
  // settling a row whose send then early-returns would kill a live offer and
  // leave the customer with nothing to tap.
  //
  // The phrasing is the customer's own language, so the transcript stays
  // coherent in an Arabic chat (LOC-01) — the same thing App does for the
  // dialog's "Book test drive". The branch NAME is Latin either way; that is
  // normal Gulf usage and `bdi` keeps it from reordering the line.
  const selectShowroom = useCallback(
    (showroom: ShowroomChoice, rowId: string) => {
      if (streaming || statusRef.current === 'closed') return
      // The row's OWN direction, not the document's and not the branch name's
      // (which is Latin in both languages) — it was stamped from the turn that
      // drew the cards, so it is the language the customer is actually speaking.
      const row = messages.find((m) => m.kind === 'showrooms' && m.id === rowId)
      const rtl = row?.kind === 'showrooms' && row.dir === 'rtl'

      setMessages((prev) =>
        prev.map((m) =>
          m.kind === 'showrooms' && m.id === rowId
            ? { ...m, selectedShowroomId: showroom.showroomId }
            : m,
        ),
      )
      send(rtl ? `أرغب في زيارة ${showroom.name}` : `I'd like to visit ${showroom.name}`)
    },
    [send, streaming, messages],
  )

  /** A tapped option chip: settle the row and send the label as an ordinary turn.
   *
   *  THE LABEL IS THE WHOLE MESSAGE. Nothing hidden rides along — no id, no
   *  structured answer — so this is byte-for-byte what the customer would have
   *  got by typing it. That is what makes ignoring the chips free, and it is the
   *  deliberate difference from selectSlot, where prose and a trusted slot_id
   *  travel together.
   *
   *  No direction juggling either, unlike selectShowroom above: Aisha wrote these
   *  labels in the language she is speaking, so the text is already right and
   *  `send` sniffs its direction from the text as it does for anything typed.
   */
  const selectOption = useCallback(
    (label: string, rowId: string) => {
      if (streaming || statusRef.current === 'closed') return
      setMessages((prev) =>
        prev.map((m) => (m.kind === 'options' && m.id === rowId ? { ...m, selectedLabel: label } : m)),
      )
      // Retire the offer the moment it is answered. A new question this turn
      // arms its own row when it flushes (queueRow), so this never strands the
      // next set of chips.
      setActiveOptionsId(null)
      send(label)
    },
    [send, streaming],
  )

  /** A recorded voice note: upload, transcribe, then send as an ordinary turn.
   *
   *  TWO STEPS, NOT ONE, and the split is the design (see api.transcribeVoice).
   *  Transcription is a bounded call that either works or does not; chat is a
   *  streaming agent loop. Kept apart, the customer's own words appear under the
   *  player the moment they are known, while Aisha is still thinking. Folded
   *  together, a vendor hiccup would look like a broken chat.
   *
   *  EVERY OUTCOME STILL PRODUCES A TURN, because the customer really did speak
   *  and a message that silently evaporates is the worst of the options:
   *    • transcribed        -> normal turn, transcript as the text, player above
   *    • stored, not heard  -> empty turn carrying the player; Aisha is told a
   *                            recording arrived that we could not make out, and
   *                            answers in her own words and the thread's language
   *    • never reached us   -> nothing is sent; the recording never existed
   *                            server-side, so there is nothing honest to show */
  const sendVoice = useCallback(
    async (blob: Blob, _mime: string, durationMs: number) => {
      if (streaming || statusRef.current === 'closed') return
      setTranscribing(true)
      // The customer is mid-interaction: hold the idle ladder off while a
      // recording uploads, or a slow connection could let the 2-minute nudge
      // fire at someone who is actively waiting on us.
      nudgedRef.current = false
      setLastActivityAt(Date.now())
      try {
        const result = await transcribeVoice(sessionId.current, blob, durationMs)
        // TRANSPORT FAULT — we never reached our own backend. No recording was
        // stored, so there is no audio_id to point a player at and no turn to
        // send: fabricating one would put a bubble in the transcript with
        // nothing behind it, and the backend would have no record of it at all.
        //
        // But saying NOTHING is not the honest alternative, it is just a quieter
        // failure — the customer taps stop, the indicator vanishes, and their
        // message evaporates with no explanation. So: a client-side system line,
        // in the thread's own language. It is ours, not Aisha's (the model was
        // never reached), which is the same reasoning as the idle-ladder lines,
        // and it renders muted rather than in her voice.
        if (!result) {
          setMessages((prev) => {
            const lang = conversationLang(prev)
            return [
              ...prev,
              {
                id: crypto.randomUUID(),
                kind: 'text',
                role: 'system',
                text: VOICE_LABELS[lang].sendFailed,
                streaming: false,
                lang,
              },
            ]
          })
          return
        }
        if (result.ok && result.text) {
          send(result.text, undefined, undefined, {
            audioId: result.audio_id ?? undefined,
            durationMs,
          })
          return
        }
        send('', undefined, undefined, {
          audioId: result.audio_id ?? undefined,
          durationMs,
          failed: true,
        })
      } finally {
        setTranscribing(false)
      }
    },
    [send, streaming],
  )

  // --- voice call (voice phase) ------------------------------------------- #
  // Refresh the thread FROM the server — the call renders nothing live (the
  // overlay owns the screen), so when it ends the transcript the backend was
  // writing all along is fetched once and painted whole. Reuses the resume
  // machinery: same fetch, same card/anchor rebuild, same closed handling.
  const refreshThread = useCallback(async (): Promise<number> => {
    const convo = await fetchConversation(sessionId.current)
    if (!convo || convo.turns.length === 0) return 0
    const rebuilt = await rebuildTurns(convo.turns, sessionId.current)
    setMessages(rebuilt)
    if (convo.status === 'closed') {
      // Aisha signed off ON the call (rule 19a): the session is closed
      // server-side, and the composer must say so rather than invite a
      // message into a chat that ended.
      statusRef.current = 'closed'
      setStatus('closed')
    }
    return convo.turns.length
  }, [])

  const callEnded = useCallback(
    (reason?: string) => {
      setInCallState(false)
      nudgedRef.current = false
      setLastActivityAt(Date.now())
      void (async () => {
        // The summary row is written by the server's own teardown a beat
        // AFTER call_ended reaches us, so one immediate fetch races it and
        // loses — the "Voice call — 2:14" line then only appeared at the
        // next incidental repaint (live 2026-07-31). Poll briefly until the
        // thread GROWS; a call that left no row (nothing happened) stops at
        // the bounded end.
        const before = await refreshThread()
        for (let attempt = 0; attempt < 4; attempt++) {
          await new Promise((resolve) => setTimeout(resolve, 350))
          const after = await refreshThread()
          if (after > before) break
        }
        // The end-reason line lands AFTER the refreshes or a repaint would
        // wipe it. Endings the customer did not choose get a stated cause —
        // the voice-note sendFailed pattern: ours, not Aisha's, muted, in the
        // thread's own language. Reasons with no mapped line (hangup, closed,
        // session_closed) end silently on purpose.
        const line = reason ? CALL_END_LINES.en[reason] : undefined
        if (line) {
          setMessages((prev) => {
            const lang = conversationLang(prev)
            return [
              ...prev,
              {
                id: crypto.randomUUID(),
                kind: 'text',
                role: 'system',
                text: CALL_END_LINES[lang][reason as string] ?? line,
                streaming: false,
                lang,
              },
            ]
          })
        }
      })()
    },
    [refreshThread],
  )

  /** Tell the idle ladder a call is live. Same contract as setRecording:
   *  suppressed while true, stamped on the way out so the call's minutes are
   *  never retroactively counted as silence. */
  const setInCall = useCallback((active: boolean) => {
    setInCallState(active)
    nudgedRef.current = false
    setLastActivityAt(Date.now())
  }, [])

  /** Tell the idle ladder the microphone is open (or has just closed).
   *
   *  Stamping activity on the way OUT is the half that matters: the ladder is
   *  suppressed while recording, so when it resumes it must resume from a fresh
   *  anchor. Without the stamp, a customer who recorded for a minute would have
   *  that minute counted as silence the moment they stopped — and could be
   *  nudged, or closed, seconds after speaking. Stamped on the way IN too, so a
   *  cancelled recording (which emits nothing) still counts as having been
   *  there. */
  const setRecording = useCallback((active: boolean) => {
    setRecordingState(active)
    nudgedRef.current = false
    setLastActivityAt(Date.now())
  }, [])

  /** Tell the idle ladder a message is being read aloud. Same contract as
   *  setRecording: suppressed while true, and stamped on the way out so the
   *  minute spent listening is not retroactively counted as silence. */
  const setSpeaking = useCallback((active: boolean) => {
    setSpeakingState(active)
    nudgedRef.current = false
    setLastActivityAt(Date.now())
  }, [])

  const stop = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  // The LIVE session id, resolved at call time — for consumers that connect
  // AFTER a rotation in the same tick (the voice call, 2026-07-29). The plain
  // `sessionId` return below is a render snapshot: correct for rendering,
  // stale for anything that dials out between a newChat() and the re-render.
  const getSessionId = useCallback(() => sessionId.current, [])

  // Rule 19: a closed session is ENDED, not resumable. A fresh session_id is the
  // whole point — it is the checkpointer's thread_id, so a new id opens a new
  // thread and the closed conversation's history is never replayed into it.
  // Reusing the id would resume a chat the customer just said goodbye to.
  const newChat = useCallback(() => {
    abortRef.current?.abort() // belt and braces: nothing should be in flight
    abortRef.current = null
    // CLOSE THE OLD ONE FIRST. Same service as the agent's close_conversation
    // tool, so a New chat pressed while a slot is held RELEASES that slot
    // instead of stranding it for the full lock TTL — a New chat must not cost
    // inventory any more than an idle close does. Fire-and-forget: it is the
    // previous conversation's business, and it must never delay this one
    // starting (the lock TTL remains the backstop if the request fails).
    void closeConversation(sessionId.current)
    rotateSession()
    statusRef.current = 'active'
    nudgedRef.current = false
    setStatus('active')
    setMessages([])
    setActiveSlotsId(null)
    setWorking(null)
    setStreaming(false)
    setHydrating(false)
    setLastActivityAt(Date.now())
  }, [rotateSession])

  // The live showroom offer: the NEWEST showrooms row, and only while nothing has
  // been picked from it. Derived, not stored — see the UseChat comment.
  const activeShowroomsId = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.kind !== 'showrooms') continue
      return m.selectedShowroomId == null ? m.id : null
    }
    return null
  })()

  return {
    messages,
    streaming,
    working,
    activeSlotsId,
    activeOptionsId,
    selectOption,
    send,
    activeShowroomsId,
    selectShowroom,
    stop,
    status,
    newChat,
    hydrating,
    sendVoice,
    transcribing,
    setRecording,
    setSpeaking,
    sessionId: sessionId.current,
    getSessionId,
    callEnded,
    setInCall,
  }
}
