// Slot-chip data (FR-SL-01) — from the get_available_slots tool_event. Sibling
// of cards.ts. Everything is grounded in the tool result: slot_id (identity),
// start_iso (the fact we localize from), and the location (showroom name or
// doorstep service area). Nothing invented.

import { newId } from './id'

export interface SlotChip {
  id: string
  slotId: number
  startIso: string
  locationType: 'showroom' | 'doorstep'
  location: string // showroom name, or doorstep service area — verbatim from the tool
  /** The trim this demo car is, when the model has more than one. Null for a
   *  single-version model — the seed's convention, not a missing value. */
  variantName: string | null
}

interface SlotDict {
  slot_id?: number
  start_iso?: string
  variant_name?: string | null
  location_type?: string
  showroom?: { name?: string | null } | null
  service_area?: string | null
}

// No slice here — the TOOL owns the cap (FR-SL-01: get_available_slots returns at
// most 3). The frontend renders exactly what the tool returned, so the chips and
// the agent's prose are the same set and cannot disagree. Slicing here again was
// half the original bug: the tool returned 6, this took the first 3 by time, and
// those need not be the 3 the agent actually offered — a tapped chip could pin a
// slot the customer had just rejected.
export function extractSlotChips(output: unknown): SlotChip[] {
  if (!output || typeof output !== 'object') return []
  const slots = (output as Record<string, unknown>).slots
  if (!Array.isArray(slots)) return []
  return (slots as SlotDict[])
    .map((s) => {
      const doorstep = s.location_type === 'doorstep'
      return {
        id: String(s.slot_id ?? s.start_iso ?? newId()),
        slotId: Number(s.slot_id),
        startIso: String(s.start_iso ?? ''),
        locationType: doorstep ? 'doorstep' : 'showroom',
        location: (doorstep ? s.service_area : s.showroom?.name) ?? '',
        variantName: typeof s.variant_name === 'string' && s.variant_name.trim()
          ? s.variant_name
          : null,
      } as SlotChip
    })
    .filter((c) => Number.isFinite(c.slotId) && c.startIso)
}

// Parse the naive ISO wall-clock and format via Intl. No timeZone override: the
// parse and the format both use the browser's zone, so they cancel and the stored
// wall-clock is shown as-is regardless of where the viewer is (UAE demo-correct).
function fmt(startIso: string, dir: 'rtl' | 'ltr', opts: Intl.DateTimeFormatOptions): string {
  const d = new Date(startIso)
  if (Number.isNaN(d.getTime())) return startIso
  const locale = dir === 'rtl' ? 'ar' : 'en-GB'
  return new Intl.DateTimeFormat(locale, opts).format(d)
}

// Display label for the chip: "Monday, 20 July · 11:00 AM" (en) / Arabic weekday
// + Arabic-Indic digits (ar), plus the location.
export function slotChipLabel(chip: SlotChip, dir: 'rtl' | 'ltr'): { when: string; where: string } {
  const day = fmt(chip.startIso, dir, { weekday: 'long', day: 'numeric', month: 'long' })
  const time = fmt(chip.startIso, dir, { hour: 'numeric', minute: '2-digit', hour12: true })
  return { when: `${day} · ${time}`, where: chip.location }
}

/** The chip's own label: THE TIME AND NOTHING ELSE ("5:00 pm"). The day and the
 *  place moved up to the group heading (25 Jul 2026), so a row of times reads as
 *  one decision — "which hour?" — rather than three near-identical sentences. */
export function slotTimeLabel(chip: SlotChip, dir: 'rtl' | 'ltr'): string {
  return fmt(chip.startIso, dir, { hour: 'numeric', minute: '2-digit', hour12: true })
}

/** The pill's text: the hour, plus the trim WHEN THE TRIM IS WHAT VARIES.
 *
 *  FOUND IN A BROWSER, 2026-09-03. Inventory became per-trim, so a branch that
 *  used to hold one G700 at 5pm now holds three — Comfort, Luxury and Flagship —
 *  and a row that shows only the hour drew "5:00 pm  5:00 pm  5:00 pm". Three
 *  identical pills, three different cars, AED 71,450 apart. The heading carries
 *  the day and the place for exactly this reason (see groupSlotChips) and the
 *  pill has always carried "the one thing that varies within a group"; on this
 *  data that is no longer only the hour.
 *
 *  ONLY WHEN IT DISAMBIGUATES. Adding the trim to every pill would make the
 *  common row — one trim, three times — longer to read for nothing. */
export function slotPillLabel(
  chip: SlotChip,
  dir: 'rtl' | 'ltr',
  trimVaries: boolean,
): string {
  const time = slotTimeLabel(chip, dir)
  return trimVaries && chip.variantName ? `${time} · ${chip.variantName}` : time
}

/** One group of chips: a day AT A PLACE, with the times offered there.
 *
 *  GROUPED BY DAY **AND** LOCATION, not by day alone. The mockup heads each
 *  group with a date only, which is right whenever the offered slots share a
 *  showroom — the usual case. But the tool may legitimately return three slots
 *  across two branches, and a bare "Sunday, 26 July" over a row of times would
 *  then let a customer tap 7:00 pm believing it is the branch they just named.
 *  The chip no longer carries the place, so the heading must (rule 2). Where all
 *  the slots really are in one place, the heading reads exactly as drawn plus
 *  the branch, and nothing is invented either way. */
export interface SlotGroup {
  key: string
  day: string
  where: string
  locationType: 'showroom' | 'doorstep'
  chips: SlotChip[]
}

export function groupSlotChips(chips: SlotChip[], dir: 'rtl' | 'ltr'): SlotGroup[] {
  const groups: SlotGroup[] = []
  for (const chip of chips) {
    // The calendar day, taken from the ISO string itself rather than a formatted
    // label — two locales must not produce two groups for one day.
    const dayKey = chip.startIso.slice(0, 10)
    const key = `${dayKey}|${chip.locationType}|${chip.location}`
    const existing = groups.find((g) => g.key === key)
    if (existing) {
      existing.chips.push(chip)
      continue
    }
    groups.push({
      key,
      day: fmt(chip.startIso, dir, { weekday: 'long', day: 'numeric', month: 'long' }),
      where: chip.location,
      locationType: chip.locationType,
      chips: [chip],
    })
  }
  return groups
}

// The id-FREE human prose sent on tap (the id travels in the hidden slot_id
// field). English carrier, following the View-more precedent; Arabic-in-Arabic is
// a later persona-phase addition.
export function slotPickText(chip: SlotChip): string {
  const when = fmt(chip.startIso, 'ltr', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  })
  // The trim rides along whenever there is one: the tap SENDS this sentence, and
  // "I'll take Thursday 3 September at 5:00 pm at Jetour Al Quoz" names one of
  // three different cars once inventory is per trim.
  const car = chip.variantName ? ` in the ${chip.variantName}` : ''
  return chip.locationType === 'doorstep'
    ? `I'll take ${when}${car} — a doorstep drive in ${chip.location}.`
    : `I'll take ${when}${car} at ${chip.location}.`
}
