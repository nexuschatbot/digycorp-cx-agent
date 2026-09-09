// Vehicle detail dialog (rule 18) — opened by a card's "View more".
//
// BROWSING IS NOT A CONVERSATION. This used to send "Tell me more about the T2"
// as a chat message, which spent a whole agent turn (two model calls) fetching
// detail the browser had already been given and discarded. The answer is fully
// determined by the vehicle id, so it never needed a language model: the dialog
// fetches /api/vehicles/{id} directly and costs zero tokens.
//
// Aisha stays SILENT while the dialog is open, and that is deliberate. Nobody
// expects a dialog to talk. She speaks again the moment the customer asks
// something real — or taps "Book test drive", which is a commit, not a browse,
// and is the one action here that starts a conversation.
//
// GROUNDING IS UNCHANGED (FR-CX-07). Every value is rendered from the tool/REST
// payload — an unpublished price shows NO price line rather than "None", and a
// missing image collapses to a placeholder rather than a broken URL. The dialog
// displays data; it never narrates and never infers.
//
// BILINGUAL (LOC-01, LOC-09). The backend ships enums and numbers; the labels
// come from i18n.ts per language. Latin model names inside Arabic lines are
// wrapped in <bdi> so the bidi algorithm isolates them — without it, trailing
// punctuation jumps to the wrong end of "Jetour T2" and the line looks broken.

import { useEffect, useRef, useState } from 'react'

import { fetchVehicle, type VehicleDetail } from '../api'
import { LABELS, features, fuelLabel, priceLine, seatsLine, type Lang } from '../i18n'
import { VehiclePhoto } from './VehiclePhoto'

// Two aria-labels used only here — kept local rather than added to i18n.ts,
// whose LABELS table is consumed elsewhere and should not grow for this.
const NAV_LABELS = {
  en: { previous: "Previous car", next: "Next car" },
  ar: { previous: "السيارة السابقة", next: "السيارة التالية" },
} as const

interface Props {
  vehicleId: number
  /** The conversation's language, from the card that opened this. */
  lang: Lang
  onClose: () => void
  /** Commit: hands the model name back so the caller can start the booking. */
  onBook: (vehicle: VehicleDetail) => void
  /** Whether we are taking test drives at all (config.TEST_DRIVE_ENABLED,
   *  served by /api/config). False hides the one commit in this dialog —
   *  a button that starts a booking we would immediately hand to a
   *  colleague is a promise the page has no business making. Browsing is
   *  unaffected: the dialog is a catalogue read and stays fully useful. */
  canBook?: boolean
  /** The row this was opened from, so the customer can browse it in place. */
  siblings?: { vehicleId?: number | null }[]
  index?: number
  onIndex?: (i: number) => void
}

/** An unnamed trim renders under the model's own short name — "X70 FL" —
 *  because "the only version" has nothing else to be called. */
function displayNameForTrim(model: string): string {
  return model.replace(/^Jetour\s+/, '')
}


