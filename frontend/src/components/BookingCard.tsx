// Booking card(s) in the chat thread — what the customer sees when they ask
// about an existing test drive. Driven off the get_booking tool_event, and
// rebuilt after a reload from the `booking_refs` anchor (see useChat).
//
// READ-ONLY, like the vehicle cards and unlike the slot chips. Nothing here
// commits: the map link leaves the page and touches no booking. Rescheduling or
// cancelling stays conversational — those are decisions, and a decision belongs
// in a sentence, not a button that fires silently. If you are ever tempted to
// put "Cancel booking" on this card, that is a slot-chip reflex in the wrong
// place: cancelling owes the customer one retention offer first (rule 11),
// which only the agent can make.
//
// NO ACTION ROW AT ALL (developer decision 25 Jul 2026). "Book In My Calendar"
// was the last one and is gone, so the card is purely a statement of fact and
// ends on the map link. The mockup's "Edit" and "Send To My Email" were NOT
// built in its place: neither has anything behind it — there is no edit flow
// (that is what reschedule is, and it is conversational per rule 11) and no
// email channel in this system at all (WhatsApp is the only one). A control
// that looks live and does nothing is worse on a demo stage than no control.
//
// THE HEADER IS INSIDE THE CARD, beside the rail, and REPEATS on every card —
// drawn that way, and kept that way. It used to repeat the ROW TOTAL, which was
// invisible while the cards were a horizontal strip showing one at a time. Once
// they stacked (2026-07-28) the same "Current Bookings: 2" appearing twice, one
// above the other, read as a duplicated card rather than as a design.
//
// So the badge now carries the card's POSITION, "1 of 2", and the heading went
// SINGULAR to match: each card describes its own one booking. Both numbers are
// still derived from the rendered array rather than stored, so the guarantee
// that a card cannot disagree with the cards beside it is unchanged. The
// alternative — hoisting one heading above the stack — was considered and not
// taken; the plural labels stay in i18n for it.
//
// THE PERSONAL ROWS ARE CONDITIONAL. name and mobile arrive only when the
// backend judged this session entitled to them. Both rows are omitted entirely
// when absent — never a placeholder, never an empty value next to a label. See
// the header of bookings.ts for why a "Guest" fallback would be wrong.

import type { BookingCard as Booking } from '../bookings'
import { LABELS, slotRange, type Lang } from '../i18n'

/** One labelled fact. Renders nothing at all when the value is absent, so an
 *  ungated card closes up cleanly instead of showing a label with a gap. */
function Field({ label, value }: { label: string; value: string | null }) {
  if (!value) return null
  return (
    <div className="bkg-field">
      <span className="bkg-label">{label}</span>
      {/* bdi isolates Latin model names and +971 numbers inside an Arabic
          card, so they do not reorder the line around them (LOC-01). */}
      <bdi className="bkg-value">{value}</bdi>
    </div>
  )
}

/** Is this booking still ahead of us? Drives the heading (see LABELS) — a
 *  booking is "current" until its slot has actually passed, which is a fact about
 *  the clock rather than about which tool drew the card. Unparseable or missing
 *  start counts as upcoming: a live booking mislabelled "Previous" is the worse
 *  of the two errors. */
function isUpcoming(startIso: string | null): boolean {
  if (!startIso) return true
  const t = new Date(startIso).getTime()
  return Number.isNaN(t) || t >= Date.now()
}

