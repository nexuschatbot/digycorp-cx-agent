// Booking card data — derived from the get_booking tool_event, or rebuilt after
// a reload from GET /api/booking/{ref}. The booking-shaped sibling of cards.ts,
// and it follows the same three rules: every field comes from the tool result
// (FR-CX-07), nothing display-shaped is stored (LOC-01), and a field the payload
// did not carry renders as ABSENT rather than as "null".
//
// THE PERSONAL FIELDS ARE OPTIONAL AND THAT IS THE FEATURE. name/mobile arrive
// only when the backend decided this session is the booking's customer
// (services/booking._booking_dict). The card must therefore look deliberate with
// them and equally deliberate without them — never a gap where a name should be.
// Do NOT add a fallback like "Guest" or "—": an absent name is not an unknown
// name, it is a name we were not entitled to show, and inventing a placeholder
// for it is the same class of mistake as inventing the name itself.

import type { BookingDetail } from './api'

export interface BookingCard {
  id: string
  ref: string
  vehicleName: string | null
  /** The trim, when the model has more than one. Null means the model IS the
   *  car (single-version models carry no trim name — the seed's convention),
   *  so the card shows the model name alone rather than an empty row. */
  variantName: string | null
  startIso: string | null
  endIso: string | null
  locationType: 'showroom' | 'doorstep'
  /** Showroom name, or the doorstep service area — whichever this booking has. */
  where: string | null
  mapLink: string | null
  /** Both scripts ship; the card picks by conversation language, never the
   *  backend. Null when this viewer is not entitled to the name (see above). */
  nameEn: string | null
  nameAr: string | null
  mobile: string | null
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v : null
}

export function toBookingCard(b: BookingDetail): BookingCard {
  const doorstep = b.location_type === 'doorstep'
  return {
    id: b.booking_ref,
    ref: b.booking_ref,
    vehicleName: str(b.vehicle_name),
    variantName: str(b.variant_name),
    startIso: str(b.start_iso),
    endIso: str(b.end_iso),
    locationType: doorstep ? 'doorstep' : 'showroom',
    where: doorstep ? str(b.service_area) ?? str(b.doorstep_address) : str(b.showroom_name),
    // A doorstep visit has no showroom to pin, so no map link — correct, not missing.
    mapLink: doorstep ? null : str(b.showroom_map_link),
    nameEn: str(b.customer_name_en),
    nameAr: str(b.customer_name_ar),
    mobile: str(b.customer_mobile),
  }
}

/**
 * get_booking output -> 0..n cards. Shapes handled:
 *   { bookings: [...], count }        -> a card per booking
 *   { bookings: [] }                  -> none (an honest "no booking found")
 *   { error: ... } / anything else    -> none
 *
 * NO CAP, deliberately unlike extractSearchCards' 3. Truncating a list of
 * SUGGESTIONS is editing; truncating the list of a customer's OWN bookings is
 * hiding one from them. Mirrors _card_anchor in the backend's api/chat.py, which
 * records the same refs for reload — the two must agree or a refresh would draw
 * a different number of cards than the conversation showed.
 */
export function extractBookingCards(output: unknown): BookingCard[] {
  if (!output || typeof output !== 'object') return []
  const rows = (output as Record<string, unknown>).bookings
  if (!Array.isArray(rows)) return []
  return rows
    .filter((b): b is BookingDetail => !!b && typeof b === 'object' && 'booking_ref' in b)
    .map(toBookingCard)
}
