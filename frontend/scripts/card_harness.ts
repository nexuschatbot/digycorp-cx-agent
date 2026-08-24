// Rule-18 card proof — drives the REAL api.ts streamChat and the REAL cards.ts
// extractCards against a live backend, printing the cards that would render for
// each query. Proves the DATA path (how many cards, image URLs, null-price).
//
//   node --experimental-strip-types scripts/card_harness.ts
//
// Does NOT prove the React rendering (image <img> load, RTL layout) — that is the
// manual browser check. This proves what the UI is handed.

import { streamChat } from '../src/api.ts'
import { extractCards, type VehicleCard } from '../src/cards.ts'

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:8000'

function show(cards: VehicleCard[]) {
  if (cards.length === 0) {
    console.log('    → 0 cards')
    return
  }
  for (const c of cards) {
    console.log(`    → card: name="${c.name}"  image=${c.imageUrl ?? '(none)'}  price=${c.priceLabel ?? '(no price line)'}`)
  }
}

async function query(label: string, message: string) {
  console.log(`\n=== ${label} ===\n> "${message}"`)
  const sessionId = crypto.randomUUID()
  const controller = new AbortController()
  let detailsSeen = 0
  await streamChat(
    sessionId,
    message,
    {
      onToolEvent: (e) => {
        if (e.tool === 'get_vehicle_details') {
          detailsSeen += 1
          const cards = extractCards(e.output)
          console.log(`  get_vehicle_details #${detailsSeen} → ${cards.length} card(s)`)
          show(cards)
        }
      },
    },
    controller.signal,
    { baseUrl: BASE },
  )
  if (detailsSeen === 0) console.log('  (agent did not call get_vehicle_details)')
}

async function main() {
  await query('T2 (ambiguous → expect two variants)', 'Tell me about the Jetour T2, both versions.')
  await query('Single model (expect one card)', 'Tell me about the Jetour X50.')
  await query('Null-price model — JET-T1-IDM (card, no price, never "None")', 'Tell me about the Jetour T1 i-DM.')
  console.log('\nCard harness complete.')
}

void main()
