// Tappable slot chips in the chat thread (FR-SL-01) — sibling of VehicleCards.
//
// GROUPED BY DAY, TIME-ONLY PILLS (developer decision 25 Jul 2026). Each chip
// used to be a two-line card carrying day, date, time and place, which made a
// row of three offers read as three near-identical sentences the customer had to
// diff. The day and the place moved up into a heading and the pill kept the one
// thing that actually varies within a group — the hour. Tapping still sends the
// same clean human prose via send() AND the exact slot_id via the hidden field
// (the backend pins lock_slot to it), so nothing about the commit path changed.
//
// THE HEADING CARRIES THE PLACE, and that is load-bearing rather than decorative:
// the pill no longer says where it is, so if the tool returns slots across two
// branches the heading is the only thing standing between a customer and tapping
// 7:00 pm at the wrong showroom (rule 2). See groupSlotChips.
//
// COMMIT semantics (contrast VehicleCards' browse): a tap SENDS A CHOICE. Once
// the customer picks, the row is settled — `selectedSlotId` marks the chosen
// chip SELECTED (visibly, not just greyed) and every sibling goes dead. A settled
// row reads "one selected, the rest disabled", never all-dead-identical, so a
// glance back shows what they picked. `disabled` (the whole row inert, e.g. a
// superseded offer) is orthogonal: a superseded row with no pick is all disabled
// and unmarked; a picked row keeps its one selected chip.

import { groupSlotChips, slotPickText, slotTimeLabel, type SlotChip } from '../slots'

export function SlotChips({
  slots,
  dir,
  onPick,
  selectedSlotId,
  disabled,
}: {
  slots: SlotChip[]
  dir: 'rtl' | 'ltr'
  onPick: (text: string, slotId: number) => void
  selectedSlotId?: number
  disabled?: boolean
}) {
  const groups = groupSlotChips(slots, dir)

  return (
    <div className={`slots slots--${dir}`} dir={dir}>
      {groups.map((group) => {
        // The heading's mark goes yellow on the group holding the pick and stays
        // grey elsewhere — so a settled row says WHICH DAY was chosen at a glance,
        // not only which pill.
        const holdsSelection =
          selectedSlotId != null && group.chips.some((c) => c.slotId === selectedSlotId)
        return (
          <div className="slot-group" key={group.key}>
            <div className="slot-group-head">
              <span
                className={`slot-group-mark${holdsSelection ? ' slot-group-mark--picked' : ''}`}
                aria-hidden="true"
              />
              <span className="slot-group-day">{group.day}</span>
              {group.where && (
                <span className="slot-group-where">
                  {group.locationType === 'doorstep' ? `Doorstep · ${group.where}` : group.where}
                </span>
              )}
            </div>

            <div className="slot-group-times">
              {group.chips.map((chip) => {
                const selected = selectedSlotId != null && chip.slotId === selectedSlotId
                return (
                  <button
                    key={chip.id}
                    type="button"
                    className={`slot-chip${selected ? ' slot-chip--selected' : ''}`}
                    // A settled row is not tappable (disabled OR a pick has
                    // happened); the selected chip stays visually distinct via the
                    // class, not the button state. aria-pressed announces the
                    // choice to assistive tech.
                    disabled={disabled || selectedSlotId != null}
                    aria-pressed={selected}
                    // The pill reads "5:00 pm", so on its own it would announce a
                    // bare time. The full sentence is spelled out for assistive
                    // tech, which cannot see the heading sitting above it.
                    aria-label={slotPickText(chip)}
                    // Visible message is id-free human prose; the exact slot_id
                    // rides the hidden field so lock_slot lands on THIS slot, no
                    // prose re-match.
                    onClick={() => onPick(slotPickText(chip), chip.slotId)}
                  >
                    {slotTimeLabel(chip, dir)}
                  </button>
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}
