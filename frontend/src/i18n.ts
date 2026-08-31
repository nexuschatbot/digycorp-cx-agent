// UI label translations (LOC-01, LOC-09).
//
// WHY THESE LIVE HERE AND NOT IN THE DATABASE. CLAUDE.md's rule is that
// structured data drives logic and the language is chosen at speaking time; a
// stored English string cannot be spoken into an Arabic conversation. That rule
// applies to the UI too. So the backend sends ENUMS and NUMBERS — fuel:
// "phev", seats 5-7, price 146000 — and the labels are chosen here, per
// language, at render time. Nothing in this file is fetched.
//
// This is the one place bilingual burden moves from the model to the UI. Aisha
// still phrases everything she SAYS; the dialog is not speech, it is a data
// panel, so it labels its own fields.
//
// NUMBERS STAY WESTERN (150,000 — not ١٥٠٬٠٠٠). That is standard Gulf usage and
// keeps the dialog consistent with the +971 mobile format and the TD-YYYY-NNNN
// reference the agent reads back (LOC-09).
//
// PENDING NATIVE REVIEW before 30 Jul. Drafted rather than machine-translated,
// but a Gulf speaker should confirm the register — and Jetour UAE may publish
// official Arabic for the marketing feature names (Tank Turn, Park Cool, Magic
// Electric Door) and the trim names, which the seed currently leaves in Latin.

export type Lang = 'en' | 'ar'

export const LABELS = {
  en: {
    price: 'Price',
    seats: 'Seats',
    fuel: 'Fuel',
    bodyType: 'Body type',
    availableAt: 'Available at',
    highlights: 'Highlights',
    bookTestDrive: 'Book test drive',
    close: 'Close',
    loading: 'Loading…',
    unavailable: 'Details are not available right now.',
    seatsUnit: 'seats',
    from: 'From',
    upTo: 'Up to',
    currency: 'AED',
    // Booking card. FOUR headings, chosen by WHEN THE SLOT IS, not by which
    // tool drew the card: a booking looked up the day before it happens is
    // upcoming, and calling it "Previous" (as the original mockup did) is simply
    // false. See bookingsTitle().
    currentBooking: 'Current Booking:',
    currentBookings: 'Current Bookings:',
    previousBooking: 'Previous Booking:',
    previousBookings: 'Previous Bookings:',
    // "1 of 2" — the card's POSITION in a stack, not the stack's size. Only
    // rendered when there IS more than one; a lone card shows a bare "1",
    // because "1 of 1" is a count dressed up as a position.
    ofTotal: 'of',
    nameLabel: 'Name:',
    mobileLabel: 'Mobile:',
    vehicleLabel: 'Vehicle Model:',
    timeSlotLabel: 'Time Slot:',
    mapLinkLabel: 'Show Room Map Link:',
    doorstepLabel: 'Doorstep visit:',
    addToCalendar: 'Book In My Calendar',
    refLabel: 'Reference:',
    // Showroom choice card
    select: 'Select',
    selected: 'Selected',
    // Vehicle dialog extras (2026-08-29)
    economy: 'Fuel economy',
    kmPerL: 'km/L',
    interiorColours: 'Interior colours',
    trims: 'Trims & prices',
    comingSoon: 'Coming soon',
  },
  ar: {
    price: 'السعر',
    seats: 'المقاعد',
    fuel: 'الوقود',
    bodyType: 'نوع الهيكل',
    availableAt: 'متوفر في',
    highlights: 'أبرز المواصفات',
    bookTestDrive: 'احجز تجربة قيادة',
    close: 'إغلاق',
    loading: 'جارٍ التحميل…',
    unavailable: 'التفاصيل غير متوفرة حالياً.',
    seatsUnit: 'مقاعد',
    from: 'تبدأ من',
    upTo: 'حتى',
    currency: 'درهم',
    // Booking card — see the English block for why there are four.
    currentBooking: 'حجزك الحالي:',
    currentBookings: 'حجوزاتك الحالية:',
    previousBooking: 'حجز سابق:',
    previousBookings: 'الحجوزات السابقة:',
    ofTotal: 'من',
    nameLabel: 'الاسم:',
    mobileLabel: 'رقم الجوال:',
    vehicleLabel: 'السيارة:',
    timeSlotLabel: 'الموعد:',
    mapLinkLabel: 'موقع المعرض على الخريطة:',
    doorstepLabel: 'زيارة منزلية:',
    addToCalendar: 'أضف إلى تقويمي',
    refLabel: 'رقم الحجز:',
    // Showroom choice card
    select: 'اختيار',
    selected: 'تم الاختيار',
    // Vehicle dialog extras (2026-08-29)
    economy: 'استهلاك الوقود',
    kmPerL: 'كم/لتر',
    interiorColours: 'ألوان المقصورة',
    trims: 'الفئات والأسعار',
    comingSoon: 'قريباً',
  },
} as const

