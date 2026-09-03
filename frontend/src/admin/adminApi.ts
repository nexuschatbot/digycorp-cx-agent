// Console API client — the admin panel's only network layer.
//
// Mirrors src/api.ts in shape (native fetch, no library, `baseUrl` escape hatch
// so a Node harness can point at an absolute origin) but differs from it in ONE
// deliberate way, and the difference is the reason this is a separate module
// rather than more functions in api.ts:
//
//   api.ts COLLAPSES FAILURE TO null. That is correct there: a card whose
//   vehicle no longer resolves must be DROPPED rather than drawn as a shell
//   (FR-CX-07, rule 18), because a customer must never see a half-rendered
//   claim about a car. Silence is the honest outcome.
//
//   HERE, SILENCE WOULD BE THE DISHONEST OUTCOME. An operator looking at an
//   empty Bookings table needs to know whether there are genuinely no bookings
//   or whether the backend is down — those are opposite facts and a null makes
//   them identical. Rule 16 ("never show fake dashboard data") cuts both ways:
//   rendering an empty table for a failed request is showing a count of zero
//   that nothing measured. So these return a Result and the UI states which
//   case it is.
//
// ⚠️ The console is UNGATED (decision 2026-07-21). There is no token to attach
// here, and /customers and /bookings return names and +971 mobiles. If a gate
// lands, this module is one of its two insertion points — every console request
// goes through `request` below.

/** The paginated envelope every data-browser endpoint returns. The three
 *  CONTRACTED endpoints (audit, metrics, notifications) deliberately do NOT use
 *  it — their shapes are signed in CLAUDE.md and are not ours to reshape. */
export interface Envelope<T> {
  rows: T[]
  total: number
  limit: number
  offset: number
}

export type Result<T> = { ok: true; data: T } | { ok: false; error: string }

export interface AdminOptions {
  /** Absolute origin for non-browser callers. The browser leaves it unset and
   *  goes through the Vite dev proxy, exactly as src/api.ts does. */
  baseUrl?: string
  signal?: AbortSignal
}

export type QueryParams = Record<string, string | number | undefined | null>

function qs(params: QueryParams): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
  return parts.length ? `?${parts.join('&')}` : ''
}

/** One request, one place. Every console fetch funnels through here so error
 *  handling, and any future auth header, exist once. */
async function request<T>(
  path: string,
  params: QueryParams,
  opts: AdminOptions,
): Promise<Result<T>> {
  try {
    const res = await fetch(`${opts.baseUrl ?? ''}/api/console${path}${qs(params)}`, {
      signal: opts.signal,
    })
    if (!res.ok) {
      // The status is the useful half of the message for an operator: a 404
      // means the router is not mounted, a 500 means the query broke. Both are
      // actionable, and neither should read as "no data".
      return { ok: false, error: `Request failed (HTTP ${res.status})` }
    }
    return { ok: true, data: (await res.json()) as T }
  } catch (err) {
    // An aborted request is a navigation, not a fault — the caller discards it.
    if (err instanceof DOMException && err.name === 'AbortError') {
      return { ok: false, error: 'aborted' }
    }
    return { ok: false, error: 'Could not reach the backend' }
  }
}

/** The console's only WRITE (2026-07-23). Separate from `request` rather than a
 *  method flag on it, because the two differ in what a non-2xx MEANS: a failed
 *  GET is "we could not show you this", a failed POST is "your change did not
 *  happen", and an operator must never be left believing it did.
 *
 *  The backend answers a rejected change with 200 + `{error}` (a cancelled
 *  booking is a refusal, not a transport fault), so BOTH are unwrapped here and
 *  surfaced the same way — the caller branches on one Result, never on two. */
