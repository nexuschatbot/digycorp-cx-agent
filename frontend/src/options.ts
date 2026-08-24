/**
 * Option chips — tappable answers to a question the agent asked (2026-08-14).
 *
 * The list is DECLARED BY THE AGENT (offer_options), never inferred from her
 * prose. We already learned what inferring costs: the "use my current location"
 * control keyed off a phrase in the agent's text and appeared under "please
 * share your booking reference". Text is not a contract, and it would need a
 * second Arabic pattern set on top.
 *
 * A chip's label IS its message. Tapping sends that exact string as an ordinary
 * typed turn — nothing downstream knows a chip was involved, which is precisely
 * why ignoring the chips and typing "the second one" costs the customer nothing.
 * Deliberately unlike the slot chips, which pin a trusted slot_id: there is no id
 * here, and inventing one would make the buttons authoritative over the
 * customer's own words.
 */

// Mirrors MIN_OPTIONS / MAX_OPTIONS in the backend's app/tools/ui_tools.py. The
// backend already trimmed and de-duplicated; this cap is the belt to that
// braces, so a shape we did not expect can never spill a menu across the thread.
const MAX_OPTION_CHIPS = 4

export interface OptionChip {
  /** Shown on the chip AND sent verbatim when tapped. One string, one meaning. */
  label: string
}

/**
 * offer_options output → 0 or 2+ chips.
 *   { offered: true, options: ["Yes", "No"] } → two chips
 *   { error } / anything else                → none
 *
 * Returns [] for a single option too: one button is not a choice, and rendering
 * it would imply the customer has to press it.
 */
export function extractOptionChips(output: unknown): OptionChip[] {
  if (!output || typeof output !== 'object') return []
  const o = output as Record<string, unknown>
  if (o.offered !== true || !Array.isArray(o.options)) return []
  const labels = (o.options as unknown[])
    .filter((x): x is string => typeof x === 'string')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .slice(0, MAX_OPTION_CHIPS)
  return labels.length >= 2 ? labels.map((label) => ({ label })) : []
}
