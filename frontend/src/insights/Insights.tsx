// EG management insights portal — the Salesforce-linked view that turns the
// LangSmith evaluator data + customer rankings into plain-language insight for
// non-technical leadership. Two routes: /insights (the rankings list) and
// /insights/:sessionId (the drill-down). Reads the read-only /api/insights/*.
//
// Its OWN page, but dressed to MATCH THE CONSOLE (dark sidebar, grey content,
// white cards with the console's amber accent, big bold numbers) so it reads as
// one product with /admin — while staying self-contained so the Salesforce deep
// link works without the console. Deliberately light (no MUI); the console's
// Inter/Space-Grotesk/Archivo aren't loaded, so this uses the Plex families that
// actually render, exactly as the console ends up doing.
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { Link, NavLink, useParams } from 'react-router-dom'
import './insights.css'

interface FeedbackItem {
  id: number
  session_id: string
  client_name: string
  mobile: string | null
  stars: number
  comment: string | null
  booking_ref: string | null
  created_at: string | null
}
interface Summary {
  total: number
  avg_stars: number
  distribution: Record<string, number>
  positive: number
  neutral: number
  negative: number
}
interface Dim {
  key: string
  label: string
  score: number
  verdict: 'good' | 'warn' | 'bad'
}
interface Driver {
  key: string
  label: string
  avg_score: number
  weak_sessions: number
  total_sessions: number
  verdict: 'good' | 'warn' | 'bad'
}
interface DriversData {
  sessions_analyzed: number
  low_rated_total: number
  drivers: Driver[]
}
interface SessionData {
  session_id: string
  channel: string
  started_at: string | null
  ended_at: string | null
  duration_seconds: number | null
  message_count: number
  rating: {
    stars: number
    comment: string | null
    client_name: string
    mobile: string | null
    booking_ref: string | null
    created_at: string | null
  } | null
  dimensions: Dim[]
  runs_evaluated: number
  why: string
  transcript: { speaker: string; text: string; lang: string | null }[]
}

function pct(n: number, total: number): number {
  return total ? Math.round((n / total) * 100) : 0
}
function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return (
    d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) +
    ', ' +
    d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
  )
}
function fmtDay(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
}
function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}
function fmtDuration(sec: number | null): string {
  if (sec == null || sec < 0) return '—'
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s ? `${m}m ${s}s` : `${m}m`
}

/* ── little inline icons (no icon lib in this bundle) ───────────────────────*/
type Ico = () => ReactNode
const svg = (children: ReactNode): ReactNode => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
)
const IcDash: Ico = () => svg(<><rect x="3" y="3" width="7" height="9" rx="1" /><rect x="14" y="3" width="7" height="5" rx="1" /><rect x="14" y="12" width="7" height="9" rx="1" /><rect x="3" y="16" width="7" height="5" rx="1" /></>)
// Matches the console's MUI "Insights" icon (Admin.tsx) — an upward trend line
// with a sparkle — so the Customer-insights entry looks the SAME on both pages.
const IcChart: Ico = () => svg(
  <>
    <path d="M4 15l4.5-4.5 3 3 4.3-5" />
    <path
      d="M18.6 2.5c.36 1.55.69 1.88 2.24 2.24-1.55.36-1.88.69-2.24 2.24-.36-1.55-.69-1.88-2.24-2.24 1.55-.36 1.88-.69 2.24-2.24z"
      fill="currentColor"
      stroke="none"
    />
  </>,
)
const IcCar: Ico = () => svg(<><path d="M5 13l1.5-4.2A2 2 0 0 1 8.4 7.5h7.2a2 2 0 0 1 1.9 1.3L19 13" /><path d="M4 13h16v4a1 1 0 0 1-1 1h-1a1 1 0 0 1-1-1v-1H7v1a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z" /><path d="M7 16h.01M17 16h.01" /></>)
const IcStore: Ico = () => svg(<><path d="M4 9l1-4.5h14L20 9" /><path d="M5 9v10h14V9" /><path d="M4 9h16" /><path d="M9 19v-5h6v5" /></>)
const IcCal: Ico = () => svg(<><rect x="3" y="4.5" width="18" height="16" rx="2" /><path d="M16 2.5v4M8 2.5v4M3 9.5h18" /><path d="M9 14.5l2 2 4-4" /></>)
const IcTicket: Ico = () => svg(<><path d="M4 8a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v1.5a2 2 0 0 0 0 5V16a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-1.5a2 2 0 0 0 0-5z" /><path d="M14 6v12" /></>)
const IcAudit: Ico = () => svg(<><rect x="4" y="3.5" width="16" height="17" rx="2" /><path d="M8.5 12l2 2 4-4" /></>)
const IcChat: Ico = () => svg(<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />)