async function post<T>(
  path: string,
  body: unknown,
  opts: AdminOptions = {},
): Promise<Result<T>> {
  try {
    const res = await fetch(`${opts.baseUrl ?? ''}/api/console${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: opts.signal,
    })
    const data = (await res.json().catch(() => null)) as
      | (T & { error?: string; detail?: string })
      | null
    if (!res.ok) {
      return { ok: false, error: data?.detail ?? `Request failed (HTTP ${res.status})` }
    }
    if (data && typeof data === 'object' && 'error' in data && data.error) {
      return { ok: false, error: data.detail ?? data.error }
    }
    return { ok: true, data: data as T }
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      return { ok: false, error: 'aborted' }
    }
    return { ok: false, error: 'Could not reach the backend' }
  }
}

export const isAbort = (r: Result<unknown>): boolean =>
  !r.ok && r.error === 'aborted'

// --------------------------------------------------------------------------- //
// Row types — mirror services/console.py exactly.
// --------------------------------------------------------------------------- //
/** One purchasable trim. Mirrors console._variant_row. */
export interface VehicleVariantRow {
  variant_id: number
  /** NULL means "the only version", NOT "unknown" — several models ship one
   *  trim the workbook never names. Render the model name alone, never "None". */
  name: string | null
  /** All nullable together: the G700 Top Fire is coming soon and has none. */
  price_aed: number | null
  insurance_aed: number | null
  registration_aed: number | null
  /** Price + insurance + registration, or null unless ALL THREE are present —
   *  a partial sum would land below the list price and read as a discount. */
  on_road_aed: number | null
  seats_min: number | null
  seats_max: number | null
  km_per_l_min: number | null
  km_per_l_max: number | null
  /** NOT the same as sold out and NOT the same as a missing price. It is the
   *  answer to "why won't Aisha quote this car?", so it must be visible. */
  coming_soon: boolean
}

export interface VehicleRow {
  vehicle_id: number
  model_code: string | null
  brand: string | null
  name: string | null
  body_type: string | null
  seats_min: number | null
  seats_max: number | null
  /** Both nullable, and null means NOT PUBLISHED — never zero. The UI must
   *  render an absence, not a price of nothing (FR-CX-07). */
  price_aed_min: number | null
  price_aed_max: number | null
  fuel_type: string | null
  showroom_ids: number[]
  headline_features: string[]
  headline_features_ar: string[]
  images: string[]
  /** Browser-ready URL derived server-side from the stored seed path (the same
   *  derivation the chat's vehicle cards use). Null when the row has no image —
   *  render the absence, never a broken <img>. */
  image_url: string | null
  /** Ordered by PRICE, never by name — the T1's Luxury is its 4WD range-topper
   *  and costs more than its Flagship. Unpriced sorts last. */
  variants: VehicleVariantRow[]
  /** Model-level palette. NOT paired with the exterior colour and NOT stocked —
   *  it answers "which interiors exist", never "is that combination available". */
  interior_colours: string[]
}

/** One entry in the human-callback queue. Mirrors console._handoff_row. */
export interface HandoffRow {
  audit_id: number
  ts: string | null
  session: string
  reason: string
  /** The customer's OWN WORDS for what they want a person for ("discount on the
   *  T2"). Shown so the operator knows what they are calling about; it is never
   *  spoken back to a customer as policy (rule 13). */
  topic: string | null
  customer_id: number | null
  customer_name: string | null
  customer_mobile: string | null
  /** Human takeover (2026-08-31): who holds this conversation live, if anyone.
   *  Null until an admin takes it over; then their display name, so the queue
   *  shows a claimed row as claimed — and by whom. */
  taken_over_by: string | null
  taken_over_ts: string | null
  /** The conversation is still open. A closed chat has nobody in it to talk
   *  to, so the queue offers no live takeover on it — callback only. */
  session_active: boolean
}

/** One step of one multi-agent turn. Mirrors orchestrator/run.trace_steps. */
export interface RoutingStep {
  agent: string | null
  status: string
  /** How the agent was chosen: 'fast_lane' (free, instant) or 'judge' (Haiku). */
  layer: string | null
  score: number | null
  /** Gap to the runner-up. The fast lane only routes above MIN_MARGIN. */
  margin: number | null
  latency_ms: number
  detail: string | null
}

/** One turn's routing detail. Mirrors console._routing_row. */
export interface RoutingRow {
  trace_id: string
  session: string
  ts: string | null
  user_message: string | null
  detected_lang: string | null
  routed_to: string | null
  previous_agent: string | null
  router_latency_ms: number | null
  latency_ms: number | null
  /** EMPTY on every turn that ran single-agent, which is all of them until the
   *  kill switch flips. Empty means "not applicable", never "it failed". */
  steps: RoutingStep[]
  step_count: number
  fast_lane_steps: number
  judged_steps: number
  failed_steps: number
  prompt_versions: Record<string, string> | null
}

export interface RoutingSummary {
  turns: number
  steps: number
  fast_lane: number
  judged: number
  /** THE NUMBER THIS VIEW EXISTS FOR. Calibration predicted ~1/3 on 32 synthetic
   *  utterances; this is what real briefs do. Far lower means the fast lane is
   *  not earning its complexity. */
  fast_lane_share: number | null
  failed: number
  by_agent: Record<string, number>
}

export interface ShowroomRow {
  showroom_id: number
  brand: string | null
  name: string | null
  city: string | null
  emirate: string | null
  address: string | null
  address_ar: string | null
  map_link: string | null
  phone: string | null
  hours_by_day: Record<string, unknown>
  hours: string | null
}

export interface SlotRow {
  slot_id: number
  start: string | null
  end: string | null
  status: string | null
  location_type: string | null
  vehicle_id: number | null
  vehicle_name: string | null
  variant_name: string | null
  showroom_id: number | null
  showroom_name: string | null
  service_area: string | null
  /** The lock's own fields. Surfaced to an OPERATOR only — rule 14's corollary
   *  forbids the AGENT ever learning a lock's timing, because a model told when
   *  something expires will eventually pressure a customer with it. */
  locked_by_session: string | null
  locked_at: string | null
  licence_ack: boolean
}

/** The four survey scores. NULL means NOT ANSWERED, and must render as an
 *  em-dash rather than a zero — a zero is a score nobody gave, and it would drag
 *  the average down with it. */
export interface FeedbackScores {
  overall: number | null
  ease: number | null
  vehicle: number | null
  team: number | null
}

/** One post-drive feedback survey (2026-07-26), conducted over WhatsApp.
 *
 *  EMBEDDED in the booking row rather than fetched per-ref: the collapsed row
 *  shows an average-score badge, so the list needs every survey anyway and a
 *  separate endpoint would mean one request per visible booking.
 *
 *  ⚠️ `comment` is the customer's own prose and can identify them or a member of
 *  staff. It is deleted by a rule 12 erasure along with the rest of the survey. */
export interface FeedbackSummary {
  booking_ref: string
  /** queued | pending | sent | in_progress | completed | skipped | expired |
   *  abandoned. The four terminal values stay distinct on purpose: "they told us
   *  it was a 2", "they never replied" and "they asked us to stop" are different
   *  facts, and collapsing them makes the response rate unreadable. */
  status: string
  lang: string
  step: number
  scores: FeedbackScores
  /** How many of the four were answered — so partial surveys read honestly. */
  answered: number
  /** Over the ANSWERED questions only; null when none have been. */
  average: number | null
  comment: string | null
  created_at: string | null
  sent_at: string | null
  closed_at: string | null
  updated_at: string | null
}

/** ⚠️ Carries PII: customer name and +971 mobile, ungated. */
export interface BookingRow {
  booking_id: number
  ref: string
  status: string | null
  /** The SECOND axis (2026-07-23): `status` says whether the booking is alive,
   *  `attendance` says whether the customer turned up. Independent — a
   *  completed drive is still a `confirmed` booking. */
  attendance: string | null
  /** Set when the no-show follow-up was written, so an operator can see the
   *  message has already gone. The row would otherwise look identical before
   *  and after, and someone would chase the customer twice. */
  noshow_notified_at: string | null
  /** The post-drive survey, or null when the drive was never marked completed. */
  feedback: FeedbackSummary | null
  /** When the survey was OPENED. Present even after a rule 12 erasure has
   *  removed the survey itself, so an operator can still tell "never surveyed"
   *  from "surveyed, then erased" — without it the booking would look as though
   *  we never asked. */
  feedback_requested_at: string | null
  customer_id: number | null
  customer_name_en: string | null
  customer_name_ar: string | null
  customer_mobile: string | null
  driver_name: string | null
  driver_relationship: string | null
  vehicle_id: number | null
  vehicle_name: string | null
  variant_name: string | null
  slot_id: number | null
  start: string | null
  end: string | null
  location: string | null
  /** The branch, reached through the slot — a Booking has no showroom of its
   *  own, because where a drive happens is a property of the slot that was
   *  locked. All keys are always present; the showroom set is null for a
   *  doorstep booking and `service_area` is null for a showroom one. */
  showroom_id: number | null
  showroom_name: string | null
  showroom_city: string | null
  showroom_emirate: string | null
  showroom_address: string | null
  showroom_address_ar: string | null
  showroom_phone: string | null
  showroom_map_link: string | null
  service_area: string | null
  doorstep_address: string | null
  companions: string | null
  child_seat: boolean
  consultant_pref: string | null
  reason_code: string | null
  created_at: string | null
  updated_at: string | null
}

/** ⚠️ Carries PII: name, +971 mobile, email, ungated. */
export interface CustomerRow {
  customer_id: number
  title: string | null
  title_ar: string | null
  name_en: string | null
  name_ar: string | null
  mobile: string | null
  email: string | null
  emirate: string | null
  nationality: string | null
  language_pref: string | null
  memory_consent: boolean
  memory_consent_at: string | null
  whatsapp_consent: boolean
  whatsapp_consent_at: string | null
  winback_flag: boolean
  /** WHETHER a profile exists, never its contents — recall is consent-gated on
   *  read as well as write, and a console that printed `notes` would be a
   *  second reader that forgot the gate. */
  has_memory_profile: boolean
  bookings: number
  created_at: string | null
}

export interface ConversationRow {
  session_id: string
  turns: number
  first_ts: string | null
  last_ts: string | null
  langs: string[]
  voice_turns: number
  status: string
}

export interface NotificationRow {
  id: number
  ref: string
  kind: string | null
  lang: string | null
  status: string | null
  send_at: string | null
  /** Legitimately "" until the template phase renders it — an honest empty, not
   *  a placeholder for the UI to fill with invented English copy (LOC-01). */
  body: string
  payload: Record<string, unknown>
}

export interface AuditRow {
  id: number
  ts: string | null
  session: string
  actor: string | null
  type: string | null
  payload: Record<string, unknown>
}

export interface Metrics {
  conversations: number
  turns_by_lang: Record<string, number>
  bookings: Record<string, number>
  notifications_sent: number
  agent_registry: { agent: string; status: string }[]
}

// --------------------------------------------------------------------------- //
// Endpoints
// --------------------------------------------------------------------------- //

/** Contracted endpoint — real counts only, never illustrative figures (rule 16).
 *  A fresh database correctly reports zeros; that is the honest reading. */
export function fetchMetrics(opts: AdminOptions = {}): Promise<Result<Metrics>> {
  return request<Metrics>('/metrics', {}, opts)
}

// --------------------------------------------------------------------------- //
// Dashboard (2026-07-23) — its own endpoint, NOT a wider /metrics.
// --------------------------------------------------------------------------- //
/** The rolling windows the backend accepts. An unknown value falls back to the
 *  default server-side, and the applied window comes back in the payload — so
 *  the UI renders what it GOT, never what it asked for. */
export type DashboardWindow = '7d' | '30d' | '90d' | 'all'

export interface RankedVehicle {
  vehicle_id: number
  name: string
  bookings: number
}

/** A branch OR a doorstep service area — they rank together, because doorstep
 *  is a real way to take a test drive (FR-SL-07) and dropping it would make the
 *  panel silently describe a subset of the bookings the tile above counted.
 *  `showroom_id` is null and `location_type` is 'doorstep' on those rows; the
 *  console composes their label, since a stored "Doorstep (Dubai)" would be
 *  display text in a data field. */
export interface RankedPlace {
  showroom_id: number | null
  name: string | null
  city: string | null
  emirate: string | null
  location_type: 'showroom' | 'doorstep'
  bookings: number
}

/** Only branches WITH coordinates arrive here — a null lat/lng drops the pin
 *  server-side rather than defaulting to (0,0), which is open ocean. */
export interface MapPin {
  showroom_id: number
  name: string
  lat: number
  lng: number
  bookings: number
}

export interface Dashboard {
  window: DashboardWindow
  test_drives: {
    booked: number
    previous: number
    /** Null when there is no previous period (all time) or it was empty —
     *  "up ∞%" from a base of zero is not a fact. The UI says so in words. */
    change_pct: number | null
  }
  enquiries: {
    handled: number
    converted: number
    /** Null on an empty window: "0% of nothing converted" is a claim about a
     *  period in which nobody asked anything. */
    conversion_pct: number | null
  }
  /** Escalations are recorded now (rule 13 / FR-ES): request_human_callback logs
   *  an `escalation` audit row and the dashboard counts them. `count` is the
   *  enquiries handed to a human in the window, `resolved_without_human` the
   *  rest, and `reasons` the breakdown ({reason, count}, one entry per reason
   *  seen). A true zero (0 / 100% / []) when nothing was escalated — derived,
   *  not hardcoded. */
  handoffs: {
    count: number
    pct: number
    resolved_without_human: number
    resolved_pct: number | null
    reasons: { reason: string; count: number }[]
  }
  funnel: {
    enquiries: number
    booked: number
    completed: number
    rescheduled: number
    cancelled: number
    noshow: number
    /** Of drives with an OUTCOME (completed + noshow), so an unmarked booking
     *  does not dilute it. Null until at least one drive has been marked. */
    noshow_rate_pct: number | null
    noshow_rate_previous_pct: number | null
  }
  top_vehicles: RankedVehicle[]
  top_places: RankedPlace[]
  map_pins: MapPin[]
}

/** The dashboard for one rolling window.
 *
 *  A SEPARATE ENDPOINT from /metrics, which stays frozen at its contracted
 *  shape — see services/console.py. Every figure is a query against real
 *  activity; a fresh database reports zeros rather than a demo shape. */
export function fetchDashboard(
  window: DashboardWindow,
  opts: AdminOptions = {},
): Promise<Result<Dashboard>> {
  return request<Dashboard>('/dashboard', { window }, opts)
}

export function fetchVehicles(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<VehicleRow>>> {
  return request<Envelope<VehicleRow>>('/vehicles', params, opts)
}

export function fetchHandoffs(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<HandoffRow>>> {
  return request<Envelope<HandoffRow>>('/handoffs', params, opts)
}

export function fetchRouting(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<RoutingRow>>> {
  return request<Envelope<RoutingRow>>('/routing', params, opts)
}


export function fetchShowrooms(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<ShowroomRow>>> {
  return request<Envelope<ShowroomRow>>('/showrooms', params, opts)
}

export function fetchSlots(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<SlotRow>>> {
  return request<Envelope<SlotRow>>('/slots', params, opts)
}

export function fetchBookings(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<BookingRow>>> {
  return request<Envelope<BookingRow>>('/bookings', params, opts)
}

export type Attendance = 'pending' | 'completed' | 'noshow'

export interface AttendanceResult {
  booking: Record<string, unknown>
  changed: boolean
  /** True only when this call WROTE the no-show follow-up. False on a repeat,
   *  on a correction round-trip, and on every non-noshow value — the message
   *  fires once per booking, server-side. */
  notified: boolean
}

/** Mark a booking attended / no-show / not recorded.
 *
 *  ⚠️ THE ONE CONSOLE ACTION THAT CONTACTS A CUSTOMER. Moving a booking to
 *  `noshow` sends them a WhatsApp asking whether all is well and offering a new
 *  time. Every rule around that — cancelled bookings refused, once-only
 *  sending, the consent gate, the audit row — lives in the SERVICE, so this is
 *  a plain call and the UI cannot get any of it wrong. */
export function setBookingAttendance(
  ref: string,
  attendance: Attendance,
  opts: AdminOptions = {},
): Promise<Result<AttendanceResult>> {
  return post<AttendanceResult>(
    `/bookings/${encodeURIComponent(ref)}/attendance`,
    { attendance },
    opts,
  )
}

export function fetchCustomers(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<CustomerRow>>> {
  return request<Envelope<CustomerRow>>('/customers', params, opts)
}

export function fetchConversations(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<ConversationRow>>> {
  return request<Envelope<ConversationRow>>('/conversations', params, opts)
}

/** The console's own cross-session browser. NOT the contracted endpoint — see
 *  fetchSessionAudit below, which is. */
export function fetchAuditLog(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<Envelope<AuditRow>>> {
  return request<Envelope<AuditRow>>('/audit-log', params, opts)
}

/** CONTRACTED endpoint — the per-conversation audit drawer CLAUDE.md specifies,
 *  returning a BARE LIST. This is what the Conversations tab opens when a row is
 *  selected; the cross-session table above is a separate, additive view. */
export function fetchSessionAudit(
  sessionId: string,
  opts: AdminOptions = {},
): Promise<Result<AuditRow[]>> {
  return request<AuditRow[]>('/audit', { session_id: sessionId }, opts)
}

/** CONTRACTED endpoint — the WhatsApp simulator feed, also a BARE LIST. */
export function fetchNotifications(
  params: QueryParams = {},
  opts: AdminOptions = {},
): Promise<Result<NotificationRow[]>> {
  return request<NotificationRow[]>('/notifications', params, opts)
}
