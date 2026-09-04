// One vehicle photograph, in whichever format the browser can actually decode.
//
// WHY A <picture> AND NOT AN <img>. The stored URL is AVIF, which is ~30% smaller
// than the WebP beside it and is what every current browser takes. Safari before
// 16.4 decodes no AVIF at all and would render the card's empty-state diamond
// instead of a car — a silent failure on exactly the kind of device a QR-code
// attendee might walk up with. The <source> costs one extra line of markup and
// removes that whole class of "it works on my phone".
//
// SIZE IS THE CALLER'S CHOICE, not this component's. A card wants the 480px
// rendition and the dialog wants the 960px one; passing `size` makes that a
// decision at the call site rather than something inferred from CSS that a later
// style change could silently invalidate.

import { fullSrc, webpOf } from '../media'

export function VehiclePhoto({
  src,
  alt,
  className,
  size = 'card',
  loading,
  onClick,
  onError,
}: {
  src: string
  alt: string
  className?: string
  /** 'card' = the 480px rendition (card, compare tile, trim thumbnail);
   *  'full' = the 960px one (detail dialog hero). */
  size?: 'card' | 'full'
  /** 'lazy' on cards, which stack up as a thread grows and mostly sit off
   *  screen; left unset (eager) where the photo IS the thing just opened. */
  loading?: 'lazy' | 'eager'
  onClick?: () => void
  onError?: () => void
}) {
  const url = size === 'full' ? fullSrc(src) : src
  const webp = webpOf(url)
  // ORDER IS THE WHOLE MECHANISM, and getting it backwards is silent. <picture>
  // takes the FIRST <source> the browser can decode and only falls through to
  // the <img> when none matches — so the SMALLER format (AVIF) is the source and
  // the universal one (WebP) is the img. Written the other way round, as this
  // was until it showed up in the layout harness, every modern browser quietly
  // takes the WebP and the AVIF is never fetched by anyone: the whole size win
  // disappears with nothing broken on screen to reveal it.
  //
  // With no WebP sibling (a non-AVIF stored URL, e.g. Salesforce's free-text
  // Image_URL__c) there is nothing to offer and the img carries the URL itself.
  return (
    <picture>
      {webp ? <source srcSet={url} type="image/avif" /> : null}
      <img
        className={className}
        src={webp ?? url}
        alt={alt}
        // async, so decoding a photo never blocks the token stream painting the
        // sentence beside it.
        decoding="async"
        loading={loading}
        onClick={onClick}
        onError={onError}
      />
    </picture>
  )
}