// The console's own navigation, mirrored here so this page carries the SAME
// sidebar as /admin (Admin.tsx) — same groups, same order, same items — with
// "Customer insights" as the active one. The other entries link into the
// console routes, exactly as they do from the console itself.
const NAV: { group: string; items: { to: string; label: string; Icon: Ico; end?: boolean }[] }[] = [
  { group: 'Overview', items: [{ to: '/admin', label: 'Dashboard', Icon: IcDash, end: true }] },
  { group: 'Insights', items: [{ to: '/insights', label: 'Customer insights', Icon: IcChart, end: true }] },
  {
    group: 'Catalogue',
    items: [
      { to: '/admin/vehicles', label: 'Vehicles', Icon: IcCar },
      { to: '/admin/showrooms', label: 'Showrooms', Icon: IcStore },
      { to: '/admin/slots', label: 'Slots', Icon: IcCal },
    ],
  },
  { group: 'Operations', items: [{ to: '/admin/bookings', label: 'Bookings', Icon: IcTicket }] },
  { group: 'Agent', items: [{ to: '/admin/audit', label: 'Audit trail', Icon: IcAudit }] },
]

function Stars({ n }: { n: number }) {
  return (
    <span className="ins-stars" aria-label={`${n} of 5`}>
      {'★'.repeat(n)}
      <span className="ins-stars-off">{'★'.repeat(Math.max(0, 5 - n))}</span>
    </span>
  )
}

// EMBEDDED MODE. When the portal is framed inside another app (Salesforce shows
// the Web Tab in an iframe), its own sidebar is redundant — Salesforce already
// provides navigation — so we hide it and let the content read as a native part
// of Salesforce. Detected by being in an iframe (window.self !== window.top);
// `?embed=1`/`?embed=0` force it either way. The standalone page (opened directly
// or from the console) is not framed, so it keeps the full sidebar.
const EMBED: boolean = (() => {
  try {
    const p = new URLSearchParams(window.location.search)
    if (p.get('embed') === '1') return true
    if (p.get('embed') === '0') return false
    return window.self !== window.top
  } catch {
    return true
  }
})()

/** The console-style shell: dark sidebar + grey content with a white page head. */
function Shell({
  title,
  subtitle,
  back,
  // Right-hand side of the page header. Optional so the session drill-down and
  // the error state — neither of which owns a fetch the user would re-run —
  // render exactly as before.
  aside,
  children,
}: {
  title: ReactNode
  subtitle?: ReactNode
  back?: ReactNode
  aside?: ReactNode
  children: ReactNode
}) {
  return (
    <div className={`ins-app${EMBED ? ' ins-app--embed' : ''}`}>
      {!EMBED && (
        <aside className="ins-side">
          <a className="ins-side-brand" href="/admin">
            <img src="/EliteLogo.png" alt="" aria-hidden="true" className="ins-side-logo" />
            <span className="ins-side-brandtext">
              <span className="ins-side-title">Elite Console</span>
              <span className="ins-side-sub">Test drive booking agent</span>
            </span>
          </a>
          <div className="ins-navscroll">
            {NAV.map((section) => (
              <nav className="ins-navlist" key={section.group}>
                <span className="ins-nav-group">{section.group}</span>
                {section.items.map(({ to, label, Icon, end }) => (
                  <NavLink
                    key={to}
                    to={to}
                    end={end}
                    className={({ isActive }) => `ins-navitem${isActive ? ' is-on' : ''}`}
                  >
                    <Icon />
                    <span>{label}</span>
                  </NavLink>
                ))}
              </nav>
            ))}
          </div>
          <div className="ins-navlist ins-navlist--foot">
            <a className="ins-navitem" href="/">
              <IcChat />
              <span>Back to chat</span>
            </a>
          </div>
        </aside>
      )}
      <div className="ins-content">
        <header className="ins-pagehead">
          <div className="ins-pagehead-main">
            {back}
            <h1 className="ins-pagetitle">{title}</h1>
            {subtitle && <p className="ins-pagesub">{subtitle}</p>}
          </div>
          {aside && <div className="ins-pagehead-aside">{aside}</div>}
        </header>
        <main className="ins-main">{children}</main>
      </div>
    </div>
  )
}

