// SSE client for POST /api/chat (AD-B / AD-E / AD-K).
//
// Framework-free ON PURPOSE: the exact `streamChat` the Stop button's onClick
// calls is also importable in Node for the transport harness — no React, no DOM,
// only native fetch + ReadableStream + AbortController + eventsource-parser.
//
// AD-K: native `fetch` is the only transport that does POST + streaming body +
// AbortController together. NOT axios (XHR, can't stream a response body) and NOT
// EventSource (GET-only, can't POST). eventsource-parser (~2 kB) frames the SSE
// and, crucially, drops comment lines — the server's ": ping" heartbeat and
// ": stream open" (AD-D) never reach us as events.

import { createParser, type EventSourceMessage } from 'eventsource-parser'

export type Lang = 'en' | 'ar'

export interface TokenEvent {
  text: string
}
export interface ToolEvent {
  tool: string
  input: unknown
  output: unknown
}
export interface BookingEvent {
  booking_ref: string
}
/** Rule 17: a BUBBLE BOUNDARY — seal the bubble streamed so far and start the
 *  next tokens in a fresh one. Payload-free; the fact IS the event. Fired the
 *  instant the model starts a tool_use block, so text written before the tool
 *  call cannot run together with the answer that follows it.
 *
 *  IT DELETES NOTHING. Its predecessor (`text_reset`) emptied the live bubble,
 *  on the assumption that pre-tool text is always narration — which erased a
 *  completed booking recap and a farewell, each written in the same message that
 *  called a tool with nothing following to re-say it. Splitting instead of
 *  deleting fixes the run-on without ever taking back words the customer read. */
export interface TextBreakEvent {}
/** Rule 19: the agent said goodbye and closed the chat. Payload-free on purpose —
 *  the fact IS the event. Fired by the backend only on a genuine close (never on
 *  an idempotent replay), so the UI can treat it as the one-way transition it is. */
export interface ClosedEvent {}
export interface DoneEvent {
  lang: Lang
  /** The stored id of the agent turn this stream just produced (read-aloud,
   *  2026-07-21). What the speaker icon points at: the client asks the server to
   *  read TURN N, and the server looks Aisha's words up itself — so everything
   *  spoken in her voice is provably something she said, and this endpoint can
   *  never be used to synthesise arbitrary text. Absent on older backends. */
  turn_id?: number
}
export interface ErrorEvent {
  message: string
}

export interface StreamHandlers {
  onToken?: (e: TokenEvent) => void
  onToolEvent?: (e: ToolEvent) => void
  onBooking?: (e: BookingEvent) => void
  onTextBreak?: (e: TextBreakEvent) => void
  onClosed?: (e: ClosedEvent) => void
  onDone?: (e: DoneEvent) => void
  onError?: (e: ErrorEvent) => void
  /** Fired when the caller aborts via the signal (Stop button). The client-side
   *  confirmation that the reader terminated — distinct from a server error. */
  onAborted?: () => void
}

export interface StreamOptions {
  /** Absolute origin for non-browser callers (the Node harness). The browser
   *  leaves this unset and uses the relative path through the Vite dev proxy. */
  baseUrl?: string
  /** The exact slot_id from a tapped slot chip (Option B). Sent as a separate
   *  request field so the visible message stays clean human prose; the backend
   *  pins lock_slot to it. Omitted for typed turns — backward compatible. */
  slotId?: number
  /** The recording this turn was SPOKEN as (voice notes, 2026-07-21), already
   *  stored and transcribed by transcribeVoice below. `message` is then the
   *  transcript — an ordinary string. Stamped onto the stored turn so a reload
   *  can rebuild the player; never seen by the model. */
  audioId?: string
  /** The customer recorded, but nothing could be transcribed. `message` is then
   *  EMPTY: the backend shows Aisha a note ABOUT the failure rather than words
   *  she would otherwise have to invent, and the stored turn honestly holds a
   *  player with no text. */
  voiceFailed?: boolean
  /** WhatsApp handoff kickoff (2026-07-22): `message` is the SERVER-AUTHORED
   *  greeting directive from POST /api/whatsapp/handoff/redeem, not the
   *  customer's words — so Aisha greets FIRST when they open the link. The
   *  backend stores no customer turn for it (no customer bubble to show), and
   *  the caller likewise appends no user bubble. */
  kickoff?: boolean
}

