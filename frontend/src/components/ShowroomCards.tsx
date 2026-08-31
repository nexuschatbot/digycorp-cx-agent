// Showroom choice cards — the row of branches Aisha offers when the customer
// asks where we are, or which branch they'd like to visit. Driven off the
// get_showrooms tool_event.
//
// COMMIT semantics, like SlotChips and NOT like VehicleCards. A tap SENDS A
// CHOICE, so the row settles: the chosen card renders selected and its siblings
// go inert, and only the newest showroom row stays live at all. Without that, a
// customer scrolling up three turns could pick a branch the conversation has
// already moved past. If you ever find yourself letting these stay tappable
// forever "because a showroom can't be taken" — the staleness that matters here
// is the CONVERSATION's, not the inventory's.
//
// BUT IT COMMITS AS A PLAIN CHAT TURN, not a tap-intent. There is deliberately
// no /api/intent/select-showroom: no service records a chosen showroom, because
// it is only ever an argument to get_available_slots. Same reasoning that keeps
// showroom-vs-doorstep conversational in CLAUDE.md. So the tap sends prose and
// Aisha answers with slots — exactly as if the customer had typed the name.
//
// THE WHOLE PILL IS THE CONTROL (developer decision 25 Jul 2026), which cost the
// map link. The card used to pair a plain <article> with a "Select →" button,
// and its address was an <a> to the branch's pin. The redesign has no visible
// action, so the row itself must be the button — and an <a> cannot be nested
// inside a <button>. The address is therefore plain text here. Nothing is lost
// from the journey: the map pin (LOC-09) still ships on the BOOKING card, which
// is where a customer actually needs directions; on this card the address is
// there to tell two branches apart, not to navigate to one.
//
// The address comes in both scripts; the card picks one by the conversation's
// language (LOC-01).

import type { ShowroomChoice } from '../showrooms'
import { LABELS, type Lang } from '../i18n'

/** The radio mark. A hollow ring until this branch is the chosen one, then a
 *  ring with a filled centre — the same grammar as any radio, so "one of these"
 *  is legible before the customer reads a word of it. */
function Mark({ selected }: { selected: boolean }) {
  return (
    <span className="shr-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="22" height="22">
        <circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" strokeWidth="2.5" />
        {selected && <circle cx="12" cy="12" r="5" fill="currentColor" />}
      </svg>
    </span>
  )
}

function Card({
  showroom,
  lang,
  state,
  onSelect,
}: {
  showroom: ShowroomChoice
  lang: Lang
  state: 'live' | 'selected' | 'dead'
  onSelect: () => void
}) {
  const t = LABELS[lang]
  // Falls back across scripts rather than showing nothing: a readable English
  // address in an Arabic card beats an addressless card. The seed loader
  // requires address_ar, so this should not fire.
  const address = lang === 'ar' ? showroom.addressAr ?? showroom.address : showroom.address
  const selected = state === 'selected'

  return (
    <button
      className={`shr shr--${state}`}
      type="button"
      // A settled row is not tappable, but the SELECTED one is not merely greyed:
      // it keeps full opacity and its filled mark, so a glance back shows the
      // branch that was chosen. aria-pressed announces it to assistive tech.
      disabled={state !== 'live'}
      aria-pressed={selected}
      // The visible text is a name and an address; the label says what pressing
      // does, which is not otherwise stated anywhere on the control.
      aria-label={selected ? `${showroom.name} — ${t.selected}` : `${t.select} ${showroom.name}`}
      onClick={onSelect}
    >
      <Mark selected={selected} />
      {/* The double bar, same construction as the slot-group heading: one
          element's inline borders, so the pair cannot fall out of step. */}
      <span className="shr-bars" aria-hidden="true" />
      <span className="shr-name">{showroom.name}</span>
      {address && (
        <span className="shr-address">
          {/* bdi so a Latin street name inside an Arabic address does not
              reorder the line around it. */}
          <bdi>{address}</bdi>
        </span>
      )}
    </button>
  )
}

// The RICHER card, used for the nearest-showroom result: distance · drive time,
// a "Get directions" link, and a Select button. Unlike the choice pill this is
// an <article> (not one big <button>) precisely so the directions <a> can live
// inside it alongside the select control.
function NearestCard({
  showroom,
  lang,
  state,
  onSelect,
}: {
  showroom: ShowroomChoice
  lang: Lang
  state: 'live' | 'selected' | 'dead'
  onSelect: () => void
}) {
  const t = LABELS[lang]
  const address = lang === 'ar' ? showroom.addressAr ?? showroom.address : showroom.address
  const selected = state === 'selected'
  const parts: string[] = []
  if (showroom.distanceKm != null) parts.push(`${showroom.distanceKm} km`)
  if (showroom.driveMinutes != null) {
    parts.push(lang === 'ar' ? `${showroom.driveMinutes} دقيقة` : `${showroom.driveMinutes} min`)
  }
  const meta = parts.length ? `≈ ${parts.join(' · ')}` : null
  const dirLabel = lang === 'ar' ? 'الاتجاهات' : 'Get directions'

  return (
    <article className={`nsr nsr--${state}`}>
      <div className="nsr-top">
        <span className="nsr-name">{showroom.name}</span>
        {meta && <span className="nsr-meta">{meta}</span>}
      </div>
      {address && (
        <p className="nsr-address">
          <bdi>{address}</bdi>
        </p>
      )}
      <div className="nsr-actions">
        {showroom.directionsUrl && (
          <a
            className="nsr-dir"
            href={showroom.directionsUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            {'\u{1F9ED}'} {dirLabel}
          </a>
        )}
        <button
          className="nsr-select"
          type="button"
          disabled={state !== 'live'}
          aria-pressed={selected}
          aria-label={selected ? `${showroom.name} — ${t.selected}` : `${t.select} ${showroom.name}`}
          onClick={onSelect}
        >
          {selected ? `✓ ${t.selected}` : t.select}
        </button>
      </div>
    </article>
  )
}

export function ShowroomCards({
  showrooms,
  dir,
  selectedShowroomId,
  disabled,
  onSelect,
  variant = 'choice',
}: {
  showrooms: ShowroomChoice[]
  dir: 'rtl' | 'ltr'
  /** The branch picked from THIS row, once one has been. Undefined until a pick. */
  selectedShowroomId?: number
  /** Streaming, a closed chat, or a superseded row — see App. */
  disabled?: boolean
  onSelect: (showroom: ShowroomChoice) => void
  /** 'choice' = get_showrooms pills; 'nearest' = the rich nearest-showroom card. */
  variant?: 'choice' | 'nearest'
}) {
  if (showrooms.length === 0) return null
  const lang: Lang = dir === 'rtl' ? 'ar' : 'en'
  const Item = variant === 'nearest' ? NearestCard : Card

  return (
    <div className={`shrs shrs--${dir}${variant === 'nearest' ? ' shrs--nearest' : ''}`} dir={dir}>
      {showrooms.map((s) => (
        <Item
          key={s.id}
          showroom={s}
          lang={lang}
          state={
            selectedShowroomId === s.showroomId
              ? 'selected'
              : selectedShowroomId != null || disabled
                ? 'dead'
                : 'live'
          }
          onSelect={() => onSelect(s)}
        />
      ))}
    </div>
  )
}