/** "Updated 14:32" plus a manual refresh.
 *
 *  WHY THIS EXISTS. The page fetched once on mount and never again, so a tab
 *  left open — which is exactly how a dashboard is used, and how it is used
 *  inside the Salesforce Web Tab — showed figures from whenever it happened to
 *  be opened, with nothing on screen admitting it. Stale numbers that look live
 *  are worse than no numbers: someone reads a rating average from this morning
 *  and believes it is now.
 *
 *  THE TIMESTAMP IS THE POINT, more than the button. Even auto-refreshed, a
 *  dashboard has to say how old it is, because the honest answer is never
 *  "now" — it is "as of the last successful load".
 */
function Refreshed({
  at,
  busy,
  onRefresh,
}: {
  at: Date | null
  busy: boolean
  onRefresh: () => void
}) {
  return (
    <div className="ins-refresh">
      <span className="ins-refresh-at">
        {at
          ? `Updated ${at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`
          : 'Loading…'}
      </span>
      <button
        type="button"
        className="ins-refresh-btn"
        onClick={onRefresh}
        disabled={busy}
        aria-label="Refresh insights"
      >
        {busy ? 'Refreshing…' : 'Refresh'}
      </button>
    </div>
  )
}

/** A console-style stat tile: amber accent stripe, big number, optional badge. */
function Tile({
  label,
  value,
  sub,
  tone,
  badge,
}: {
  label: string
  value: string
  sub?: ReactNode
  tone?: 'ok' | 'error'
  badge?: ReactNode
}) {
  return (
    <div className="ins-tile">
      {badge && <span className="ins-tile-badge">{badge}</span>}
      <span className="ins-tile-label">{label}</span>
      <span className={`ins-tile-value${tone ? ` ins-tile-value--${tone}` : ''}`}>{value}</span>
      {sub && <span className="ins-tile-sub">{sub}</span>}
    </div>
  )
}

function DistBar({ dist, total }: { dist: Record<string, number>; total: number }) {
  return (
    <div className="ins-dist">
      {[5, 4, 3, 2, 1].map((s) => {
        const n = dist[String(s)] ?? 0
        return (
          <div className="ins-dist-row" key={s}>
            <span className="ins-dist-star">{s}★</span>
            <span className="ins-dist-track">
              <span
                className={`ins-dist-fill ins-dist-fill--${s >= 4 ? 'ok' : s <= 2 ? 'bad' : 'mid'}`}
                style={{ width: `${pct(n, total)}%` }}
              />
            </span>
            <span className="ins-dist-n">{n}</span>
          </div>
        )
      })}
    </div>
  )
}