// --------------------------------------------------------------------------- //
// Voice notes — INPUT ONLY (2026-07-21 scope override). Aisha replies in text.
// --------------------------------------------------------------------------- //
/** The transcribe envelope, returned verbatim from the service — failure cases
 *  included, so the caller recovers without a second request. `ok` is the only
 *  thing to branch on: the endpoint answers 200 even when nothing could be made
 *  out, because that is a successful round trip with a disappointing answer, and
 *  the recording is still stored and still playable. */
export interface TranscribeResult {
  ok: boolean
  audio_id: string | null
  text?: string
  lang?: Lang
  duration_ms?: number | null
  reason?: string
}

/** Upload one recording and get back what was heard in it.
 *
 *  A SEPARATE ROUND TRIP from streamChat, deliberately: this is a bounded call
 *  that either works or does not, while chat is a 30-second streaming loop.
 *  Split, the customer sees their own words appear under the player while the
 *  agent is still thinking; folded together, a vendor failure would surface as a
 *  broken chat rather than a retryable recording.
 *
 *  Returns null only on a transport fault — a vendor failure comes back as a
 *  normal envelope with ok:false, which is a different thing and recovers
 *  differently (null means "we never reached our own backend"). */
export async function transcribeVoice(
  sessionId: string,
  blob: Blob,
  durationMs: number,
  signal?: AbortSignal,
  opts: StreamOptions = {},
): Promise<TranscribeResult | null> {
  const form = new FormData()
  form.append('session_id', sessionId)
  form.append('duration_ms', String(Math.round(durationMs)))
  // The filename is cosmetic here — the backend names the file for the vendor
  // from the MIME type, which is the part that must be right.
  form.append('audio', blob, 'voice')
  try {
    // NO Content-Type header: the browser must set it itself, because only it
    // knows the multipart boundary. Setting it by hand is the classic way to
    // make a multipart upload arrive unparseable.
    const res = await fetch(`${opts.baseUrl ?? ''}/api/voice/transcribe`, {
      method: 'POST',
      body: form,
      signal,
    })
    if (!res.ok) return null
    return (await res.json()) as TranscribeResult
  } catch {
    return null
  }
}

/** Playback URL for a stored recording. A URL rather than a fetch: the <audio>
 *  element streams it itself, so nothing has to buffer the blob in JS.
 *
 *  `session_id` is required — the backend releases a recording only to the
 *  session that made it, and answers an identical 404 for a wrong session, an
 *  unknown id and an expired one. A 404 means "drop the player, keep the
 *  transcript", never "render an empty shell" (FR-CX-07). */
export function audioUrl(audioId: string, sessionId: string, opts: StreamOptions = {}): string {
  return `${opts.baseUrl ?? ''}/api/audio/${encodeURIComponent(audioId)}?session_id=${encodeURIComponent(sessionId)}`
}

/** Playback URL for one of Aisha's replies, read aloud (read-aloud, 2026-07-21).
 *
 *  A URL, not a fetch: the <audio> element streams it, and the server caches the
 *  clip so a replay costs nothing. Generation happens on the first request, which
 *  is why the first play of a message takes about a second and every one after it
 *  is instant.
 *
 *  A TURN ID, NEVER THE TEXT. The server reads the words from the transcript, so
 *  nothing can put speech in Aisha's mouth that she did not write, and nobody can
 *  use this as a free text-to-speech proxy. */
export function speechUrl(turnId: number, sessionId: string, opts: StreamOptions = {}): string {
  return `${opts.baseUrl ?? ''}/api/speech/${turnId}?session_id=${encodeURIComponent(sessionId)}`
}

// --------------------------------------------------------------------------- //
// Reload-resume: the DIRECT path (no LLM), plain JSON over the same /api base.
// --------------------------------------------------------------------------- //
/** The vehicle cards one stored turn rendered (rule 18) — ids and a variant, not
 *  a frozen copy of the card. The thread resolves each id through fetchVehicle
 *  below, so a reloaded card shows live catalogue data and an Arabic thread can
 *  redraw it in Arabic. Empty on customer turns and on agent turns with no cards. */
