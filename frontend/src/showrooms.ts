// Showroom choices — the rows behind the "which branch?" cards, extracted from
// the get_showrooms tool_event. Mirrors cards.ts / bookings.ts.
//
// POINTERS AND RAW STRINGS, no display text assembled here. Both scripts of the
// address come over the wire (the backend ships `address` and `address_ar`,
// added 21 Jul 2026) and the card picks one at render time by the conversation's
// language — a stored English address cannot be shown in an Arabic thread
// (LOC-01), which is the same rule that keeps price and seats structured.
//
// NO CAP, unlike search_vehicles' three recommendations. "Where are you located?"
// is a legitimate question about all six branches, and silently dropping three of
// them would answer it wrongly.
//
// BUT A PLACE FILTER IS NOT A CAP, and the one rule below exists because of a
// real defect (observed 2026-07-23). Asked for "slots today in Jetour Al Quoz",
// the row rendered THREE equal cards with three equal Select buttons: the two Al
// Quoz branches and Jetour Deira, a different district entirely.
//
// The backend was not wrong and neither was the model. list_showrooms WIDENS a
// place filter to the whole emirate on purpose — for "anything in Al Ain?", the
// Abu Dhabi branch is a real alternative and hiding it would be as dishonest as
// inventing one — and it labels every row so the difference survives the trip:
//
//     match_type: "city"     this branch IS in the place they named
//     match_type: "emirate"  same emirate, somewhere else
//
// This file simply threw that label away, so a distinction the service had
// already computed never reached the screen.
//
// WE DROP THE NEARBY ONES RATHER THAN DEMOTING THEM, and the reason is what a
// card can and cannot say. "Nearby, but not the place you asked for" is a
// nuance, and a card has no way to express one — it has a Select button or it
// does not. Prose does: the tool result still carries every row, so Aisha can
// say "we also have Deira, about twenty minutes away" in her own words, which
// is where that qualification belongs (and her tool description already tells
// her to). Honesty moves to the sentence; the cards become literal.
//
// ONLY WHEN A CITY MATCH ACTUALLY EXISTS. Ask for a car that is only stocked at
// Deira and every remaining row is an "emirate" match — those ARE the answer,
// and dropping them would leave an empty row where the options should be.
// match_type is also absent entirely when no place was named, and then all six
// are peers, which is the right answer to "where are you located?".

export interface ShowroomChoice {
  /** Row key for React. */
  id: string
  /** The real showroom id. Never goes stale the way a slot_id does — a branch
   *  cannot be "taken" by another customer. */
  showroomId: number
  name: string
  address: string | null
  addressAr: string | null
  mapLink: string | null
  /** Nearest-showroom extras (present only for the 'nearest' variant): the
   *  driving distance/time to this branch and a Google Maps directions link. */
  distanceKm?: number | null
  driveMinutes?: number | null
  directionsUrl?: string | null
}

interface ShowroomDict {
  showroom_id?: number
  name?: string
  address?: string | null
  address_ar?: string | null
  map_link?: string | null
  /** "city" | "emirate", or absent when the agent named no place. Read only to
   *  decide which rows are cards; deliberately NOT carried onto ShowroomChoice,
   *  because after the filter below every surviving row is offered on equal
   *  terms and a field nothing renders would just be dead weight. */
  match_type?: string
}

/** A row that has already passed the id/name check. Named so the narrowing
 *  survives into the filter below — without it, TypeScript widens the fields
 *  back to optional the moment the guard and the use sit in different loops. */
type UsableShowroom = ShowroomDict & { showroom_id: number; name: string }

/** Pull the showroom rows out of a get_showrooms tool output. Tolerates a
 *  missing/odd payload by returning [] — a malformed result must never throw
 *  inside a live turn (rule 2's "errors as data" applied at the UI edge). */
export function extractShowroomChoices(output: unknown): ShowroomChoice[] {
  if (!output || typeof output !== 'object') return []
  const rows = (output as { showrooms?: unknown }).showrooms
  if (!Array.isArray(rows)) return []

  const usable: UsableShowroom[] = []
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const s = row as ShowroomDict
    // A row with no id or no name has nothing to show and nothing to select —
    // dropped rather than rendered as a shell (FR-CX-07).
    if (typeof s.showroom_id !== 'number' || typeof s.name !== 'string') continue
    usable.push(s as UsableShowroom)
  }

  // Keep only the branches actually in the place the customer named — but ONLY
  // when there is at least one, so a set made entirely of nearby branches is
  // still offered rather than silently emptied. See the header for why these
  // are dropped from the CARDS while staying in the tool result Aisha speaks
  // from. Applied after the shell filter above: a malformed row must not be
  // what decides whether a city match "exists".
  const kept = usable.some((s) => s.match_type === 'city')
    ? usable.filter((s) => s.match_type === 'city')
    : usable

  const out: ShowroomChoice[] = []
  for (const s of kept) {
    out.push({
      id: `showroom-${s.showroom_id}`,
      showroomId: s.showroom_id,
      name: s.name,
      address: s.address ?? null,
      addressAr: s.address_ar ?? null,
      mapLink: s.map_link ?? null,
    })
  }
  return out
}

/** Showroom choices from a nearest_showroom tool output (its `nearest` branch
 *  plus `alternatives`). Returns [] for an unresolved result — e.g. when the tool
 *  asked for a landmark — so no selectable cards render until a specific place is
 *  given. Lets the customer TAP a branch instead of typing it. */
interface NearestDict extends ShowroomDict {
  distance_km?: number | null
  drive_minutes?: number | null
  directions_url?: string | null
}

function toChoice(s: NearestDict): ShowroomChoice {
  return {
    id: `showroom-${s.showroom_id}`,
    showroomId: s.showroom_id as number,
    name: s.name as string,
    address: s.address ?? null,
    addressAr: s.address_ar ?? null,
    mapLink: s.map_link ?? null,
    distanceKm: typeof s.distance_km === 'number' ? s.distance_km : null,
    driveMinutes: typeof s.drive_minutes === 'number' ? s.drive_minutes : null,
    directionsUrl: typeof s.directions_url === 'string' ? s.directions_url : null,
  }
}

export function extractNearestShowroomChoices(output: unknown): ShowroomChoice[] {
  if (!output || typeof output !== 'object') return []
  const o = output as { resolved?: boolean; nearest?: unknown; alternatives?: unknown }
  if (o.resolved !== true) return []

  const nearest = o.nearest as NearestDict | undefined
  if (!nearest || typeof nearest !== 'object') return []
  if (typeof nearest.showroom_id !== 'number' || typeof nearest.name !== 'string') return []

  const out: ShowroomChoice[] = [toChoice(nearest)]
  // The backend already decided which branches to show (by the customer's emirate:
  // both Dubai branches, the one city branch elsewhere, or the 2 nearest when
  // their emirate has none). So render the nearest plus whatever alternatives it
  // returned — no extra filtering here.
  const alts = Array.isArray(o.alternatives) ? (o.alternatives as NearestDict[]) : []
  for (const a of alts) {
    if (!a || typeof a !== 'object') continue
    if (typeof a.showroom_id !== 'number' || typeof a.name !== 'string') continue
    if (a.showroom_id === nearest.showroom_id) continue
    out.push(toChoice(a))
  }
  return out
}
