// Inline vehicle card(s) in the chat thread (rule 18).
//
// ONE CARD FACE, TWO BEHAVIOURS (developer decision 25 Jul 2026). The two
// variants used to be two different sizes — a prominent 265px card for
// get_vehicle_details and a small picture-and-name tile for search_vehicles
// recommendations. They are now the SAME 265x238 card to a pixel, and `variant`
// survives only to say whether the card is PRESSABLE:
//   full    — get_vehicle_details: an <article>. Nothing to open; you are
//             already looking at the thing.
//   compact — search_vehicles (FR-BK-10): a <button>. The whole card opens the
//             detail dialog.
// The row scrolls sideways rather than wrapping, because three 265px cards do
// not fit a phone at any breakpoint — see the strip note in styles.css.
//
// A null price shows NO price line (never "None"), a null body_type drops the
// tagline row, and a null/broken image collapses to a clean placeholder. `dir`
// mirrors the layout for Arabic conversations without depending on the (mostly
// Latin) card text.
//
// BROWSE semantics — do NOT give these commit behaviour (contrast SlotChips).
// A tap OPENS A DIALOG; it is exploration, not a choice. Tapping one card must
// LEAVE every other card fully live (browse T2 -> X70 -> back). So the ONLY
// thing that may disable a card is `disabled` (streaming — don't tap
// mid-response); there is deliberately no per-card selected/settled state and no
// sibling-disabling. If you ever find yourself adding "disable the others on tap"
// here, that is a slot-chip behaviour in the wrong place — stop. Same reason the
// tick badge from the 21 Jul mockup is NOT here: a checkmark says "chosen", and
// choosing a car is a sentence Aisha has to hear, not a card state.
//
// It used to send "Tell me more about the <name>" as a chat message, which spent
// a whole agent turn (two model calls) fetching detail the browser had already
// been handed and thrown away. Browsing is fully determined by the vehicle id,
// so it needs no model: the dialog fetches /api/vehicles/{id} for zero tokens.
// The COMMIT — "Book test drive" — lives inside the dialog, and that one does
// wake the agent, because it starts a conversation rather than answering one.

import { useState } from 'react'

import type { VehicleCard } from '../cards'
import { fuelLabel, seatsLine, type Lang } from '../i18n'

export type CardVariant = 'compact' | 'full'

/** Spec chips. TWO, not the three in the mockup: seats and powertrain are the
 *  only specs the catalogue actually carries. There is no boot-capacity field
 *  anywhere, and engine displacement exists only inside `headline_features`
 *  prose — so "620L boot" and "Petrol 1.5T" would both have to be invented, and
 *  a chip is a factual claim about the car (FR-CX-07). Re-confirmed 25 Jul 2026
 *  against the shipped seed; the mockup's own chips were wrong for the car it
 *  drew (the real T2 is 5 seats and a 2.0L turbo). A chip with no data behind it
 *  is omitted, never guessed — a third chip means seeding real data first. */
function specChips(card: VehicleCard, lang: Lang): string[] {
  const chips: string[] = []
  if (card.seatsMin != null && card.seatsMax != null) {
    chips.push(seatsLine(card.seatsMin, card.seatsMax, lang))
  }
  const fuel = fuelLabel(card.fuel, lang)
  if (fuel) chips.push(fuel)
  return chips
}

/** THE BRAND PREFIX IS A DEMO-ONLY SHORTCUT (developer decision 25 Jul 2026).
 *
 *  The card sets name and price on ONE line, and the real catalogue prices eight
 *  of nine models as a RANGE — "AED 146,000–179,000" is 121px of a 219px text
 *  column. At 14px the full "Jetour X70 Plus" no longer fits beside it (measured:
 *  five of nine cards truncated); dropping the brand brings the widest row to
 *  205px and all nine fit.
 *
 *  KNOWN AND ACCEPTED: this hardcodes ONE brand into the UI, in a system that is
 *  multi-brand everywhere else — every vehicle and showroom carries a `brand`,
 *  and SOUEAST / The Elite Cars are production roadmap. A SOUEAST card would
 *  therefore render its brand while the Jetour ones do not. The clean fix is
 *  `brand` on the vehicle-detail payload so this strips whatever the row
 *  actually carries; it was declined as a backend change for the demo. Do this
 *  properly before a second brand ships.
 *
 *  Strips for DISPLAY only — `card.name` stays whole, so the image alt text and
 *  the detail dialog still say "Jetour T2". */
const BRAND_PREFIX = /^Jetour\s+/

function displayName(name: string): string {
  return name.replace(BRAND_PREFIX, '')
}

