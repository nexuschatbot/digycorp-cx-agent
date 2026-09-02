// Vehicle card data (rule 18) — derived from the get_vehicle_details tool_event
// output. Everything here is grounded in the tool result: the image URL and price
// come from the data, never invented (FR-CX-07), and an unpublished price renders
// as no price line — never "None"/"null".

import type { VehicleDetail } from './api'
import { newId } from './id'

/** One trim row for the card's strip. Card data only — the dialog owns the
 *  full trim record. `name` null = "the only version"; `priceAed` null = not
 *  published, which the strip must render as "coming soon", never a number
 *  (FR-CX-07). */
export interface TrimRow {
  variantId: number
  name: string | null
  priceAed: number | null
}

export interface VehicleCard {
  id: string
  /** The catalogue row id, when the tool payload carried one. The dialog needs
   *  it to fetch full detail — `id` above is a React key and may fall back to a
   *  name or uuid, so it is not safe to fetch with. null → no "View more" (we
   *  would have nothing to open). */
  vehicleId: number | null
  name: string
  imageUrl: string | null // null → render the card without an image
  /** Carousel frames for the card image (hero first, then interior shots).
   *  Set ONLY when the agent passed show_interior on get_vehicle_details —
   *  absent on search cards and plain detail lookups, which stay single-image. */
  gallery?: string[]
  priceLabel: string | null // null → unpublished; the card shows no price line
  /** The tagline under the name (developer decision 25 Jul 2026). The ONE
   *  catalogue field behind that row — "Mid-size off-road SUV, 5-door". It is
   *  the exception to the no-display-text rule and a knowing one: `body_type`
   *  is stored English prose with no `_ar` twin, so an Arabic card shows an
   *  English line. Chosen over translating all nine seed rows; the row clamps
   *  to one line, so the long ones ("Full-size luxury off-road SUV
   *  (body-on-frame)") ellipsise rather than breaking the fixed card height.
   *  Null → the row is omitted, never rendered empty. */
  bodyType: string | null
  /** Spec chips (full card only). Kept as RAW seat numbers and the fuel ENUM,
   *  never as label text: the same card renders in either language, and a
   *  stored "5 seats" could not be spoken into an Arabic thread (LOC-01). The
   *  labels come from i18n's seatsLine()/fuelLabel() at render time.
   *  Null → that chip is omitted; there is no boot/engine data to chip. */
  seatsMin: number | null
  seatsMax: number | null
  fuel: string | null
  /** Trim rows (2026-09-01). Populated only when the payload carried the
   *  variant list — the FULL get_vehicle_details shape and the dialog detail
   *  do; the base dict behind search results and ambiguous candidates does
   *  not, and stays []. Whether the strip actually renders is VehicleCards'
   *  call (single full card, >1 trim), so a rehydrated card that resolves
   *  through /api/vehicles/{id} — which always has the trims — still renders
   *  identically to its live original. */
  trims: TrimRow[]
}

interface VehicleDict {
  vehicle_id?: number
  name?: string
  image_url?: string | null
  body_type?: string | null
  price_aed_min?: number | null
  price_aed_max?: number | null
  price_published?: boolean
  seats_min?: number | null
  seats_max?: number | null
  fuel?: string | null
  variants?: unknown
}

function fmt(n: number): string {
  return n.toLocaleString('en-US')
}

/** AED price line, or null when the price is not published (JET-T1-IDM). Never
 *  emits "None"/"null" — the null-price rule applies to the card too. */
export function priceLabel(v: VehicleDict): string | null {
  if (v.price_published === false) return null
  const min = v.price_aed_min ?? null
  const max = v.price_aed_max ?? null
  if (min == null && max == null) return null
  if (min != null && max != null) {
    return min === max ? `AED ${fmt(min)}` : `AED ${fmt(min)}–${fmt(max)}`
  }
  if (min != null) return `From AED ${fmt(min)}`
  return `Up to AED ${fmt(max as number)}`
}

/** Trim rows from a variants payload (grounding as ever — rule 18: rows come
 *  from the data or not at all). Tolerates the two shapes that carry them:
 *  the tool's _variant_full dicts and the dialog's VehicleDialogVariant. */
function extractTrims(raw: unknown): TrimRow[] {
  if (!Array.isArray(raw)) return []
  const rows: TrimRow[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const v = item as Record<string, unknown>
    if (typeof v.variant_id !== 'number') continue
    rows.push({
      variantId: v.variant_id,
      name: typeof v.name === 'string' && v.name.trim() ? v.name : null,
      priceAed: typeof v.price_aed === 'number' ? v.price_aed : null,
    })
  }
  return rows
}

