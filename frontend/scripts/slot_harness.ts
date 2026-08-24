// Slot-chip + Option-B proof — drives the REAL api.ts streamChat and the REAL
// slots.ts extractors against a live backend.
//
//   node --experimental-strip-types scripts/slot_harness.ts
//
// Proves: get_available_slots -> chips (day/date/time/location), the doorstep
// service-area case, Arabic day-name localization, and the TAP chain — a clean
// id-free prose message + the hidden slot_id, with lock_slot landing on that
// EXACT id (read off its tool_event). Backend stale-slot/pin guarantees are the
// deterministic pytest; React rendering is the manual browser check.

import { streamChat } from '../src/api.ts'
import { extractSlotChips, slotChipLabel, slotPickText, type SlotChip } from '../src/slots.ts'

const BASE = process.env.BASE_URL ?? 'http://127.0.0.1:8000'

function line(s = '') {
  process.stdout.write(s + '\n')
}

// One turn on a session; returns the slot chips + any lock_slot tool input seen.
async function turn(sessionId: string, message: string, slotId?: number) {
  const chips: SlotChip[] = []
  let lockInput: Record<string, unknown> | null = null
  let reply = ''
  await streamChat(
    sessionId,
    message,
    {
      onToken: (e) => { reply += e.text },
      onToolEvent: (e) => {
        if (e.tool === 'get_available_slots') chips.push(...extractSlotChips(e.output))
        if (e.tool === 'lock_slot') lockInput = e.input as Record<string, unknown>
      },
    },
    new AbortController().signal,
    slotId != null ? { baseUrl: BASE, slotId } : { baseUrl: BASE },
  )
  return { chips, lockInput, reply }
}

async function getSlots(label: string, message: string) {
  line(`\n=== ${label} ===`)
  const sid = crypto.randomUUID()
  const { chips } = await turn(sid, message)
  line(`> "${message}"  → ${chips.length} chip(s):`)
  for (const c of chips) {
    const { when, where } = slotChipLabel(c, 'ltr')
    line(`   [${when} · ${where}]   (slot_id ${c.slotId}, ${c.locationType})`)
  }
  return { sid, chips }
}

async function main() {
  // 1 — showroom slots as chips (proofs #1/#3: however many the day has, ≤3)
  const showroom = await getSlots(
    'Showroom slots → chips',
    "Hi, I'm Test Driver, mobile +971509998877. I'd like to test drive the Jetour T2 petrol this Saturday afternoon at the Al Quoz showroom.",
  )

  // 4 — doorstep → service area shown, not a showroom
  await getSlots(
    'Doorstep slots → service area (proof #4)',
    "Hi, I'm Test Driver, +971509998877. I'd like a doorstep test drive of the Jetour T2 petrol in Dubai this Saturday evening.",
  )

  // 5 — Arabic day-name localization of the same chips
  if (showroom.chips.length) {
    const c = showroom.chips[0]
    const ar = slotChipLabel(c, 'rtl')
    line(`\n=== Arabic localization (proof #5) ===\n   ar chip: [${ar.when} · ${ar.where}]`)
  }

  // 2 — TAP: clean prose bubble + hidden slot_id → lock_slot lands on that id
  if (showroom.chips.length) {
    const chip = showroom.chips[0]
    const prose = slotPickText(chip)
    line(`\n=== TAP the chip (proof #2/#6) ===`)
    line(`   visible message (transcript): "${prose}"`)
    line(`   contains the raw id ${chip.slotId}? ${prose.includes(String(chip.slotId))}  (must be false)`)
    line(`   hidden slot_id sent: ${chip.slotId}`)
    const { lockInput } = await turn(showroom.sid, prose, chip.slotId)
    if (lockInput) {
      line(`   lock_slot tool_event input: ${JSON.stringify(lockInput)}`)
      line(`   lock_slot slot_id === tapped ${chip.slotId}? ${lockInput.slot_id === chip.slotId}`)
    } else {
      line('   (agent did not call lock_slot this turn)')
    }
    line(`   → grep the backend audit for this session to see slot_id_pinned:true — session=${showroom.sid}`)
  }

  line('\nSlot harness complete.')
}

void main()