/** The two client-side lifecycle lines (rule 19b): the 2-minute "still there?"
 *  check-in and the 3-minute farewell.
 *
 *  CANNED AND CLIENT-SIDE ON PURPOSE. These are the only customer-visible lines
 *  in the app that Aisha does not write. An abandoned session must not trigger a
 *  backend round-trip, and a canned string cannot hang mid-demo the way a live
 *  generation can — a nudge that arrives 30 seconds late, to a customer who has
 *  already walked away, is worse than no nudge.
 *
 *  BILINGUAL, unlike the English-only line this replaced: these fire on true
 *  abandonment, which the presenter will not script, so an Arabic conversation
 *  ending in English is exactly the failure nobody rehearses. The language comes
 *  from the last thing actually said in the thread (conversationLang in
 *  useChat), and both render through the same dirFor() path as any bubble. */
export const LIFECYCLE_LINES: Record<Lang, { nudge: string; closing: string }> = {
  en: {
    nudge: "Are you still there? I'm happy to carry on whenever you're ready.",
    closing:
      "I'll close our chat here for now — thank you for visiting Elite, and reach out anytime.",
  },
  ar: {
    nudge: 'هل ما زلت معنا؟ يسعدني أن نكمل متى ما كنت جاهز.',
    closing: 'سأغلق محادثتنا هنا الآن — شكراً لزيارتك إيليت، وتواصل معنا في أي وقت.',
  },
}

/** Human takeover (2026-08-31): the muted line announcing a person joined the
 *  chat. Client-side and ephemeral like the idle-ladder lines above — the
 *  FACT (the takeover audit row, the staff turns) lives server-side; this line
 *  is only the moment being narrated, so it is never persisted or replayed.
 *  A function of the name because both languages inflect around it. */
export const TAKEOVER_LINES: Record<Lang, { joined: (name: string) => string }> = {
  en: {
    joined: (name) => `${name} from Elite has joined the chat — you're now talking with a person.`,
  },
  ar: {
    joined: (name) => `انضم ${name} من إيليت إلى المحادثة — أنت الآن تتحدث مع شخص.`,
  },
}

/** Voice-note chrome (2026-07-21). CONTROLS AND STATUS ONLY — never anything
 *  Aisha would say. When a recording cannot be transcribed she answers in her
 *  own words, in the thread's own language, because a failure mid-conversation
 *  deserves a real reply rather than a fixed string; the only canned lines in
 *  this app remain the two idle-ladder ones above, which fire when nobody is
 *  there to answer at all.
 *
 *  `unsupported` is the HTTPS blocker made visible: getUserMedia is blocked on
 *  plain HTTP everywhere except localhost, so QR attendees on http://<ip> get
 *  this instead of a dead button. Naming the cause on screen beats a control
 *  that silently does nothing on stage. */
export const VOICE_LABELS: Record<Lang, {
  record: string
  stop: string
  cancel: string
  play: string
  pause: string
  recording: string
  transcribing: string
  denied: string
  unsupported: string
  sendFailed: string
  readAloud: string
  stopReading: string
}> = {
  en: {
    record: 'Record a voice note',
    stop: 'Stop recording',
    cancel: 'Cancel recording',
    play: 'Play voice note',
    pause: 'Pause voice note',
    recording: 'Recording…',
    transcribing: 'Transcribing…',
    denied: 'Microphone unavailable — you can type instead.',
    unsupported: 'Recording needs a secure (https) connection — you can type instead.',
    // A transport fault, so the model was never reached and nothing was stored.
    // Says what happened and what to do, and does NOT apologise on Aisha's
    // behalf for something she had no part in.
    sendFailed: "That voice note didn't send. Please try again, or type your message.",
    // Read-aloud (2026-07-21). These are per-MESSAGE labels, so unlike the
    // composer chrome they already render in the message's own language.
    readAloud: 'Read this message aloud',
    stopReading: 'Stop reading',
  },
  ar: {
    record: 'تسجيل رسالة صوتية',
    stop: 'إيقاف التسجيل',
    cancel: 'إلغاء التسجيل',
    play: 'تشغيل الرسالة الصوتية',
    pause: 'إيقاف مؤقت',
    recording: 'جارٍ التسجيل…',
    transcribing: 'جارٍ التحويل…',
    denied: 'الميكروفون غير متاح — يمكنك الكتابة بدلاً من ذلك.',
    unsupported: 'التسجيل يحتاج اتصالاً آمناً (https) — يمكنك الكتابة بدلاً من ذلك.',
    sendFailed: 'لم يتم إرسال الرسالة الصوتية. حاول مرة أخرى، أو اكتب رسالتك.',
    readAloud: 'استمع إلى هذه الرسالة',
    stopReading: 'إيقاف الاستماع',
  },
}

