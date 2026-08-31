// Speech normalisation harness (read-aloud, 2026-07-21).
//
//   npx tsx scripts/speech_harness.ts
//
// Unlike the other harnesses in this directory this needs NO SERVER and makes no
// network call: `forSpeech` is a pure function, which is precisely why it is
// worth pinning. It rewrites the sentence at the climax of the demo — the
// booking confirmation, reference number and all — and a silent regression here
// is only discovered out loud, in front of an audience.
//
// BOTH LANGUAGES IN EVERY CASE THAT HAS ONE. The first version of `times()`
// used `\b`, which in JavaScript is defined on ASCII word characters only: it
// matched every English time and silently skipped every Arabic one, because
// there is no word boundary between "م" and a space. English-only samples would
// have shipped it.

import { forSpeech } from '../src/speech-text'

interface Case {
  name: string
  input: string
  expect: string
}

const CASES: Case[] = [
  // The reference: unnormalised, this is the worst string in the app, and it
  // appears in the one message people most want read back to them.
  {
    name: 'booking ref (en)',
    input: 'Your booking reference is TD-2026-0142.',
    expect: 'Your booking reference is T D, 2026, 0 1 4 2.',
  },
  {
    name: 'booking ref inside Arabic',
    input: 'حجزك برقم TD-2026-0142 مؤكد.',
    expect: 'حجزك برقم T D, 2026, 0 1 4 2 مؤكد.',
  },
  {
    name: 'uae mobile',
    input: "We'll text you on +971505550103 before we arrive.",
    expect: "We'll text you on +971, 50, 555, 01 03 before we arrive.",
  },
  {
    name: 'o-clock time (en)',
    input: 'Saturday at 7:00 pm at Jetour Deira.',
    expect: 'Saturday at 7 pm at Jetour Deira.',
  },
  {
    // THE REGRESSION THIS FILE EXISTS FOR.
    name: 'o-clock time (ar) — the \\b trap',
    input: 'يوم السبت الساعة 7:00 م في ديرة.',
    expect: 'يوم السبت الساعة 7 م في ديرة.',
  },
  {
    name: 'half past is left alone',
    input: 'Saturday at 7:30 pm.',
    expect: 'Saturday at 7:30 pm.',
  },
  {
    name: 'ordinary sentence is untouched',
    input: 'Of course — would you prefer the showroom, or shall we bring the car to you?',
    expect: 'Of course — would you prefer the showroom, or shall we bring the car to you?',
  },
  {
    name: 'stray markdown is stripped (rule 17 defence)',
    input: 'The **Jetour T2** is available.',
    expect: 'The Jetour T2 is available.',
  },
  {
    name: 'full confirmation, everything at once',
    input: 'Perfect. Your Jetour T2 is confirmed for Saturday at 7:00 pm, reference TD-2026-0142. We will message +971505550103.',
    expect: 'Perfect. Your Jetour T2 is confirmed for Saturday at 7 pm, reference T D, 2026, 0 1 4 2. We will message +971, 50, 555, 01 03.',
  },
]

let failed = 0
for (const c of CASES) {
  const got = forSpeech(c.input)
  const ok = got === c.expect
  if (!ok) failed++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${c.name}`)
  if (!ok) {
    console.log(`      in       ${c.input}`)
    console.log(`      expected ${c.expect}`)
    console.log(`      got      ${got}`)
  }
}

console.log(`\n${CASES.length - failed}/${CASES.length} passed`)
process.exit(failed === 0 ? 0 : 1)