export default function VehicleDialog({
  vehicleId,
  lang,
  onClose,
  onBook,
  canBook = true,
  // The row this was opened from, so the customer can move along it without
  // closing. Both optional: a dialog opened from a single card (a booking
  // receipt, a direct link) simply has no siblings and renders no arrows.
  siblings,
  index,
  onIndex,
}: Props) {
  const [detail, setDetail] = useState<VehicleDetail | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  // Which photo the big image shows: 0 = hero, then the other exteriors, then
  // the interiors. Reset whenever the dialog moves to a different car.
  const [frame, setFrame] = useState(0)
  const closeRef = useRef<HTMLButtonElement>(null)
  const t = LABELS[lang]
  // Two or more, because "1 / 1" with dead arrows is worse than no arrows.
  const hasSiblings = !!siblings && siblings.length > 1 && index != null
  const dir = lang === 'ar' ? 'rtl' : 'ltr'

  useEffect(() => {
    let live = true
    setState('loading')
    setFrame(0)
    fetchVehicle(vehicleId).then((d) => {
      if (!live) return // unmounted, or the customer opened a different car
      if (d) {
        setDetail(d)
        setState('ready')
      } else {
        setState('error')
      }
    })
    return () => {
      live = false
    }
  }, [vehicleId])

  // Escape closes. Mirrors the tap-outside affordance and matters on the demo's
  // phones, where the close button can sit under a browser chrome overlay.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    closeRef.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const price = detail ? priceLine(detail.price_aed_min, detail.price_aed_max, lang) : null
  const fuel = detail ? fuelLabel(detail.fuel, lang) : null
  const bullets = detail ? features(detail.headline_features, detail.headline_features_ar, lang) : []

  // The photo set the big image cycles through: hero, then the rest of the
  // exteriors, then the interiors. EVERY photo we hold of the car, in that
  // order — outside before inside, which is the order someone looks at a car in.
  //
  // The exteriors were added 2026-09-04 and fixed a quiet waste: models have
  // carried two or three exterior frames since the first seed, and this
  // carousel's hero-then-interiors shape meant only the first was ever
  // reachable. The card's own carousel is deliberately NOT changed — that one
  // is opened by an interior question, so interiors are what it should show.
  const frames: string[] = detail
    ? [
        ...(detail.image_url ? [detail.image_url] : []),
        ...(detail.exterior_image_urls ?? []),
        ...(detail.interior_image_urls ?? []),
      ]
    : []
  // Alt text has to follow the frame ORDER, not assume it. It said "interior N"
  // for every frame past the hero, which was true while the carousel ran
  // hero-then-interiors and became wrong the moment the exteriors went in
  // between — a screen reader would have announced the car's other angles as
  // its cabin.
  const interiorCount = detail?.interior_image_urls?.length ?? 0
  const frameAlt = (i: number): string => {
    const name = detail?.name ?? ''
    if (i === 0) return name
    const firstInterior = frames.length - interiorCount
    return i >= firstInterior ? `${name} interior` : `${name} exterior`
  }
  const current = frames.length > 0 ? frames[Math.min(frame, frames.length - 1)] : null
  const step = (d: number) => setFrame((i) => (i + d + frames.length) % frames.length)

  // "AED 79,000" per trim — reuses the single-value path of priceLine.
  const trimPrice = (aed: number | null) => (aed == null ? null : priceLine(aed, aed, lang))
  const trims = detail?.variants ?? []
  // One unnamed trim is "the only version" — a section naming nothing under a
  // price already shown above would be noise.
  const showTrims = trims.length > 1 || (trims.length === 1 && trims[0].name != null)

  return (
    <div
      className="vdlg-backdrop"
      // Tap outside to dismiss — but only on the backdrop itself, never on a
      // click that bubbled up from inside the panel.
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="vdlg" role="dialog" aria-modal="true" dir={dir}>
        {/* Loading / error keep a plain header bar; the ready state's chrome
            all floats over the hero photo instead. */}
        {state !== 'ready' && (
          <header className="vdlg-head">
            <bdi className="vdlg-title">{detail?.name ?? ''}</bdi>
            <button ref={closeRef} className="vdlg-close" onClick={onClose} aria-label={t.close}>
              ×
            </button>
          </header>
        )}
        {state === 'loading' && <p className="vdlg-note">{t.loading}</p>}
        {state === 'error' && <p className="vdlg-note vdlg-note--error">{t.unavailable}</p>}

        {state === 'ready' && detail && (
          <>
            {/* FULL-BLEED HERO. The photo runs edge to edge with the name,
                body line and price pill overlaid on a bottom scrim — the car
                sells the card. It is a CAROUSEL over every photo held of the
                car — hero, other exteriors, then interiors: click the photo to
                advance, arrows both ways, thumbnails below jump. All browse,
                nothing commits. */}
            <div className="vdlg-hero">
              {current ? (
                <VehiclePhoto
                  className="vdlg-hero-img"
                  src={current}
                  alt={frameAlt(frame)}
                  size="full"
                  onClick={() => frames.length > 1 && step(1)}
                />
              ) : (
                <div className="vdlg-hero-img vdlg-hero-img--empty" aria-hidden="true">
                  ◆
                </div>
              )}
              <div className="vdlg-hero-scrim" aria-hidden="true" />

              <button
                ref={closeRef}
                className="vdlg-close vdlg-close--float"
                onClick={onClose}
                aria-label={t.close}
              >
                ×
              </button>

              {/* MOVE ALONG THE ROW IN PLACE — same semantics as before, now
                  floating on the photo. DOM order, not screen order, so the
                  reading-start side holds "previous" in both languages. */}
              {hasSiblings && (
                <div className="vdlg-nav vdlg-nav--float">
                  <button
                    type="button"
                    className="vdlg-navbtn"
                    onClick={() => onIndex?.((index! - 1 + siblings!.length) % siblings!.length)}
                    aria-label={NAV_LABELS[lang].previous}
                  >
                    ‹
                  </button>
                  <span className="vdlg-navpos">
                    {(index ?? 0) + 1} / {siblings!.length}
                  </span>
                  <button
                    type="button"
                    className="vdlg-navbtn"
                    onClick={() => onIndex?.((index! + 1) % siblings!.length)}
                    aria-label={NAV_LABELS[lang].next}
                  >
                    ›
                  </button>
                </div>
              )}

              {frames.length > 1 && (
                <div className="vdlg-carousel-nav" dir="ltr">
                  <button
                    type="button"
                    className="vcard-carousel-arrow"
                    aria-label={NAV_LABELS[lang].previous}
                    onClick={() => step(-1)}
                  >
                    ‹
                  </button>
                  <button
                    type="button"
                    className="vcard-carousel-arrow"
                    aria-label={NAV_LABELS[lang].next}
                    onClick={() => step(1)}
                  >
                    ›
                  </button>
                </div>
              )}

              <div className="vdlg-hero-caption">
                <div className="vdlg-hero-text">
                  <bdi className="vdlg-hero-name">{detail.name}</bdi>
                  <span className="vdlg-hero-sub">
                    <bdi>{detail.body_type}</bdi>
                  </span>
                </div>
                {/* No pill at all when the price is unpublished — never "None". */}
                {price && (
                  <span className="vdlg-hero-price">
                    <bdi>{price}</bdi>
                  </span>
                )}
              </div>
            </div>

            <div className="vdlg-content">
              {frames.length > 1 && (
                <div className="int-gallery int-gallery--dialog">
                  {frames.map((src, i) => (
                    <button
                      key={src}
                      type="button"
                      className={i === frame ? 'vdlg-thumb vdlg-thumb--on' : 'vdlg-thumb'}
                      onClick={() => setFrame(i)}
                    >
                      <VehiclePhoto
                        className="int-gallery-img"
                        src={src}
                        alt={frameAlt(i)}
                        loading="lazy"
                      />
                    </button>
                  ))}
                </div>
              )}

              {/* STAT TILES — the three facts a buyer scans first. A tile with
                  no data behind it is omitted, never guessed (FR-CX-07). */}
              <div className="vdlg-stats">
                <div className="vdlg-stat">
                  <span className="vdlg-stat-value">
                    {detail.seats_min === detail.seats_max
                      ? detail.seats_min
                      : `${detail.seats_min}–${detail.seats_max}`}
                  </span>
                  <span className="vdlg-stat-label">{t.seats}</span>
                </div>
                {fuel && (
                  <div className="vdlg-stat">
                    <span className="vdlg-stat-value">{fuel}</span>
                    <span className="vdlg-stat-label">{t.fuel}</span>
                  </div>
                )}
                {/* NO fuel-economy tile (2026-09-01, reversing 2026-08-29): the
                    model-level band spans the trims ("8.3–10.3 km/L" is two
                    different cars), so a tile up here read as one car's figure.
                    The per-trim figure lives in each trim's accordion below. */}
              </div>

              {(detail.interior_colours?.length ?? 0) > 0 && (
                <section className="vdlg-section">
                  <h3 className="vdlg-section-title">{t.interiorColours}</h3>
                  <div className="vdlg-pills">
                    {detail.interior_colours!.map((c) => (
                      <span className="vdlg-pill" key={c}>
                        <bdi>{c}</bdi>
                      </span>
                    ))}
                  </div>
                </section>
              )}

              {/* TRIMS, with their list prices. A trim with no published price
                  says "coming soon", never a number (FR-CX-07). Hidden for a
                  model with one unnamed version — nothing to choose.

                  EACH ROW IS AN ACCORDION (2026-09-01): expanding shows that
                  trim's OWN fuel economy — which is where the per-car km/L
                  went when the model-level tile above was removed. Seats sit
                  ON the collapsed row beside the trim name (same day, on
                  request), not in the panel: always visible, and never stated
                  twice in one box. Native <details>, so keyboard and
                  screen-reader behaviour come free. A row with no per-trim
                  figures renders as the plain row it always was — a chevron
                  that opens onto nothing promises detail we don't have. Rows
                  the data can't fill are OMITTED, never rendered empty
                  (FR-CX-07). */}
              {showTrims && (
                <section className="vdlg-section">
                  <h3 className="vdlg-section-title">{t.trims}</h3>
                  <div className="vdlg-trims">
                    {trims.map((v) => {
                      const specs: Array<[string, string]> = []
                      if (v.km_per_l_min != null && v.km_per_l_max != null) {
                        const kml =
                          v.km_per_l_min === v.km_per_l_max
                            ? `${v.km_per_l_min}`
                            : `${v.km_per_l_min}–${v.km_per_l_max}`
                        specs.push([t.economy, `${kml} ${t.kmPerL}`])
                      }
                      const head = (
                        <>
                          <span className="vdlg-trim-id">
                            {/* ONLY WHERE THE TRIM WAS SHOT SEPARATELY. Most
                                rows carry no photo and show none — filling the
                                gap with the model's car would repeat one
                                picture down every row and imply a difference
                                between the trims that it does not show. */}
                            {v.image_url && (
                              <VehiclePhoto
                                className="vdlg-trim-img"
                                src={v.image_url}
                                alt=""
                                loading="lazy"
                              />
                            )}
                            <bdi className="vdlg-trim-name">
                              {v.name ?? displayNameForTrim(detail.name)}
                            </bdi>
                            {v.seats_min != null && v.seats_max != null && (
                              <span className="vdlg-trim-seats">
                                · <bdi>{seatsLine(v.seats_min, v.seats_max, lang)}</bdi>
                              </span>
                            )}
                          </span>
                          {trimPrice(v.price_aed) ? (
                            <span className="vdlg-trim-price">
                              <bdi>{trimPrice(v.price_aed)}</bdi>
                            </span>
                          ) : (
                            <span className="vdlg-trim-soon">{t.comingSoon}</span>
                          )}
                        </>
                      )
                      if (specs.length === 0) {
                        return (
                          <div className="vdlg-trim" key={v.variant_id}>
                            {head}
                          </div>
                        )
                      }
                      return (
                        <details className="vdlg-trim-acc" key={v.variant_id}>
                          <summary className="vdlg-trim">
                            {head}
                            <span className="vdlg-trim-chevron" aria-hidden="true">
                              ▾
                            </span>
                          </summary>
                          <div className="vdlg-trim-specs">
                            {specs.map(([label, value]) => (
                              <div className="vdlg-trim-spec" key={label}>
                                <span className="vdlg-trim-spec-label">{label}</span>
                                <span className="vdlg-trim-spec-value">
                                  <bdi>{value}</bdi>
                                </span>
                              </div>
                            ))}
                          </div>
                        </details>
                      )
                    })}
                  </div>
                </section>
              )}

              {bullets.length > 0 && (
                <section className="vdlg-section">
                  <h3 className="vdlg-section-title">{t.highlights}</h3>
                  <ul className="vdlg-features">
                    {bullets.map((f, i) => (
                      <li key={i}>
                        <bdi>{f}</bdi>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {/* COMMIT — the one action here that wakes the agent. */}
              {canBook && (
                <button className="vdlg-book" onClick={() => onBook(detail)}>
                  {t.bookTestDrive}
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