/** Voice-call chrome (voice phase). CONTROLS AND STATUS ONLY, like the
 *  voice-note labels above — everything Aisha SAYS on a call is her own text,
 *  spoken; nothing here is ever in her voice. The Arabic is written natively
 *  in the same register as the rest of this file and is PENDING LAYLA'S
 *  REVIEW, like the notification copy before it. */
export const CALL_LABELS: Record<Lang, {
  call: string
  hangUp: string
  mute: string
  unmute: string
  connecting: string
  listening: string
  thinking: string
  speaking: string
  muted: string
  ended: string
  unsupported: string
  micDenied: string
  connectionLost: string
  unavailable: string
}> = {
  en: {
    call: 'Start a voice call',
    hangUp: 'End call',
    mute: 'Mute microphone',
    unmute: 'Unmute microphone',
    connecting: 'Connecting…',
    listening: 'Listening…',
    thinking: 'Thinking…',
    speaking: 'Aisha is speaking…',
    muted: 'Microphone muted',
    ended: 'Call ended',
    unsupported: 'Calls need a secure (https) connection — you can type instead.',
    micDenied: 'Microphone unavailable — you can type instead.',
    connectionLost: 'The call dropped. You can call again or carry on typing.',
    unavailable: 'Calls are not available right now — you can type instead.',
  },
  ar: {
    call: 'بدء مكالمة صوتية',
    hangUp: 'إنهاء المكالمة',
    mute: 'كتم الميكروفون',
    unmute: 'إلغاء كتم الميكروفون',
    connecting: 'جارٍ الاتصال…',
    listening: 'أسمعك…',
    thinking: 'لحظة من فضلك…',
    speaking: 'عائشة تتحدث…',
    muted: 'الميكروفون مكتوم',
    ended: 'انتهت المكالمة',
    unsupported: 'المكالمات تحتاج اتصالاً آمناً (https) — يمكنك الكتابة بدلاً من ذلك.',
    micDenied: 'الميكروفون غير متاح — يمكنك الكتابة بدلاً من ذلك.',
    connectionLost: 'انقطعت المكالمة. يمكنك الاتصال مرة أخرى أو متابعة الكتابة.',
    unavailable: 'المكالمات غير متاحة حالياً — يمكنك الكتابة بدلاً من ذلك.',
  },
}

/** Why a call ended, said OUT LOUD in the thread (client-side system lines,
 *  the voice-note sendFailed pattern). A call that vanishes without a word
 *  reads as a broken button — which is exactly how the missing mapping read
 *  in dev, where CALL_ENABLED defaults off and the server's polite refusal
 *  had nowhere to appear. Keyed by the wire reason; reasons with no entry
 *  (hangup, closed, session_closed) end silently ON PURPOSE — those endings
 *  are either the customer's own act or already narrated by the composer. */
export const CALL_END_LINES: Record<Lang, Partial<Record<string, string>>> = {
  en: {
    call_disabled:
      "Calls aren't switched on for this server — you can type instead.",
    voice_not_configured:
      "Calling isn't set up on this server — you can type instead.",
    mic_denied: 'Microphone unavailable — you can type instead.',
    connection_failed: "The call couldn't connect. You can try again or keep typing.",
    connection_lost: 'The call dropped. You can call again or carry on typing.',
    silence: 'The call ended after a quiet spell — start another anytime.',
    max_minutes: 'The call reached its time limit — start another anytime.',
    rate_limited:
      'The line is busy at our end just now — try calling again in a minute, or keep typing.',
    error: 'The call hit a problem on our side — you can call again or type.',
  },
  ar: {
    call_disabled: 'المكالمات غير مفعّلة على هذا الخادم — يمكنك الكتابة بدلاً من ذلك.',
    voice_not_configured: 'المكالمات غير مهيأة على هذا الخادم — يمكنك الكتابة بدلاً من ذلك.',
    mic_denied: 'الميكروفون غير متاح — يمكنك الكتابة بدلاً من ذلك.',
    connection_failed: 'تعذّر الاتصال. يمكنك المحاولة مرة أخرى أو متابعة الكتابة.',
    connection_lost: 'انقطعت المكالمة. يمكنك الاتصال مرة أخرى أو متابعة الكتابة.',
    silence: 'انتهت المكالمة بعد فترة هدوء — يمكنك بدء مكالمة أخرى في أي وقت.',
    max_minutes: 'بلغت المكالمة حدّها الزمني — يمكنك بدء مكالمة أخرى في أي وقت.',
    rate_limited: 'الخط مشغول من جهتنا حالياً — حاول الاتصال مرة أخرى بعد دقيقة، أو تابع الكتابة.',
    error: 'واجهت المكالمة مشكلة من جهتنا — يمكنك الاتصال مرة أخرى أو الكتابة.',
  },
}