function Card({
  booking,
  lang,
  title,
  index,
  count,
  superseded,
}: {
  booking: Booking
  lang: Lang
  /** The heading, resolved by the row (see BookingCards) so every card in a row
   *  says the same thing about the same set of bookings. */
  title: string
  /** 1-based position in the stack. */
  index: number
  count: number
  /** This card has been replaced by a later one for the same reference (a
   *  reschedule), or the booking was cancelled. Kept in the thread so the
   *  transcript stays honest about what happened, but muted — see the header. */
  superseded?: boolean
}) {
  const t = LABELS[lang]
  const name = lang === 'ar' ? booking.nameAr ?? booking.nameEn : booking.nameEn ?? booking.nameAr
  const when = slotRange(booking.startIso, booking.endIso, lang)

  return (
    <article className={superseded ? 'bkg bkg--superseded' : 'bkg'}>
      {/* The rail: a grey gutter carrying the person mark, fenced off from the
          content by the double yellow rule (the rule lives in CSS, as the rail's
          right border plus the body's left border — see styles.css). */}
      <div className="bkg-rail" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="26" height="26" fill="currentColor">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8z" />
        </svg>
      </div>

      <div className="bkg-main">
        <div className="bkg-head">
          <span className="bkg-head-label">{title}</span>
          {/* POSITION, not row size, once the stack has more than one card:
              "1 of 2". Both numbers are derived from the rendered array, never
              stored, so the badge cannot disagree with the cards around it.
              A lone card keeps the bare "1" — "1 of 1" is a count pretending to
              be a position. */}
          <span className="bkg-count">
            {count > 1 ? `${index} ${t.ofTotal} ${count}` : count}
          </span>
        </div>

        <div className="bkg-grid">
          {/* The booking number first: it is the handle the customer quotes on
              WhatsApp or a reschedule call, so it leads the facts. Always
              present — every booking has a ref (FR-BK-08). */}
          <Field label={t.refLabel} value={booking.ref} />
          <Field label={t.nameLabel} value={name} />
          <Field label={t.mobileLabel} value={booking.mobile} />
          {/* Model and trim on ONE line, not two. They name a single car —
              "Jetour G700 Flagship" — and splitting them into two labelled
              fields would read as two facts to check rather than one car. A
              single-version model carries no trim and is unchanged. */}
          <Field
            label={t.vehicleLabel}
            value={
              booking.vehicleName && booking.variantName
                ? `${booking.vehicleName} ${booking.variantName}`
                : booking.vehicleName
            }
          />
          <Field label={t.timeSlotLabel} value={when} />
        </div>

        {booking.mapLink && booking.where ? (
          <div className="bkg-field bkg-field--wide">
            <span className="bkg-label">{t.mapLinkLabel}</span>
            <a className="bkg-link" href={booking.mapLink} target="_blank" rel="noreferrer noopener">
              <bdi>{booking.where}</bdi>
            </a>
          </div>
        ) : (
          // Doorstep has no showroom to pin; show WHERE WE ARE COMING TO
          // instead, so the card never just drops the location silently
          // (FR-SL-07).
          <div className="bkg-field bkg-field--wide">
            <Field
              label={booking.locationType === 'doorstep' ? t.doorstepLabel : t.mapLinkLabel}
              value={booking.where}
            />
          </div>
        )}
      </div>
    </article>
  )
}

export function BookingCards({
  bookings,
  dir,
  superseded,
}: {
  bookings: Booking[]
  dir: 'rtl' | 'ltr'
  superseded?: boolean
}) {
  if (bookings.length === 0) return null
  const lang: Lang = dir === 'rtl' ? 'ar' : 'en'
  const t = LABELS[lang]

  // Upcoming unless EVERY booking here has already happened — one live booking
  // in the row is enough to make the row about the present.
  const upcoming = bookings.some((b) => isUpcoming(b.startIso))
  // ALWAYS SINGULAR now (2026-07-28). Each card carries its own heading and its
  // own "n of N", so the heading describes THAT CARD's one booking; a card
  // reading "Current Bookings: 1 of 2" would be describing the set and its own
  // position in the same breath. The plural labels are kept in i18n rather than
  // deleted: they are the wording to reach for if the heading is ever hoisted
  // above the stack, which was the alternative considered here.
  const title = upcoming ? t.currentBooking : t.previousBooking

  // VERTICAL STACK (product decision 2026-07-28), reversing the horizontal
  // strip. Multiple bookings push the thread down by a card each, which is the
  // cost the strip existed to avoid and is now accepted: a sideways strip with
  // no visible scrollbar hid every booking after the first, and a booking the
  // customer cannot find reads as one we lost. See styles.css .bkgs for why
  // .vcards stays horizontal while this does not.
  return (
    <div className={`bkgs bkgs--${dir}${superseded ? ' bkgs--superseded' : ''}`} dir={dir}>
      {bookings.map((b, i) => (
        <Card
          key={b.id}
          booking={b}
          lang={lang}
          title={title}
          index={i + 1}
          count={bookings.length}
          superseded={superseded}
        />
      ))}
    </div>
  )
}