export interface CardAnchor {
  vehicle_ids?: number[]
  variant?: 'compact' | 'full'
  /** Booking-card anchor. The union's OTHER shape — an entry carries either
   *  vehicle_ids+variant or booking_refs, never both, and readers key off which
   *  is present (see ConversationTurn.cards). Optional on both sides so a row
   *  written before booking cards existed still parses. */
  booking_refs?: string[]
}
export interface TranscriptTurn {
  /** Stored turn id — the read-aloud pointer (see DoneEvent.turn_id). */
  turn_id?: number
  /** 'system' (2026-07-29): a marker turn nobody spoke — today only the
   *  voice-call summary row ("call:<seconds>"), rendered as a muted line. */
  /** 'staff' (2026-08-31): a human operator's turn during a takeover. */
  speaker: 'customer' | 'agent' | 'system' | 'staff'
  lang: Lang
  text: string
  ts: string | null
  cards?: CardAnchor[]
  /** The voice note this turn was spoken as — the SAME KIND OF POINTER as
   *  `cards`, resolved live through audioUrl(). Null on typed turns and on every
   *  agent turn (Aisha never speaks; TTS stays out of scope). Optional so rows
   *  written before voice notes existed still parse. */
  audio_id?: string | null
}
export interface Conversation {
  session_id: string
  status: 'active' | 'closed'
  turns: TranscriptTurn[]
  /** Human takeover (2026-08-31): request_human_callback fired on this session.
   *  A reload uses it to reopen the takeover socket and hold the idle ladder —
   *  the live path learns the same fact from the tool_event. Optional so a
   *  backend predating the field still parses. */
  escalated?: boolean
  /** Who holds this session, if a person has taken it over. Also how a reload
   *  labels the staff bubbles: the transcript stores speaker 'staff' but not the
   *  name — one takeover per session (first admin wins), so the holder here
   *  names every staff turn. */
  takeover?: { taken_over: boolean; admin_name: string | null; ts: string | null }
}

/** This session's transcript + status, for rehydrating the thread after a page
 *  reload. Text plus the vehicle-card anchors each turn rendered; SLOT CHIPS are
 *  still never replayed, because a rehydrated chip would pin a slot_id that may
 *  since have been booked, while a vehicle_id cannot go stale that way. Returns
 *  null on any failure, which the caller treats as "no usable session, start
 *  fresh" — resume must never be able to block the demo from loading. */
export async function fetchConversation(
  sessionId: string,
  opts: StreamOptions = {},
): Promise<Conversation | null> {
  try {
    const res = await fetch(
      `${opts.baseUrl ?? ''}/api/conversation/${encodeURIComponent(sessionId)}`,
    )
    if (!res.ok) return null
    return (await res.json()) as Conversation
  } catch {
    return null
  }
}

/** End a conversation from the UI ("New chat"). Hits the same service as the
 *  agent's close_conversation tool, so a New chat pressed while a slot is held
 *  releases it rather than stranding inventory for the lock TTL. */
export async function closeConversation(
  sessionId: string,
  opts: StreamOptions = {},
): Promise<void> {
  try {
    await fetch(`${opts.baseUrl ?? ''}/api/conversation/${encodeURIComponent(sessionId)}/close`, {
      method: 'POST',
    })
  } catch {
    // Best effort. If it fails the slot lock's TTL is still the backstop, and
    // failing to close an abandoned session must never block starting a new one.
  }
}

/** The result of opening a WhatsApp handoff link (?h=<token>). On success it
 *  carries a FRESH session_id already bound to the customer server-side, plus the
 *  server-authored `kickoff` directive the caller fires so Aisha greets first.
 *  A stale/spent/invalid token comes back {ok:false} — the caller then just
 *  starts a normal empty chat, so a forwarded or re-opened link degrades
 *  gracefully rather than erroring. */
export interface HandoffResult {
  ok: boolean
  session_id?: string
  kickoff?: string
  lang?: 'en' | 'ar'
  reason?: string
}

/** Redeem a handoff token. The token is the ONLY credential; identity is
 *  resolved from it server-side, so the client sends nothing about who the
 *  customer is. Returns {ok:false} on any failure (network included) — never
 *  throws, because a bad link must fall back to a fresh chat, not a broken page. */
export async function redeemHandoff(
  token: string,
  opts: StreamOptions = {},
): Promise<HandoffResult> {
  try {
    const res = await fetch(`${opts.baseUrl ?? ''}/api/whatsapp/handoff/redeem`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    })
    if (!res.ok) return { ok: false, reason: `http_${res.status}` }
    return (await res.json()) as HandoffResult
  } catch {
    return { ok: false, reason: 'network' }
  }
}

