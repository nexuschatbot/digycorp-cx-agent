// Rewrite one of Aisha's messages for SPEECH. Display text is never touched —
// this produces a throwaway string handed to the synthesiser, so what is read
// and what is on screen remain the same message (read-aloud, 2026-07-21).
//
// WHY THIS EXISTS AT ALL. Speech engines read punctuation-joined alphanumerics
// literally, and Aisha's most important sentence is full of them: the booking
// confirmation. Unnormalised, "TD-2026-0142" comes out as one run-on token and
// "+971505550103" as a twelve-digit number — at the exact moment of the demo
// everyone is watching. Three rules cover essentially everything she says that
// reads badly aloud; the fourth (dates, currency) was deliberately NOT added,
// because Arabic number agreement is genuinely hard and a wrong rule sounds
// worse than no rule.
//
// SEPARATORS, NOT WORDS. Every rule here inserts spaces, commas and digits —
// never English or Arabic prose. That is what keeps this file language-neutral:
// a comma is a pause in both languages, so one implementation serves both and
// there is no translated string to drift. It also means this can never put
// words in Aisha's mouth that she did not write, which is the same instinct as
// refusing to synthesise a customer's untranscribable speech.

/** Booking reference: TD-2026-0142.
 *
 *  Read literally this is the single worst string in the app. Split into
 *  "T D, 2026, 0 1 4 2" the engine says the letters, then the year, then the
 *  digits one at a time — which is how a person reads a reference aloud, and
 *  crucially how a listener can WRITE IT DOWN while hearing it. */
function bookingRefs(text: string): string {
  return text.replace(/\bTD-(\d{4})-([A-Z0-9]{4})\b/g, (_m, year: string, seq: string) => {
    return `T D, ${year}, ${seq.split('').join(' ')}`
  })
}

/** UAE mobile: +971505550103 -> "+971, 50, 555, 01 03".
 *
 *  Grouped the way LOC-09 already formats them on screen, so the spoken and
 *  written forms agree. Left as digits rather than spelled words: engines read
 *  short digit groups correctly in both languages, and spelling them would mean
 *  writing number words twice, once per language. */
function mobiles(text: string): string {
  return text.replace(/\+971[\s-]?(\d{2})[\s-]?(\d{3})[\s-]?(\d{4})\b/g, (_m, a, b, c) => {
    const tail = String(c)
    return `+971, ${a}, ${b}, ${tail.slice(0, 2)} ${tail.slice(2)}`
  })
}

/** Clock times: "7:00 pm" -> "7 pm", "7:30 pm" -> "7:30 pm" (unchanged).
 *
 *  Only the o'clock case, and only because ":00" is read as "seven zero zero"
 *  by most engines. Half-past and quarter-hours are already handled correctly,
 *  so touching them would be meddling. Arabic keeps ص/م untouched — they are
 *  already single tokens the voice handles. */
function times(text: string): string {
  // NOTE THE TRAILING LOOKAHEAD, NOT `\b`. JavaScript's `\b` is defined on ASCII
  // word characters only, so between "م" and a following space there is no
  // boundary — both sides are non-word to the engine — and a `\b` here silently
  // failed to match every Arabic time while working perfectly in English. Caught
  // only because the Arabic case was in the sample set; it is exactly the class
  // of bug that ships when a bilingual feature is tested in one language.
  return text.replace(
    /\b(\d{1,2}):00(\s*)(am|pm|AM|PM|ص|م)(?![A-Za-z؀-ۿ])/g,
    '$1$2$3',
  )
}

/** Anything decorative that survived rule 17. Defensive only — the prompt already
 *  forbids markdown and emoji, and the sanitiser strips them — but a stray "**"
 *  read aloud as "star star" is a silly way to lose a demo moment. */
function decoration(text: string): string {
  return text
    .replace(/[*_`#]/g, ' ')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/** Prepare one message for the synthesiser. Pure, cheap, and safe on any input —
 *  a message with none of these patterns comes back materially unchanged. */
export function forSpeech(text: string): string {
  return decoration(times(mobiles(bookingRefs(text))))
}
