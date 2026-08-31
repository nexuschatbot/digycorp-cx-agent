// A UUID that does not need HTTPS (2026-08-31).
//
// WHY THIS FILE EXISTS. `crypto.randomUUID()` is a SECURE-CONTEXT-ONLY API. It
// is present on `localhost:5173` — localhost counts as secure — and undefined
// the moment the app is served over plain HTTP from anywhere else. The first
// deploy to the test VM found this the hard way: the bundle loaded, the call at
// useChat's first render threw, React never mounted, and the page was BLANK
// under a 200 with a healthy backend.
//
// The failure is silent in the worst way — a dev machine can never reproduce it.
//
// NOT Math.random(). `crypto.getRandomValues()` IS available in insecure
// contexts — only `randomUUID` and `crypto.subtle` are restricted — so there is
// no reason to weaken this, and a reason not to: `sessionId` is a real
// identifier, not just a React key. `/api/conversation/{id}` returns that
// session's transcript, so a guessable id would hand one customer another
// customer's conversation.
//
// TLS on the test VM is the other half of this and is tracked separately; it is
// what voice (getUserMedia) and nearest-showroom (geolocation) need, and both of
// those already degrade gracefully. This one did not, which is why it is
// guarded here rather than waited on.

/** An RFC 4122 v4 UUID, over HTTP or HTTPS. */
export function newId(): string {
  const c = globalThis.crypto

  if (typeof c?.randomUUID === 'function') return c.randomUUID()

  const bytes = new Uint8Array(16)
  c.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // variant 10xx

  const hex: string[] = []
  for (const b of bytes) hex.push(b.toString(16).padStart(2, '0'))
  const s = hex.join('')
  return `${s.slice(0, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}-${s.slice(16, 20)}-${s.slice(20)}`
}