export function InsightsList() {
  const [data, setData] = useState<{ summary: Summary; items: FeedbackItem[] } | null>(null)
  const [drivers, setDrivers] = useState<DriversData | null>(null)
  const [err, setErr] = useState(false)
  const [filter, setFilter] = useState<'all' | 'pos' | 'neg'>('all')

  const [loadedAt, setLoadedAt] = useState<Date | null>(null)
  const [busy, setBusy] = useState(false)

  // One named loader instead of a fire-and-forget effect body, so the button,
  // the interval and the first mount all go through the same path — three ways
  // to load the same page is three ways for them to disagree.
  const load = useCallback(() => {
    setBusy(true)
    // The timestamp is stamped from the FEEDBACK call, which is the one the
    // figures on screen come from. Drivers rides alongside and is allowed to be
    // late or to fail; claiming a fresh load because a secondary request landed
    // would put a time against numbers that had not moved.
    fetch('/api/insights/feedback')
      .then((r) => r.json())
      .then((d) => {
        setData(d)
        setLoadedAt(new Date())
        setErr(false)
      })
      .catch(() => setErr(true))
      .finally(() => setBusy(false))
    // Separate, non-blocking: the driver analysis walks LangSmith per low-rated
    // session, so it lands a beat after the list — the page never waits on it.
    fetch('/api/insights/drivers')
      .then((r) => r.json())
      .then(setDrivers)
      .catch(() => {})
  }, [])

  useEffect(() => {
    load()
    // Sixty seconds: slow enough that the LangSmith walk behind /drivers is
    // never queued on itself, fast enough that a demo left on screen is not
    // visibly lying. The manual button is what covers "I want it NOW".
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [load])

  if (err)
    return (
      <Shell title="Customer Insights">
        <p className="ins-error">Couldn't load insights right now.</p>
      </Shell>
    )
  if (!data)
    return (
      <Shell title="Customer Insights">
        <p className="ins-muted">Loading…</p>
      </Shell>
    )

  const s = data.summary
  const items = data.items.filter((i) =>
    filter === 'all' ? true : filter === 'pos' ? i.stars >= 4 : i.stars <= 2,
  )

  return (
    <Shell
      title="Customer Insights"
      subtitle="Customer rankings and the AI's own read on each conversation, from Salesforce feedback and LangSmith evaluation."
     aside={<Refreshed at={loadedAt} busy={busy} onRefresh={load} />}
    >
      <div className="ins-tiles">
        <Tile label="Average rating" value={s.avg_stars.toFixed(2)} sub={<Stars n={Math.round(s.avg_stars)} />} badge={<span className="ins-tile-ic">★</span>} />
        <Tile label="Reviews" value={String(s.total)} sub={<span className="ins-muted">customers rated Aisha</span>} />
        <Tile label="Positive" value={`${pct(s.positive, s.total)}%`} tone="ok" sub={<span className="ins-muted">{s.positive} rated 4–5★</span>} />
        <Tile label="Negative" value={`${pct(s.negative, s.total)}%`} tone="error" sub={<span className="ins-muted">{s.negative} rated 1–2★</span>} />
      </div>

      <div className="ins-grid2 ins-grid2--list">
        <div className="ins-col">
          <section className="ins-card">
            <span className="ins-eyebrow">Rating spread</span>
            <DistBar dist={s.distribution} total={s.total} />
          </section>

          {drivers && drivers.drivers.length > 0 && (
            <section className="ins-card">
              <span className="ins-eyebrow">Why customers rate low</span>
              <p className="ins-card-note">
                The AI's weakest qualities across <strong>{drivers.sessions_analyzed}</strong> low-rated{' '}
                {drivers.sessions_analyzed === 1 ? 'conversation' : 'conversations'} — the likeliest reasons a
                customer left one or two stars.
              </p>
              <div className="ins-drivers">
                {drivers.drivers.map((dr) => (
                  <div className="ins-driver" key={dr.key}>
                    <div className="ins-driver-head">
                      <span className="ins-driver-label">{dr.label}</span>
                      <span className={`ins-driver-pct ins-driver-pct--${dr.verdict}`}>
                        {Math.round(dr.avg_score * 100)}%
                      </span>
                    </div>
                    <div className="ins-driver-track">
                      <span
                        className={`ins-driver-fill ins-driver-fill--${dr.verdict}`}
                        style={{ width: `${Math.round(dr.avg_score * 100)}%` }}
                      />
                    </div>
                    <span className="ins-driver-sub">
                      Weak in {dr.weak_sessions} of {dr.total_sessions} low-rated{' '}
                      {dr.total_sessions === 1 ? 'session' : 'sessions'}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>

        <div className="ins-col">
          <section className="ins-card ins-card--flush">
            <div className="ins-listhead">
              <span className="ins-eyebrow">All feedback</span>
              <div className="ins-filters">
                {(['all', 'pos', 'neg'] as const).map((f) => (
                  <button
                    key={f}
                    type="button"
                    className={`ins-chip${filter === f ? ' is-on' : ''}`}
                    onClick={() => setFilter(f)}
                  >
                    {f === 'all' ? 'All' : f === 'pos' ? 'Positive' : 'Negative'}
                  </button>
                ))}
              </div>
            </div>
            <ul className="ins-list">
              {items.map((it) => (
                <li key={it.id}>
                  <Link className="ins-row" to={`/insights/${it.session_id}`}>
                    <Stars n={it.stars} />
                    <span className="ins-row-name">{it.client_name}</span>
                    <span className="ins-row-comment">{it.comment || '—'}</span>
                    <span className="ins-row-date">{fmtDate(it.created_at)}</span>
                    <span className="ins-row-chev" aria-hidden="true">›</span>
                  </Link>
                </li>
              ))}
              {items.length === 0 && <li className="ins-muted ins-list-empty">No reviews in this filter.</li>}
            </ul>
          </section>
        </div>
      </div>
    </Shell>
  )
}

export function SessionInsight() {
  const { sessionId } = useParams()
  const [d, setD] = useState<SessionData | null>(null)
  const [err, setErr] = useState(false)

  useEffect(() => {
    if (!sessionId) return
    fetch(`/api/insights/session/${encodeURIComponent(sessionId)}`)
      .then((r) => r.json())
      .then(setD)
      .catch(() => setErr(true))
  }, [sessionId])

  const back = (
    <Link className="ins-back" to="/insights">
      ‹ All rankings
    </Link>
  )

  if (err)
    return (
      <Shell title="Session" back={back}>
        <p className="ins-error">Couldn't load this session.</p>
      </Shell>
    )
  if (!d)
    return (
      <Shell title="Session" back={back}>
        <p className="ins-muted">Loading…</p>
      </Shell>
    )

  const r = d.rating
  const subtitle = (
    <span className="ins-headmeta">
      <span className={`ins-badge ins-badge--${d.channel}`}>{d.channel === 'call' ? 'Voice call' : 'Chat'}</span>
      {r?.booking_ref && <span className="ins-ref">{r.booking_ref}</span>}
      {r && <Stars n={r.stars} />}
      <span className="ins-muted">{fmtDate(r?.created_at ?? d.started_at)}</span>
    </span>
  )

  return (
    <Shell title={r?.client_name || 'Guest'} subtitle={subtitle} back={back}>
      {/* Session details — plain rows, no boxes. */}
      <div className="ins-meta">
        <div className="ins-meta-item">
          <span className="ins-meta-label">Channel</span>
          <span className="ins-meta-value">{d.channel === 'call' ? 'Voice call' : 'Chat'}</span>
        </div>
        <div className="ins-meta-item">
          <span className="ins-meta-label">Date</span>
          <span className="ins-meta-value">{fmtDay(d.started_at || r?.created_at || null)}</span>
        </div>
        <div className="ins-meta-item">
          <span className="ins-meta-label">Started</span>
          <span className="ins-meta-value">{fmtTime(d.started_at)}</span>
        </div>
        <div className="ins-meta-item">
          <span className="ins-meta-label">Ended</span>
          <span className="ins-meta-value">{fmtTime(d.ended_at)}</span>
        </div>
        <div className="ins-meta-item">
          <span className="ins-meta-label">Duration</span>
          <span className="ins-meta-value">{fmtDuration(d.duration_seconds)}</span>
        </div>
        <div className="ins-meta-item">
          <span className="ins-meta-label">Messages</span>
          <span className="ins-meta-value">{d.message_count}</span>
        </div>
        <div className="ins-meta-item ins-meta-item--id">
          <span className="ins-meta-label">Session ID</span>
          <span className="ins-meta-value ins-meta-mono">{d.session_id}</span>
        </div>
      </div>

      {/* AI assessment — the headline analysis, given the full width. */}
      <section className="ins-card ins-card--assess">
        <div className="ins-cardhead">
          <span className="ins-eyebrow">AI assessment</span>
          {d.runs_evaluated > 0 && <span className="ins-cardhead-note">{d.runs_evaluated} turns evaluated</span>}
        </div>
        {d.dimensions.length ? (
          <div className="ins-scores">
            {d.dimensions.map((dim) => (
              <div key={dim.key} className={`ins-score ins-score--${dim.verdict}`}>
                <div className="ins-score-top">
                  <span className="ins-score-label">{dim.label}</span>
                  <span className="ins-score-pct">
                    {Math.round(dim.score * 100)}
                    <span className="ins-score-sign">%</span>
                  </span>
                </div>
                <div className="ins-score-track">
                  <span className="ins-score-fill" style={{ width: `${Math.round(dim.score * 100)}%` }} />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="ins-muted">No AI evaluation recorded for this session yet.</p>
        )}
      </section>

      <div className="ins-grid2 ins-grid2--detail">
        <div className="ins-col">
          <section className="ins-card">
            <span className="ins-eyebrow">Customer feedback</span>
            {r ? (
              <div className="ins-cust">
                <div className="ins-cust-head">
                  <Stars n={r.stars} />
                  <span className="ins-cust-verdict">
                    {r.stars >= 4 ? 'Positive' : r.stars <= 2 ? 'Negative' : 'Neutral'}
                  </span>
                </div>
                {r.comment ? <p className="ins-quote">“{r.comment}”</p> : <p className="ins-muted">No written comment left.</p>}
              </div>
            ) : (
              <p className="ins-muted">No customer rating recorded for this session.</p>
            )}
          </section>

          <section className="ins-card ins-why">
            <span className="ins-eyebrow">Likely reason</span>
            <p className="ins-why-text">{d.why}</p>
          </section>
        </div>

        <div className="ins-col ins-col--side">
          <section className="ins-card ins-transcript-card">
            <span className="ins-eyebrow">Conversation{d.message_count ? ` · ${d.message_count} messages` : ''}</span>
            <div className="ins-transcript">
              {d.transcript.map((t, i) => (
                <div key={i} className={`ins-turn ins-turn--${t.speaker}`}>
                  <span className="ins-turn-who">
                    {t.speaker === 'customer' ? 'Customer' : t.speaker === 'agent' ? 'Aisha' : t.speaker}
                  </span>
                  <span className="ins-turn-text" dir="auto">{t.text}</span>
                </div>
              ))}
              {d.transcript.length === 0 && <p className="ins-muted">No transcript stored.</p>}
            </div>
          </section>
        </div>
      </div>
    </Shell>
  )
}