function CardImage({ card }: { card: VehicleCard }) {
  const [broken, setBroken] = useState(false)
  if (card.imageUrl && !broken) {
    return (
      <img
        className="vcard-img"
        src={card.imageUrl}
        alt={card.name}
        loading="lazy"
        onError={() => setBroken(true)}
      />
    )
  }
  return <div className="vcard-img vcard-img--empty" aria-hidden="true">◆</div>
}

/** The card face — identical in both variants: image, then name and price on one
 *  baseline row, then the tagline, then the spec chips.
 *
 *  PRICE IS ON THE RECOMMENDATION CARD TOO, reversing the 21 Jul decision to
 *  drop it there. That reasoning — three price ranges side by side turn a choice
 *  about the car into a comparison of numbers — is still true, and the developer
 *  chose the identical-card trade anyway on 25 Jul 2026. It is a presentation
 *  call, not a grounding one: the figure was always real, and it is now one
 *  swipe rather than one tap away. */
function CardFace({ card, lang }: { card: VehicleCard; lang: Lang }) {
  const chips = specChips(card, lang)
  return (
    <>
      <CardImage card={card} />
      <div className="vcard-body">
        {/* Name leading, price trailing on one baseline row. */}
        <div className="vcard-head">
          <span className="vcard-name">{displayName(card.name)}</span>
          {card.priceLabel && <span className="vcard-price">{card.priceLabel}</span>}
        </div>
        {card.bodyType && <span className="vcard-tagline">{card.bodyType}</span>}
        {chips.length > 0 && (
          <ul className="vcard-chips">
            {chips.map((c) => (
              <li className="vcard-chip" key={c}>{c}</li>
            ))}
          </ul>
        )}
      </div>
    </>
  )
}

/** COMPACT — THE WHOLE CARD IS THE CONTROL (21 Jul 2026).
 *
 *  It used to be an image beside a name, a price and a small "View more" button.
 *  The button is gone because the card itself is now the button: a card that
 *  looks tappable and answers a tap anywhere is a bigger, more phone-friendly
 *  target than a 5rem link, which matters when the audience is on their own
 *  handsets. It is a real <button>, not a div with onClick, so it keeps keyboard
 *  focus and Enter/Space for free.
 *
 *  It no longer differs from the full card in SHAPE, only in whether it is
 *  pressable — so the class here is `--pressable`, not `--compact`: the name
 *  says what it does, since it no longer says anything about its size.
 *
 *  Falls back to a plain <article> when there is no id to open — a card that
 *  looks pressable and does nothing is worse than one that does not invite it. */
function CompactCard({
  card,
  lang,
  onViewMore,
  disabled,
}: {
  card: VehicleCard
  lang: Lang
  onViewMore?: (card: VehicleCard) => void
  disabled?: boolean
}) {
  const face = <CardFace card={card} lang={lang} />

  if (!onViewMore || card.vehicleId == null) {
    return <article className="vcard vcard--static">{face}</article>
  }

  return (
    <button
      className="vcard vcard--pressable"
      type="button"
      disabled={disabled}
      // Hands the WHOLE card up: the caller opens the dialog on card.vehicleId,
      // which resolves to this exact row — no "which T2?" ambiguity, and no chat
      // turn spent re-identifying it by name.
      onClick={() => onViewMore(card)}
    >
      {face}
    </button>
  )
}

export function VehicleCards({
  cards,
  dir,
  variant,
  onViewMore,
  disabled,
}: {
  cards: VehicleCard[]
  dir: 'rtl' | 'ltr'
  variant: CardVariant
  onViewMore?: (card: VehicleCard) => void
  disabled?: boolean
}) {
  // Same dir -> lang mapping App uses for the detail dialog: the cards were
  // rendered by a bubble whose direction already settled the language, so the
  // chips label themselves without asking useChat.
  const lang: Lang = dir === 'rtl' ? 'ar' : 'en'
  return (
    <div className={`vcards vcards--${variant} vcards--${dir}`} dir={dir}>
      {/* EVERY CARD IS PRESSABLE (2026-08-14). `full` used to render a plain
          <article> — no tap, no details — on the reasoning that a car you asked
          about by name is "the one you're looking at" rather than a suggestion
          to explore. That reasoning died on 25 Jul when the two variants became
          the same card to a pixel: they now look identical, so a full card
          INVITES the tap it then ignores. This file's own note says a card that
          looks pressable and does nothing is worse than one that does not invite
          it — which is exactly what full had become.

          Opening the dialog costs nothing either: it fetches /api/vehicles/{id}
          directly, with no chat turn and no tokens. CompactCard still falls back
          to a plain <article> when there is no id, so a card with nothing to
          open never pretends otherwise. `variant` now only picks the container
          class. */}
      {cards.map((c) => (
        <CompactCard
          key={c.id}
          card={c}
          lang={lang}
          onViewMore={onViewMore}
          disabled={disabled}
        />
      ))}
    </div>
  )
}
