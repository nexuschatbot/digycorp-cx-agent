// Enum → human label for the console.
//
// The API returns machine values on purpose — `al_quoz`, `phev`, `t24`,
// `abu_dhabi` — because the schema forbids storing display text in data fields:
// a stored English string cannot be redrawn in an Arabic thread, and a stored
// price goes stale. The UI is where wording gets chosen, and the customer-facing
// app already does exactly this (src/i18n.ts FUEL_LABELS, fuelLabel).
//
// The console was rendering those raw values straight onto the screen, which is
// the same mistake in the other direction: correct data, presented as though a
// human were supposed to read a database. This module is the console's half of
// that contract.
//
// UNKNOWN VALUES ARE TITLE-CASED, NOT DROPPED. If the seed grows a new emirate
// or a new notification kind, it appears as a readable guess rather than
// vanishing or showing raw — the console must not silently hide data it did not
// anticipate.

const EMIRATES: Record<string, string> = {
  dubai: 'Dubai',
  abu_dhabi: 'Abu Dhabi',
  sharjah: 'Sharjah',
  ajman: 'Ajman',
  ras_al_khaimah: 'Ras Al Khaimah',
  fujairah: 'Fujairah',
  umm_al_quwain: 'Umm Al Quwain',
  al_ain: 'Al Ain',
}

const CITIES: Record<string, string> = {
  al_quoz: 'Al Quoz',
  deira: 'Deira',
  rabdan: 'Rabdan',
  al_ain: 'Al Ain',
  ajman: 'Ajman',
  dubai: 'Dubai',
}

const FUEL: Record<string, string> = {
  petrol: 'Petrol',
  phev: 'Plug-in hybrid',
}

const LOCATION: Record<string, string> = {
  showroom: 'Showroom',
  doorstep: 'Doorstep',
}

const LANG: Record<string, string> = {
  en: 'English',
  ar: 'Arabic',
}

const NOTIFICATION_KIND: Record<string, string> = {
  confirmation: 'Confirmation',
  t24: '24h reminder',
  t2: '2h reminder',
  reschedule: 'Reschedule',
  cancellation: 'Cancellation',
  noshow: 'No-show follow-up',
  feedback: 'Feedback survey',
}

/** Feedback survey status (2026-07-26). The four terminal values get four
 *  DIFFERENT labels on purpose: an operator needs to tell "they told us it was a
 *  2" from "they never replied" from "they asked us to stop", and a survey cut
 *  short by a real booking request is not a refusal to answer. Collapsing them
 *  into "Closed" would make the response rate meaningless and would bury the one
 *  signal worth acting on. */
const FEEDBACK_STATUS: Record<string, string> = {
  queued: 'Waiting its turn',
  pending: 'Sending',
  sent: 'Asked, no reply yet',
  in_progress: 'Answering',
  completed: 'Complete',
  skipped: 'Declined',
  expired: 'No reply',
  abandoned: 'Interrupted',
}

/** The 1–5 scale, worded as the customer sees it in the WhatsApp list. */
const FEEDBACK_SCORE: Record<number, string> = {
  5: 'Excellent',
  4: 'Very good',
  3: 'Good',
  2: 'Fair',
  1: 'Poor',
}

/** The four questions, labelled for the console. Kept in step with the copy in
 *  the backend's services/feedback (Q5 — recommendation — was dropped by product
 *  decision before the survey shipped). */
export const FEEDBACK_QUESTIONS: { key: 'overall' | 'ease' | 'vehicle' | 'team'; label: string }[] = [
  { key: 'overall', label: 'Overall' },
  { key: 'ease', label: 'Ease of arranging' },
  { key: 'vehicle', label: 'The vehicle' },
  { key: 'team', label: 'The team' },
]

/** Attendance — did the customer turn up? (2026-07-23) A second axis alongside
 *  booking status, and the labels are deliberately not interchangeable with it:
 *  "Pending" here means "we have not recorded an outcome yet", NOT "awaiting
 *  confirmation", which is what an operator glancing at a status column would
 *  otherwise read into it. */
const ATTENDANCE: Record<string, string> = {
  pending: 'Not recorded',
  completed: 'Attended',
  noshow: 'No-show',
}