/** The voice-call summary row (2026-07-29): the DB stores the fact
 *  ("call:<seconds>", a Speaker.system marker turn) and this phrases it per
 *  language at render time — the no-display-text-in-data rule. It is the one
 *  trace an ephemeral call leaves in the thread, beside any booking card. */
export function callSummaryLine(lang: Lang, seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  const dur = m > 0 ? `${m}:${String(s).padStart(2, '0')} min` : `${s} sec`
  return lang === 'ar' ? `مكالمة صوتية — ${dur}` : `Voice call — ${dur}`
}

/** Powertrain enum -> label. The backend deliberately ships "petrol"/"phev"
 *  rather than prose: there is no electric, diesel or mild-hybrid row in the
 *  catalogue, and the honest answer to "do you have an EV" is no (FR-CX-07). */
export const FUEL_LABELS: Record<string, { en: string; ar: string }> = {
  petrol: { en: 'Petrol', ar: 'بنزين' },
  phev: { en: 'Plug-in hybrid', ar: 'هجين قابل للشحن' },
}

export function fuelLabel(fuel: string | null, lang: Lang): string | null {
  if (!fuel) return null
  return FUEL_LABELS[fuel]?.[lang] ?? null
}

/** "AED 146,000–179,000" / "146,000–179,000 درهم", or null when the price is
 *  not published — the dialog then shows NO price line. Never "None", never a
 *  fabricated figure (FR-CX-07). Mirrors priceLabel() in cards.ts, which does
 *  the same job for the compact card in English. */
export function priceLine(min: number | null, max: number | null, lang: Lang): string | null {
  if (min == null && max == null) return null
  const n = (v: number) => v.toLocaleString('en-US')
  const t = LABELS[lang]

  let amount: string
  if (min != null && max != null) amount = min === max ? n(min) : `${n(min)}–${n(max)}`
  else if (min != null) amount = `${t.from} ${n(min)}`
  else amount = `${t.upTo} ${n(max as number)}`

  // Unit leads in English ("AED 146,000"), trails in Arabic ("146,000 درهم").
  return lang === 'ar' ? `${amount} ${t.currency}` : `${t.currency} ${amount}`
}

/** "5 seats" / "5–7 مقاعد". */
export function seatsLine(min: number, max: number, lang: Lang): string {
  const range = min === max ? `${min}` : `${min}–${max}`
  return `${range} ${LABELS[lang].seatsUnit}`
}

/** Day names for the booking card's slot line. Written out rather than taken
 *  from toLocaleDateString('ar'): the browser's Arabic locale data varies by
 *  platform and would give us a different register on iOS than on Android, in
 *  the one place the demo audience is reading over someone's shoulder. */
const DAY_NAMES: Record<Lang, string[]> = {
  en: ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'],
  ar: ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت'],
}

// Own arrays rather than toLocaleDateString for the same reason as DAY_NAMES
// (see the file header): the browser's Arabic locale month data is inconsistent,
// so the display names live here and are drawn per language at render time.
const MONTH_NAMES: Record<Lang, string[]> = {
  en: ['January', 'February', 'March', 'April', 'May', 'June',
       'July', 'August', 'September', 'October', 'November', 'December'],
  ar: ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
       'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'],
}

const MERIDIEM: Record<Lang, { am: string; pm: string }> = {
  en: { am: 'am', pm: 'pm' },
  ar: { am: 'ص', pm: 'م' },
}

function clock(d: Date, lang: Lang): string {
  const h24 = d.getHours()
  const h12 = h24 % 12 || 12
  const mm = String(d.getMinutes()).padStart(2, '0')
  const suffix = MERIDIEM[lang][h24 < 12 ? 'am' : 'pm']
  return `${h12}:${mm} ${suffix}`
}

