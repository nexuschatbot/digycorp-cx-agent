// Display formatting for the console.
//
// UAE conventions (LOC-09) apply here even though this is an internal English
// screen: Gregorian dates WITH DAY NAMES and a 12-hour clock. The day name is
// not decoration — the whole calendar model turns on which day it is (Saturday
// and Sunday are the weekend, Friday carries the prayer window), so a slot row
// showing only "2026-07-24 12:00" makes the single most important question
// about it — what day is that? — require mental arithmetic.
//
// These format values the API already sent. They never invent, and nothing here
// is stored: the schema forbids display text in data fields precisely so the UI
// composes wording at render time.

const DATE_TIME = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  hour12: true,
})

const DATE_ONLY = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})

const TIME_ONLY = new Intl.DateTimeFormat('en-GB', {
  hour: 'numeric',
  minute: '2-digit',
  hour12: true,
})

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

export function fmtDateTime(iso: string | null | undefined): string | null {
  const d = parse(iso)
  return d ? DATE_TIME.format(d) : null
}

export function fmtDate(iso: string | null | undefined): string | null {
  const d = parse(iso)
  return d ? DATE_ONLY.format(d) : null
}

export function fmtTime(iso: string | null | undefined): string | null {
  const d = parse(iso)
  return d ? TIME_ONLY.format(d) : null
}

/** "Fri 24 Jul 2026, 7:00 – 8:30 pm" — one cell for a slot's whole window.
 *  Collapses the end date when it matches the start, which it always does for a
 *  test-drive slot but need not for anything else. */
export function fmtRange(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
): string | null {
  const start = parse(startIso)
  const end = parse(endIso)
  if (!start) return null
  if (!end) return DATE_TIME.format(start)

  const sameDay = start.toDateString() === end.toDateString()
  return sameDay
    ? `${DATE_ONLY.format(start)}, ${TIME_ONLY.format(start)} – ${TIME_ONLY.format(end)}`
    : `${DATE_TIME.format(start)} – ${DATE_TIME.format(end)}`
}

/** A session id is 36 characters and there are dozens of them; showing all of
 *  it turns every table into a wall of uuid. The head is enough to correlate a
 *  row with an audit trail by eye, and the full value stays in `title`. */
export function shortSession(id: string | null | undefined): string {
  if (!id) return ''
  return id.length > 12 ? `${id.slice(0, 8)}…` : id
}