// Slot inventory. `open` reads as AVAILABLE to an operator scanning a calendar —
// "open" is the database's word for the row, not the showroom's word for the
// slot, and next to "Booked" it invites the wrong reading (an open QUESTION, an
// open BOOKING). The other two are already the words a person would use.
//
// Its own map rather than a case added to statusLabel: that helper is shared by
// bookings, notifications and conversations, and it title-cases by design so an
// unanticipated status still renders. Renaming a value there would reach every
// table that has ever shown one.
const SLOT_STATUS: Record<string, string> = {
  open: 'Available',
  locked: 'Locked',
  booked: 'Booked',
}

const AUDIT_TYPE: Record<string, string> = {
  intent: 'Intent',
  tool_call: 'Tool call',
  notification: 'Notification',
  consent: 'Consent',
  handoff: 'Handoff',
  // Rule 13 / FR-ES human-callback escalation. Distinct from `handoff` (the
  // WhatsApp deep-link bridge): this is Aisha offering a specialist a callback.
  escalation: 'Escalation',
  erasure: 'Erasure',
  voice: 'Voice',
}

const ACTOR: Record<string, string> = {
  customer: 'Customer',
  agent: 'Agent',
  system: 'System',
  admin: 'Admin',
}

// Human-callback escalation reasons (rule 13 / FR-ES) — the enum the backend
// ships on each escalation, labelled for the dashboard's Handoff card. Composed
// here, not stored (schema's no-display-text rule): the backend sends the slug.
const ESCALATION_REASON: Record<string, string> = {
  price_negotiation: 'Price negotiation',
  finance: 'Finance',
  complaint: 'Complaint',
  repeated_failure: 'Repeated failure',
  explicit_request: 'Asked for a person',
  safety: 'Safety',
  other: 'Other',
}

/** snake_case → Title Case, for values no table anticipated. */
export function titleise(value: string): string {
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

function lookup(table: Record<string, string>, value: string | null | undefined) {
  if (!value) return null
  return table[value] ?? titleise(value)
}

export const emirateLabel = (v: string | null | undefined) => lookup(EMIRATES, v)
export const cityLabel = (v: string | null | undefined) => lookup(CITIES, v)
export const fuelLabel = (v: string | null | undefined) => lookup(FUEL, v)
export const locationLabel = (v: string | null | undefined) => lookup(LOCATION, v)
export const langLabel = (v: string | null | undefined) => lookup(LANG, v)
export const kindLabel = (v: string | null | undefined) =>
  lookup(NOTIFICATION_KIND, v)
export const auditTypeLabel = (v: string | null | undefined) =>
  lookup(AUDIT_TYPE, v)
export const escalationReasonLabel = (v: string | null | undefined) =>
  lookup(ESCALATION_REASON, v)
export const actorLabel = (v: string | null | undefined) => lookup(ACTOR, v)
export const attendanceLabel = (v: string | null | undefined) =>
  lookup(ATTENDANCE, v)
export const feedbackStatusLabel = (v: string | null | undefined) =>
  lookup(FEEDBACK_STATUS, v)
/** The word for one score. Null stays null so the caller renders an em-dash — an
 *  unanswered question must never read as a low one. */
export const feedbackScoreLabel = (v: number | null | undefined) =>
  v == null ? null : (FEEDBACK_SCORE[v] ?? String(v))
/** Statuses are single words already; title-casing is the whole job. */
export const statusLabel = (v: string | null | undefined) =>
  v ? titleise(v) : null
/** Slot inventory status — `open` reads as "Available". See SLOT_STATUS. */
export const slotStatusLabel = (v: string | null | undefined) =>
  lookup(SLOT_STATUS, v)

/** "Al Quoz, Dubai" — the pair as a person would say it, skipping either half
 *  when it is missing rather than leaving a dangling comma. */
export function placeLabel(
  city: string | null | undefined,
  emirate: string | null | undefined,
): string | null {
  const parts = [cityLabel(city), emirateLabel(emirate)].filter(Boolean)
  return parts.length ? parts.join(', ') : null
}

/** AED with thousands separators, or null when the price is not published —
 *  which is a real business state (the agent may not quote it), never a zero. */
export function priceLabel(
  min: number | null,
  max: number | null,
): string | null {
  if (min == null && max == null) return null
  const fmt = (n: number) => `AED ${n.toLocaleString('en-US')}`
  if (min != null && max != null) {
    return min === max ? fmt(min) : `${fmt(min)} – ${fmt(max)}`
  }
  return fmt((min ?? max) as number)
}

export function seatsLabel(
  min: number | null,
  max: number | null,
): string | null {
  if (min == null && max == null) return null
  if (min != null && max != null) {
    return min === max ? `${min} seats` : `${min}–${max} seats`
  }
  return `${min ?? max} seats`
}