/** "Sunday, 7:00 pm - 8:00 pm" / "الأحد، ٧:٠٠ م - ٨:٠٠ م" (Western digits — see
 *  the file header). Built from ISO timestamps at RENDER time, per language,
 *  because the backend deliberately ships instants rather than display text: a
 *  stored "Sunday, 7:00 pm" could not be redrawn in an Arabic thread (LOC-01),
 *  and the 12-hour clock with a day name is the UAE format (LOC-09).
 *
 *  Falls back to the start alone when there is no end, and to null when there is
 *  no start — the card then omits the line rather than printing "Invalid Date". */
export function slotRange(startIso: string | null, endIso: string | null, lang: Lang): string | null {
  if (!startIso) return null
  const start = new Date(startIso)
  if (Number.isNaN(start.getTime())) return null

  const day = DAY_NAMES[lang][start.getDay()]
  // Full calendar date beside the weekday: "Thursday, 6 August 2026". Uses the
  // same local getters as getDay()/clock(), so the date, weekday and time stay
  // consistent with each other on the card.
  const date = `${start.getDate()} ${MONTH_NAMES[lang][start.getMonth()]} ${start.getFullYear()}`
  const from = clock(start, lang)
  // Arabic comma (U+060C), not the Latin one — it sits on the opposite baseline
  // and a Latin comma in Arabic text reads as a typo to a native speaker.
  const comma = lang === 'ar' ? '،' : ','
  const when = `${day}${comma} ${date}`

  if (!endIso) return `${when}${comma} ${from}`
  const end = new Date(endIso)
  if (Number.isNaN(end.getTime())) return `${when}${comma} ${from}`
  return `${when}${comma} ${from} - ${clock(end, lang)}`
}

/** The showroom's timezone. Slot timestamps come back NAIVE ("2026-07-22T09:00:00")
 *  and mean Dubai wall-clock time — the time printed on the card is the time the
 *  customer turns up at the showroom. */
const SHOWROOM_TZ = 'Asia/Dubai'

/** "2026-07-22T09:00:00" -> "20260722T090000". String surgery, NOT via Date.
 *
 *  Deliberate, and the subtle part of this file. Round-tripping through
 *  `new Date(...).toISOString()` would reinterpret a naive stamp as the
 *  BROWSER's local time and convert from there — correct on a UAE phone, wrong
 *  by hours on a visiting attendee's phone still set to London. QR-code demo:
 *  we do not get to assume the device timezone. So we pass the wall-clock time
 *  through untouched and tell Google which timezone it is in (ctz), which is
 *  the one combination that is right on every device. */
function wallClockStamp(iso: string): string | null {
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/)
  if (!m) return null
  const [, y, mo, d, h, mi, s] = m
  return `${y}${mo}${d}T${h}${mi}${s ?? '00'}`
}

/** A Google Calendar "add event" URL for a booking.
 *
 *  WHY GOOGLE CALENDAR AND NOT AN .ics DOWNLOAD (decided 21 Jul 2026). An ICS
 *  file is the more universal, more "really your calendar" answer, but on some
 *  Android browsers it lands in the downloads tray and has to be hunted for —
 *  a clumsy pause in a demo where attendees are on their own unknown phones.
 *  A URL opens a filled-in page every time, on every device, which is the
 *  property that matters on stage.
 *
 *  Wall-clock times plus an explicit ctz — see wallClockStamp for why that beats
 *  converting to UTC here. Returns null when there is no usable start; the
 *  button is then not rendered at all rather than linking to a broken event. */
export function calendarUrl(opts: {
  title: string
  startIso: string | null
  endIso: string | null
  location?: string | null
  details?: string | null
}): string | null {
  if (!opts.startIso) return null
  const start = wallClockStamp(opts.startIso)
  if (!start) return null

  // Falls back to the start when there is no end: a zero-length event still
  // lands on the right day at the right time, which is the part that matters.
  // Better than inventing a duration the booking never specified.
  const end = (opts.endIso && wallClockStamp(opts.endIso)) || start

  const params = new URLSearchParams({
    action: 'TEMPLATE',
    text: opts.title,
    dates: `${start}/${end}`,
    ctz: SHOWROOM_TZ,
  })
  if (opts.location) params.set('location', opts.location)
  if (opts.details) params.set('details', opts.details)
  return `https://calendar.google.com/calendar/render?${params.toString()}`
}

/** The bullets in the conversation's language. Falls back to English if a row
 *  somehow lacks Arabic — better a readable English bullet than a blank panel.
 *  The seed loader requires the Arabic field, so this should not fire. */
export function features(
  en: string[],
  ar: string[],
  lang: Lang,
): string[] {
  if (lang === 'ar' && ar.length > 0) return ar
  return en
}
