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
  /** The row this was opened from, so the customer can browse it in place. */
  siblings?: { vehicleId?: number | null }[]
  index?: number
  onIndex?: (i: number) => void
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="vdlg-row">
      <span className="vdlg-row-label">{label}</span>
      {/* <bdi> so a Latin value inside an Arabic panel keeps its own direction. */}
      <bdi className="vdlg-row-value">{value}</bdi>
    </div>
  )
}

export default function VehicleDialog({
  vehicleId,
  lang,
  onClose,
  onBook,
  // The row this was opened from, so the customer can move along it without
  // closing. Both optional: a dialog opened from a single card (a booking
  // receipt, a direct link) simply has no siblings and renders no arrows.
  siblings,
  index,
  onIndex,
}: Props) {
  const [detail, setDetail] = useState<VehicleDetail | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const closeRef = useRef<HTMLButtonElement>(null)
  const t = LABELS[lang]
  // Two or more, because "1 / 1" with dead arrows is worse than no arrows.
  const hasSiblings = !!siblings && siblings.length > 1 && index != null
  const dir = lang === 'ar' ? 'rtl' : 'ltr'

  useEffect(() => {
    let live = true
    setState('loading')
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
        <header className="vdlg-head">
          {/* The model name is Latin in both languages — isolate it. */}
          <bdi className="vdlg-title">{detail?.name ?? ''}</bdi>
          {/* MOVE ALONG THE ROW IN PLACE. Rendered only when there is a row to
              move along, and the arrows are RTL-agnostic because they are DOM
              order, not screen order: the flex container flips with `dir`, so
              "previous" stays on the reading-start side in both languages. */}
          {hasSiblings && (
            <div className="vdlg-nav">
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
          <button ref={closeRef} className="vdlg-close" onClick={onClose} aria-label={t.close}>
            ×
          </button>
        </header>

        {state === 'loading' && <p className="vdlg-note">{t.loading}</p>}
        {state === 'error' && <p className="vdlg-note vdlg-note--error">{t.unavailable}</p>}

        {state === 'ready' && detail && (
          <>
            {detail.image_url ? (
              <img className="vdlg-img" src={detail.image_url} alt={detail.name} />
            ) : (
              <div className="vdlg-img vdlg-img--empty" aria-hidden="true">
                ◆
              </div>
            )}

            <div className="vdlg-rows">
              {/* No price line at all when unpublished — never "None". */}
              {price && <Row label={t.price} value={price} />}
              <Row label={t.seats} value={seatsLine(detail.seats_min, detail.seats_max, lang)} />
              {fuel && <Row label={t.fuel} value={fuel} />}
              <Row label={t.bodyType} value={detail.body_type} />
            </div>

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

            {detail.showrooms.length > 0 && (
              <section className="vdlg-section">
                <h3 className="vdlg-section-title">{t.availableAt}</h3>
                <ul className="vdlg-showrooms">
                  {detail.showrooms.map((s) => (
                    <li key={s.showroom_id}>
                      <bdi>{s.name}</bdi>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {/* COMMIT — the one action here that wakes the agent. */}
            <button className="vdlg-book" onClick={() => onBook(detail)}>
              {t.bookTestDrive}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