export interface VehicleShowroom {
  showroom_id: number
  name: string
  emirate: string
  city: string
}

/** Full detail for the vehicle dialog. Both languages always arrive: the fetch
 *  costs nothing (no model involved) so the caller need not know the
 *  conversation's language yet. `fuel` is an ENUM, seats a raw range and prices
 *  nullable — the UI labels them per language, because a stored English string
 *  could not be shown in an Arabic dialog (LOC-01). */
export interface VehicleDetail {
  vehicle_id: number
  name: string
  body_type: string
  seats_min: number
  seats_max: number
  price_aed_min: number | null // null → unpublished; render NO price line
  price_aed_max: number | null
  fuel: 'petrol' | 'phev' | null
  image_url: string | null
  /** Interior photos cropped from the brochures. Optional: the Salesforce
   *  read-through may omit it, and several models have none — both render as
   *  simply "no gallery". */
  interior_image_urls?: string[]
  headline_features: string[]
  headline_features_ar: string[]
  /** Vehicle facts shown instead of the showroom list (2026-08-29). All
   *  optional for the same read-through reason as the interiors; a missing
   *  block simply doesn't render. */
  interior_colours?: string[]
  km_per_l_min?: number | null
  km_per_l_max?: number | null
  variants?: VehicleDialogVariant[]
  showrooms: VehicleShowroom[]
}

/** One trim row in the vehicle dialog. `name` null = the only version (it
 *  renders under the model's own name); `price_aed` null = not published —
 *  the row says "coming soon", never a number (FR-CX-07). */
export interface VehicleDialogVariant {
  variant_id: number
  name: string | null
  price_aed: number | null
  seats_min: number | null
  seats_max: number | null
  km_per_l_min: number | null
  km_per_l_max: number | null
  coming_soon: boolean
}

/** One booking, as the backend's per-booking dict (services/booking.
 *  _booking_dict). THE PERSONAL FIELDS ARE OPTIONAL BY DESIGN, not by
 *  sloppiness: customer_name_en/_ar and customer_mobile are present ONLY when
 *  the viewing session is identified as this booking's customer. Every consumer
 *  must handle their absence — that is the gate working, not a payload bug. */
export interface BookingDetail {
  booking_ref: string
  status: string
  vehicle_name: string | null
  start_iso: string | null
  end_iso: string | null
  location_type: 'showroom' | 'doorstep'
  showroom_name?: string | null
  showroom_map_link?: string | null
  doorstep_address?: string | null
  service_area?: string | null
  customer_name_en?: string | null
  customer_name_ar?: string | null
  customer_mobile?: string | null
}

/** One booking by TD reference, for rebuilding a booking card after a reload.
 *
 *  session_id NAMES THE VIEWER, it does not select the booking. `ref` alone
 *  finds it (FR-RS-01); the session decides only whether the customer's name and
 *  mobile come back, and the backend resolves it from the LangGraph checkpoint
 *  rather than trusting the query string. Sending it is therefore not a
 *  permission claim — it just lets an already-identified customer see their own
 *  details again after a refresh.
 *
 *  Returns null on 404 (unknown or cancelled) or any failure, so the card is
 *  DROPPED rather than drawn as a shell (FR-CX-07). */
export async function fetchBooking(
  ref: string,
  sessionId: string,
  opts: StreamOptions = {},
): Promise<BookingDetail | null> {
  try {
    const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
    const res = await fetch(`${opts.baseUrl ?? ''}/api/booking/${encodeURIComponent(ref)}${qs}`)
    if (!res.ok) return null
    return (await res.json()) as BookingDetail
  } catch {
    return null
  }
}

/** One car's full detail, for the dialog. THE DIRECT PATH: browsing is fully
 *  determined by the id, so this never reaches the model and costs zero tokens
 *  — tapping "View more" used to spend a whole agent turn re-fetching detail the
 *  browser had already been given.
 *
 *  Also survives a page reload, which the tool_event data cannot: the
 *  conversation GET deliberately does not replay tool_events.
 *
 *  Returns null on any failure; the dialog shows an error rather than a shell. */
