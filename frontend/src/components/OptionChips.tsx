// Tappable answers to a question the agent asked (2026-08-14) — sibling of
// SlotChips, and deliberately built to look like one so the thread has ONE
// vocabulary for "this is a thing you can tap", not two.
//
// THE LABEL IS THE MESSAGE. A tap sends the chip's own text as an ordinary turn.
// There is no id, no hidden field and nothing pinned — unlike SlotChips, where
// the visible prose and the trusted slot_id are two different things travelling
// together. Here they cannot drift because there is only one of them.
//
// COMMIT semantics, same as the slot chips: a tap settles the row. The chosen
// chip renders selected and its siblings go dead, so a glance back at the thread
// shows what was answered rather than a row of live-looking buttons for a
// question that closed ten turns ago. `disabled` (a superseded row, a closed
// chat, a turn in flight) is orthogonal to that.
//
// THE CHIPS ARE NEVER THE QUESTION. Aisha asks in words in the same message and
// these sit underneath — so a screen reader, a copied transcript, or a phone that
// renders none of this still carries the question intact. That is also why there
// is no empty state here: no options means no row, not an explanatory box.

import type { OptionChip } from '../options'

export function OptionChips({
  options,
  dir,
  onPick,
  selectedLabel,
  disabled,
}: {
  options: OptionChip[]
  dir: 'rtl' | 'ltr'
  onPick: (text: string) => void
  selectedLabel?: string
  disabled?: boolean
}) {
  if (options.length === 0) return null

  return (
    <div className={`options options--${dir}`} dir={dir}>
      {options.map((opt) => {
        const selected = selectedLabel != null && opt.label === selectedLabel
        return (
          <button
            key={opt.label}
            type="button"
            className={`option-chip${selected ? ' option-chip--selected' : ''}`}
            // Settled rows are inert; the pick stays visible through the class
            // rather than the button state, exactly as the slot chips do it.
            disabled={disabled || selectedLabel != null}
            aria-pressed={selected}
            onClick={() => onPick(opt.label)}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}
