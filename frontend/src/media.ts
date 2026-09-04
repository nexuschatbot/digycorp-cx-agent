// Vehicle photography: the two renditions on disk, and how a stored URL names
// the others beside it.
//
// WHAT THE BACKEND STORES is one path per photo — the 480px-wide AVIF, which is
// the card rendition and the one nearly every view ever fetches. Beside it,
// seed/build_media.py writes three siblings with the same stem: a WebP of the
// same size, and an "@full" pair at 960px for the detail dialog. The payload
// carries one URL rather than four because the naming is a build convention, not
// a fact about the car — see build_media.py for the widths and why they are those.
//
// DERIVING IS NOT INVENTING (rule 18). The grounding rule says a URL must come
// from the data and never from the model's imagination; the same rule is what
// catalogue._image_url follows when it turns "seed/media/vehicles/x.avif" into
// "/media/vehicles/x.avif". These helpers do the identical kind of derivation on
// the identical string, in ONE place so no component invents its own scheme. If
// the naming convention ever changes, build_media.py and this file change
// together — and a mismatch is a missing sibling, which <picture> and the card's
// onError already degrade cleanly around.

/** The stored URL is the card rendition, so this is the identity function —
 *  named anyway, so call sites read as a deliberate choice of size rather than
 *  as "whatever the payload happened to hold". */
export function cardSrc(url: string): string {
  return url
}

/** The 960px rendition for the detail dialog, whose panel is min(30rem, 100%)
 *  — 480 CSS px, so 960 device px is the widest any screen can use. */
export function fullSrc(url: string): string {
  return url.replace(/\.avif$/, '@full.avif')
}

/** The WebP beside a given AVIF. Exists for pre-16.4 Safari, which renders no
 *  AVIF at all and would otherwise show the empty-card diamond. Returns null for
 *  a URL that is not an AVIF — Salesforce's Image_URL__c is a free-text column
 *  and may hold anything, in which case there is no sibling to promise. */
export function webpOf(url: string): string | null {
  return url.endsWith('.avif') ? url.replace(/\.avif$/, '.webp') : null
}

/** Fetch the hero set into the browser cache, so a card drawn mid-answer paints
 *  from memory instead of opening a connection while the customer is reading.
 *
 *  DELIBERATELY FIRE-AND-FORGET. A failure here must be invisible: the images
 *  still load normally when a card asks for them, so a rejected request means a
 *  slower card, never a broken one. Nothing awaits this and nothing reports it. */
export function warmHeroCache(urls: string[]): void {
  for (const url of urls) {
    const img = new Image()
    // Low priority: this is speculative work and must never compete with the
    // SSE stream carrying the agent's actual answer.
    img.fetchPriority = 'low'
    img.src = url
  }
}
