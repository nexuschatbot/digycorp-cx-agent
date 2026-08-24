// Transport harness for Stage 3 — drives the REAL api.ts `streamChat`, the exact
// function the Stop button's onClick calls. Run against a live backend:
//
//   (backend up on :8000 with ANTHROPIC_API_KEY)
//   node --experimental-strip-types scripts/sse_harness.ts
//
// WHAT THIS PROVES: the SSE transport + parsing + abort path in api.ts —
// incremental token accumulation, tool_events, the booking event, done+lang,
// resume across turns on one session_id, per-bubble RTL detection, and the abort
// round-trip (client reader terminates + it is the drop that cancels the server).
//
// WHAT THIS DOES NOT PROVE (verify by hand in the browser — see the checklist):
//   - that the Stop button / Send button are wired to this code
//   - the disabled/enabled button states
//   - the timer element freezing on the booking event (this harness only shows
//     the booking event ARRIVES; the on-screen freeze is React state)
// The harness exercises the transport; the React wiring is a separate seam.

import { streamChat, type Lang } from '../src/api.ts'
import { dirFor } from '../src/rtl.ts'

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:8000'
const MOBILE = '+971509998877'
const sessionId = crypto.randomUUID()

function line(s = '') {
  process.stdout.write(s + '\n')
}

// Reconstruct a bubble exactly as the UI does: append each token; note growth.
async function turn(label: string, message: string) {
  line(`\n=== ${label} ===`)
  line(`> user: ${message}`)
  let bubble = ''
  let tokenEvents = 0
  const tools: string[] = []
  let booking: string | null = null
  let doneLang: Lang | null = null
  const t0 = Date.now()

  const controller = new AbortController()
  await streamChat(
    sessionId,
    message,
    {
      onToken: (e) => {
        tokenEvents += 1
        bubble += e.text
        if (tokenEvents <= 3 || tokenEvents % 8 === 0) {
          line(`  token#${tokenEvents} → bubble now ${bubble.length} chars: "${bubble.slice(-48).replace(/\n/g, ' ')}"`)
        }
      },
      onToolEvent: (e) => { tools.push(e.tool); line(`  tool_event: ${e.tool}`) },
      onBooking: (e) => { booking = e.booking_ref; line(`  ★ booking event: ${e.booking_ref} (fires timer freeze in UI at t=${Date.now() - t0}ms)`) },
      onDone: (e) => { doneLang = e.lang },
      onError: (e) => line(`  ! error: ${e.message}`),
    },
    controller.signal,
    { baseUrl: BASE },
  )

  line(`  done. tokens=${tokenEvents}, tools=[${tools.join(', ')}], booking=${booking ?? '—'}, lang=${doneLang}`)
  line(`  reconstructed bubble dir=${dirFor(bubble)} :: "${bubble.slice(0, 90).replace(/\n/g, ' ')}${bubble.length > 90 ? '…' : ''}"`)
  return { bubble, tools, booking, doneLang }
}

async function abortRoundTrip() {
  line('\n=== ABORT ROUND-TRIP (real streamChat + AbortController) ===')
  const abortSession = crypto.randomUUID()
  const controller = new AbortController()
  let abortedFired = false
  let tokensBeforeAbort = 0

  const p = streamChat(
    abortSession,
    `I am Test Driver, ${MOBILE} — book the earliest Jetour T2 petrol at Deira, WhatsApp fine, I confirm.`,
    {
      onToken: () => { tokensBeforeAbort += 1 },
      onToolEvent: (e) => line(`  (server working: ${e.tool})`),
      onAborted: () => { abortedFired = true; line('  CLIENT: onAborted fired — the stream reader terminated') },
      onError: (e) => line(`  ! error: ${e.message}`),
    },
    controller.signal,
    { baseUrl: BASE },
  )

  // Drop the connection mid-stream, exactly as the Stop button does.
  setTimeout(() => { line(`  CLIENT: calling controller.abort() (Stop button)`); controller.abort() }, 1600)
  await p
  line(`  CLIENT: streamChat resolved after abort. onAborted=${abortedFired}, tokens seen before abort=${tokensBeforeAbort}`)
  line(`  abort session_id=${abortSession}  ← grep the server log for this to see "chat aborted by client"`)
}

async function main() {
  line(`Harness session_id=${sessionId}  base=${BASE}`)
  await turn('Turn 1 — discovery (tokens + tool_events stream in)', `Hi, I am Test Driver, mobile ${MOBILE}. I want the Jetour T2 petrol — earliest slot at any showroom.`)
  await turn('Turn 2 — RESUME same session (should NOT re-ask identity/vehicle)', 'The earliest one is perfect — please lock it and book it, WhatsApp reminders are fine, I confirm and will bring my licence.')
  await turn('Turn 3 — Arabic script (RTL detection)', 'شكراً، هل يمكنني تغيير الموعد؟')
  await abortRoundTrip()
  line('\nHarness complete.')
}

void main()