function toCard(v: VehicleDict): VehicleCard {
  return {
    id: String(v.vehicle_id ?? v.name ?? newId()),
    vehicleId: typeof v.vehicle_id === 'number' ? v.vehicle_id : null,
    name: v.name ?? 'Vehicle',
    imageUrl: typeof v.image_url === 'string' ? v.image_url : null,
    priceLabel: priceLabel(v),
    bodyType: typeof v.body_type === 'string' && v.body_type.trim() ? v.body_type : null,
    seatsMin: typeof v.seats_min === 'number' ? v.seats_min : null,
    seatsMax: typeof v.seats_max === 'number' ? v.seats_max : null,
    fuel: typeof v.fuel === 'string' ? v.fuel : null,
    trims: extractTrims(v.variants),
  }
}

/** A card rebuilt after a page reload, from GET /api/vehicles/{id} rather than a
 *  tool_event (the transcript stores card IDS, never a frozen copy — see
 *  ConversationTurn.cards).
 *
 *  IT RENDERS IDENTICALLY, which is the requirement. VehicleDetail omits
 *  `price_published`, but that flag is defined server-side as "either bound is
 *  non-null" (catalogue.py `_vehicle_dict`), and priceLabel above already falls
 *  through to exactly that null check — so an unpublished price stays a null
 *  price line here too, never a figure that appeared out of a refresh. */
export function cardFromDetail(v: VehicleDetail): VehicleCard {
  return toCard({
    vehicle_id: v.vehicle_id,
    name: v.name,
    image_url: v.image_url,
    body_type: v.body_type,
    price_aed_min: v.price_aed_min,
    price_aed_max: v.price_aed_max,
    seats_min: v.seats_min,
    seats_max: v.seats_max,
    fuel: v.fuel,
    variants: v.variants,
  })
}

/**
 * get_vehicle_details output → 0, 1, or 2+ cards. Handles all three shapes:
 *   { vehicle: {...} }               → one card
 *   { ambiguous, candidates: [...] } → a card per variant (the T2 case)
 *   { error } / anything else        → no cards
 */
export function extractCards(output: unknown): VehicleCard[] {
  if (!output || typeof output !== 'object') return []
  const o = output as Record<string, unknown>
  if (o.vehicle && typeof o.vehicle === 'object') return [toCard(o.vehicle as VehicleDict)]
  if (Array.isArray(o.candidates)) return (o.candidates as VehicleDict[]).map(toCard)
  return []
}

/** Interior photo URLs from a get_vehicle_details payload. Only the RESOLVED
 *  shape carries them — the ambiguous `candidates` case returns [] because a
 *  gallery beside a which-one question would pre-answer it with the wrong car.
 *  Grounding as ever (rule 18): URLs come from the payload or not at all. */
export function extractInteriorImages(output: unknown): { name: string; images: string[] } | null {
  if (!output || typeof output !== 'object') return null
  const o = output as Record<string, unknown>
  if (!o.vehicle || typeof o.vehicle !== 'object') return null
  const v = o.vehicle as Record<string, unknown>
  const images = Array.isArray(v.interior_image_urls)
    ? (v.interior_image_urls as unknown[]).filter((u): u is string => typeof u === 'string')
    : []
  if (images.length === 0) return null
  return { name: typeof v.name === 'string' ? v.name : '', images }
}

// Discovery recommendations (rule 18 / FR-BK-10): search_vehicles output → up to
// 3 COMPACT cards. `matches` is the answer; when nothing matched, the agent
// presents `nearest_alternatives`, so those get cards too (each still carries the
// same _vehicle_dict card data — image_url, name, null-safe price). price_unknown
// is intentionally NOT carded — the agent handles that "price on request" nuance
// in prose. Capped at 3 so recommendations don't swamp the thread.
const MAX_RECOMMENDATION_CARDS = 3

export function extractSearchCards(output: unknown): VehicleCard[] {
  if (!output || typeof output !== 'object') return []
  const o = output as Record<string, unknown>
  const matches = Array.isArray(o.matches) ? (o.matches as VehicleDict[]) : []
  const alternatives = Array.isArray(o.nearest_alternatives)
    ? (o.nearest_alternatives as VehicleDict[])
    : []
  const source = matches.length > 0 ? matches : alternatives
  return source.slice(0, MAX_RECOMMENDATION_CARDS).map(toCard)
}