export async function fetchVehicle(
  vehicleId: number,
  opts: StreamOptions = {},
): Promise<VehicleDetail | null> {
  try {
    const res = await fetch(`${opts.baseUrl ?? ''}/api/vehicles/${vehicleId}`)
    if (!res.ok) return null
    return (await res.json()) as VehicleDetail
  } catch {
    return null
  }
}

/** Lock a slot the customer TAPPED — the DIRECT path, no model involved.
 *
 *  The chip already carries slot_id, so the answer is fully determined: routing
 *  it through the agent means one model call spent emitting a tool_use for a
 *  fact the UI already had. The backend locks it, audits it as actor=customer
 *  (a person tapped — the agent did not decide), and writes the action into the
 *  agent's memory so the next turn does not re-offer a slot that is now gone.
 *
 *  Aisha STILL speaks after this: rule 8 wants the hospitable exchange right
 *  after slot acceptance. So this does not remove her turn — it removes the tool
 *  round-trip inside it, roughly halving that turn.
 *
 *  Returns the tool's own envelope, including the failure case
 *  ({locked: false, reason: "slot_no_longer_available"}) — which is not an error
 *  to swallow: the agent reads it from history and re-offers gracefully. */
export async function lockSlotIntent(
  sessionId: string,
  slotId: number,
  opts: StreamOptions = {},
): Promise<{ locked?: boolean } | null> {
  try {
    const res = await fetch(`${opts.baseUrl ?? ''}/api/intent/lock-slot`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, slot_id: slotId }),
    })
    if (!res.ok) return null
    return (await res.json()) as { locked?: boolean }
  } catch {
    // Best effort. On failure the turn still goes out carrying the pinned
    // slot_id, so the agent locks it the old way — slower, never broken.
    return null
  }
}

function safeParse(data: string): unknown {
  try {
    return JSON.parse(data)
  } catch {
    return {}
  }
}

/**
 * Open the chat stream and drive `handlers` as events arrive. Resolves when the
 * stream ends, errors, or is aborted. The SAME function the Stop button calls —
 * the only difference for the harness is `opts.baseUrl`.
 */
export async function streamChat(
  sessionId: string,
  message: string,
  handlers: StreamHandlers,
  signal: AbortSignal,
  opts: StreamOptions = {},
): Promise<void> {
  let res: Response
  try {
    res = await fetch(`${opts.baseUrl ?? ''}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        message,
        // Only include slot_id when a chip was tapped; typed turns omit it.
        ...(opts.slotId != null ? { slot_id: opts.slotId } : {}),
        // Only on a spoken turn — typed turns stay byte-identical to before.
        ...(opts.audioId ? { audio_id: opts.audioId } : {}),
        ...(opts.voiceFailed ? { voice_failed: true } : {}),
        // Handoff kickoff: the backend skips storing a customer turn for it.
        ...(opts.kickoff ? { kickoff: true } : {}),
      }),
      signal,
    })
  } catch (err) {
    if ((err as Error).name === 'AbortError') {
      handlers.onAborted?.()
      return
    }
    handlers.onError?.({ message: 'Could not reach the assistant. Please try again.' })
    return
  }

  if (!res.ok || !res.body) {
    handlers.onError?.({ message: `The assistant is unavailable (HTTP ${res.status}).` })
    return
  }

  const parser = createParser({
    onEvent(evt: EventSourceMessage) {
      const data = evt.data ? safeParse(evt.data) : {}
      switch (evt.event) {
        case 'token':
          handlers.onToken?.(data as TokenEvent)
          break
        case 'tool_event':
          handlers.onToolEvent?.(data as ToolEvent)
          break
        case 'booking':
          handlers.onBooking?.(data as BookingEvent)
          break
        case 'text_break':
          handlers.onTextBreak?.(data as TextBreakEvent)
          break
        case 'closed':
          handlers.onClosed?.(data as ClosedEvent)
          break
        case 'done':
          handlers.onDone?.(data as DoneEvent)
          break
        case 'error':
          handlers.onError?.(data as ErrorEvent)
          break
      }
    },
  })

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      parser.feed(decoder.decode(value, { stream: true }))
    }
  } catch (err) {
    // AbortError = the caller hit Stop; that dropped the connection, which is
    // what triggers the server-side cancel (AD-E). Expected, not an error.
    if ((err as Error).name === 'AbortError') {
      handlers.onAborted?.()
      return
    }
    handlers.onError?.({ message: 'The connection was interrupted. Please try again.' })
  }
}
