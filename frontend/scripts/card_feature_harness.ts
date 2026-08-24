// Discovery-cards + View-more proof — drives the REAL api.ts streamChat and the
// REAL cards.ts extractors against a live backend, printing the cards the UI
// would render (compact from search_vehicles, full from get_vehicle_details) and
// exercising the View-more send text.
//
//   node --experimental-strip-types scripts/card_feature_harness.ts
//
// Proves the DATA/extraction path. It does NOT prove React rendering (compact vs
// full sizing, the button, RTL layout, or multi-card→single promotion visuals) —
// those are the manual browser check. Multi-card→single promotion is clean by
// construction: card rows are appended, never mutate existing rows (stable keys).

import { streamChat } from '../src/api.ts'
import { extractCards, extractSearchCards, type VehicleCard } from '../src/cards.ts'

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:8000'

function line(s = '') {
  process.stdout.write(s + '\n')
}
function showCards(tag: string, cards: VehicleCard[]) {
  line(`  ${tag}: ${cards.length} card(s)`)
  for (const c of cards) {
    line(`     • ${c.name}  image=${c.imageUrl ?? '(none)'}  price=${c.priceLabel ?? '(no price line)'}`)
  }
}

// Run one turn on its own session; return the compact cards (search_vehicles) and
// full cards (get_vehicle_details) the UI would render for it.
async function turn(label: string, message: string) {
  line(`\n=== ${label} ===\n> "${message}"`)
  const sessionId = crypto.randomUUID()
  const compact: VehicleCard[] = []
  const full: VehicleCard[] = []
  await streamChat(
    sessionId,
    message,
    {
      onToolEvent: (e) => {
        if (e.tool === 'search_vehicles') compact.push(...extractSearchCards(e.output))
        if (e.tool === 'get_vehicle_details') full.push(...extractCards(e.output))
      },
    },
    new AbortController().signal,
    { baseUrl: BASE },
  )
  if (compact.length) showCards('COMPACT (search_vehicles)', compact)
  if (full.length) showCards('FULL (get_vehicle_details)', full)
  if (!compact.length && !full.length) line('  (no card tool_events)')
  return { compact, full }
}

async function main() {
  // 1 — no-match budget query → compact X50 via nearest_alternatives (proof #1).
  // NB: "car", not "SUV" — the X50 is a compact crossover, so constraining to
  // "SUV" would (correctly) exclude it. This is the developer's 5-seat/50k/petrol.
  await turn('Discovery no-match → compact nearest_alternative card (X50)', 'I want a 5-seat petrol car for around 50,000 AED.')

  // 2 — discovery with matches → 2–3 compact cards (proof #2)
  const hybrids = await turn('Discovery matches → 2–3 compact cards', 'What plug-in hybrids do you have?')

  // 5 — null-price via the nearest_alternatives path (the stage scenario)
  await turn('Null-price via nearest_alternatives (proof #5, alternatives path)', 'Show me a plug-in hybrid under 100,000 AED.')

  // 3 — View more on the X50 → exact name → full card
  await turn('View more → "Tell me more about the Jetour X50" → FULL card (proof #3)', 'Tell me more about the Jetour X50')

  // 4 — View more on a T2 variant compact card → EXACT name → that variant, no ambiguity
  const t2 = hybrids.compact.find((c) => c.name === 'Jetour T2 i-DM')
  if (t2) {
    line(`\n[View more clicked on the compact card "${t2.name}" → sends: "Tell me more about the ${t2.name}"]`)
    const r = await turn('View more on T2 i-DM → exact variant, NO ambiguity re-ask (proof #4)', `Tell me more about the ${t2.name}`)
    const names = r.full.map((c) => c.name)
    line(`  → full cards: [${names.join(', ')}]  ${names.length === 1 && names[0] === 'Jetour T2 i-DM' ? 'OK: resolved to the exact variant, one full card' : 'CHECK'}`)
  } else {
    line('\n[T2 i-DM not among the hybrid compact cards — skipping proof #4 sample]')
  }

  line('\nCard feature harness complete.')
}

void main()
