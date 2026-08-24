// Dashboard — the operator's headline view (GET /api/console/dashboard).
//
// EVERY NUMBER IS A REAL QUERY (rule 16). No illustrative figure, no seeded
// shape, no "typical demo day" placeholder. A fresh database reports zeros and
// this page shows zeros — a dashboard that looks busy when nothing has happened
// is the one thing rule 16 names outright.
//
// A FAILED REQUEST IS NOT A ZERO. If the fetch fails nothing is rendered but the
// error; "0 enquiries" because the backend is down would be a count that nothing
// measured, which is the same lie from the other direction.
//
// IT READS ITS OWN ENDPOINT, NOT /metrics. That response shape is signed in the
// CLAUDE.md API contract and is left exactly as it was — see services/console.py
// for why this is a second function rather than a wider one.
//
// WHAT THE HANDOFF PANELS MEAN. Escalations ARE recorded now (rule 13 /
// FR-ES): request_human_callback logs an `escalation` audit row, and the
// dashboard counts them. So `count` is the enquiries handed to a human in the
// window, "resolved without a human" is the rest, and the Handoff card lists the
// reasons breakdown. With no escalation logged it is a true zero (0 handoffs,
// 100% resolved without a human, empty reasons) rather than a hardcoded one.
//
// THE WINDOW GOVERNS EVERYTHING on the page, and the payload echoes back the
// window it applied, so the page describes what was RETURNED rather than what
// was asked for.

import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Typography from '@mui/material/Typography'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Skeleton from '@mui/material/Skeleton'
import Divider from '@mui/material/Divider'


import {
  fetchDashboard,
  isAbort,
  type Dashboard,
  type DashboardWindow,
  type RankedPlace,
} from '../adminApi'
// No AutoGrid here, unlike every other tab: its auto-fill columns stretch cards
// to the container, and these carry fixed sizes from the design. No PageHeader
// either — the white top bar sets its own Archivo title inline.
import { ErrorState, TopBar } from '../components/ui'
import { BranchMap } from '../components/BranchMap'
import { escalationReasonLabel, placeLabel } from '../labels'

// Auto-refresh only — there is deliberately no Refresh button (decision
// 2026-07-23). Skipped while the tab is hidden: a console left open in a
// background tab overnight should not poll the backend thousands of times.
const REFRESH_MS = 30_000

const WINDOWS: { value: DashboardWindow; label: string; previous: string }[] = [
  { value: '7d', label: 'Last 7 days', previous: 'previous 7 days' },
  { value: '30d', label: 'Last 30 days', previous: 'previous 30 days' },
  { value: '90d', label: 'Last 90 days', previous: 'previous 90 days' },
  // All time has no preceding period, so nothing on the page compares against
  // one — the backend returns null and the delta line does not render.
  { value: 'all', label: 'All time', previous: '' },
]

const DEFAULT_WINDOW: DashboardWindow = '7d'

// The mockup's palette, named once so the accents are one decision rather than
// thirty scattered hex literals.
const AMBER = 'rgba(255, 179, 0, 1)'
const GREEN = 'rgba(18, 133, 90, 1)'
const INDIGO = 'rgba(99, 102, 241, 1)'
const BAR_TRACK = '#eceff4'
// Both leaderboards share one bar fill (developer, 23 Jul 2026). A GRADIENT, so
// the bars carry a `background` rather than a `bgcolor` — see Bar.
const RANK_BAR = 'linear-gradient(45deg, #F97316 25%, #EF4444 75%)'

/** The KPI card titles — Booking funnel, Handoff, Most booked vehicles/
 *  showrooms. One constant so the four stay identical; applied to all four
 *  rather than only three, because they are the same kind of heading and one
 *  odd one out would read as a mistake. */
const KPI_TITLE_FONT = {
  fontFamily: "'Archivo', system-ui, sans-serif",
  fontWeight: 700,
  fontSize: 16,
  lineHeight: '100%',
  letterSpacing: 0,
} as const

/** The headline number on each tile. Its own family and weight, and the ONLY
 *  place Archivo is used — loaded in index.html alongside the other three. */
const TILE_VALUE_FONT = {
  fontFamily: "'Archivo', system-ui, sans-serif",
  fontWeight: 800,
  fontSize: 36,
  lineHeight: '100%',
  letterSpacing: 0,
  fontVariantNumeric: 'tabular-nums',
} as const

// Card geometry, supplied by the developer 23 Jul 2026. Named here rather than
// inlined so the design values are one edit, and so the reason the tiles are not
// on the shared AutoGrid is visible at the point of use.
const CARD_BORDER = '1px solid rgba(226, 232, 240, 1)'
const CARD_SHADOW = '0px 4px 12px 0px rgba(0, 0, 0, 0.02)'
const CARD_RADIUS = 12
const TILE_W = 497
const TILE_H = 178
const MAP_CARD_W = 432
const MAP_CARD_H = 547
const FUNNEL_W = 1062
const FUNNEL_H = 174
const PANEL_W = 523
const PANEL_H = 228

/** Every card on this page: one border, one shadow, one radius.
 *
 *  The hover override matters — the console's MuiCard theme lifts the shadow on
 *  hover, which on a read-only dashboard makes cards twitch under a cursor that
 *  is only travelling across them. */
const cardSx = {
  border: CARD_BORDER,
  borderRadius: `${CARD_RADIUS}px`,
  boxShadow: CARD_SHADOW,
  '&:hover': { boxShadow: CARD_SHADOW },
} as const

/** The amber accent: TWO 2px lines just inside the left edge, full height (per
 *  Figma), drawn as one ::before with a striped gradient (line · gap · line).
 *  overflow:hidden clips them to the card's rounded corners so they meet the
 *  edges cleanly. Shared by the top tiles and the booking-funnel card; the
 *  content of each needs a little extra left padding to clear the 20px the rail
 *  occupies. Kept off cardSx because not every card wears it. */
const amberRailSx = {
  position: 'relative',
  overflow: 'hidden',
  '&::before': {
    content: '""',
    position: 'absolute',
    insetBlock: 0,
    insetInlineStart: 8,
    width: 6,
    background: `linear-gradient(to right, ${AMBER} 0 2px, transparent 2px 4px, ${AMBER} 4px 6px)`,
  },
} as const

function pct(value: number | null | undefined): string {
  return value == null ? '—' : `${Math.round(value)}%`
}

/** A horizontal bar. `fraction` is 0..1 — the caller decides what it is a
 *  fraction OF, because the tiles measure a share of the whole while the
 *  leaderboards measure against their own top row.
 *
 *  `fill` is a CSS BACKGROUND, not a colour: the leaderboards use a gradient,
 *  and typing this as a colour would have meant a second prop and two code
 *  paths for one visual idea. */
function Bar({
  fraction,
  fill,
  height = 9,
}: {
  fraction: number
  fill: string
  height?: number
}) {
  const width = Math.max(0, Math.min(1, fraction)) * 100
  return (
    <Box sx={{ height, borderRadius: 999, bgcolor: BAR_TRACK, overflow: 'hidden' }}>
      <Box sx={{ height: '100%', width: `${width}%`, background: fill, borderRadius: 999 }} />
    </Box>
  )
}

/** One of the three headline tiles: amber rail, icon, a big number, and whatever
 *  the tile has to say for itself underneath. */
function Tile({
  label,
  value,
  valueColor,
  icon,
  iconBg,
  children,
}: {
  label: string
  value: string
  valueColor?: string
  icon: ReactNode
  iconBg: string
  children?: ReactNode
}) {
  return (
    <Card
      sx={{
        // 497x197 as specified. The WIDTH IS A CEILING, NOT A FLOOR
        // (`flex: 1 1 0` + maxWidth): three rigid 497px cards plus their gaps
        // need ~1523px of content area, which a narrower window does not have
        // once the 248px sidebar and the page padding are removed — a hard width
        // would put a horizontal scrollbar under the dashboard on the machine it
        // is presented from. `flex-basis: 0` (not auto) is what keeps the three
        // EQUAL: auto sizes each from its own content, so a longer subline made
        // one wider than the next; from a zero basis they share the row evenly
        // and all cap at exactly 497 the moment there is room.
        ...cardSx,
        flex: '1 1 0',
        maxWidth: TILE_W,
        minWidth: 260,
        height: TILE_H,
        ...amberRailSx,
      }}
    >
      {/* Extra left padding so the content clears the amber double-line (which
          ends at 20px); without it the big value sat right against the lines. */}
      <CardContent sx={{ p: 2.5, pl: 3.5, pb: '20px !important' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 2 }}>
          <Box
            sx={{
              width: 46,
              height: 46,
              borderRadius: '50%',
              display: 'grid',
              placeItems: 'center',
              bgcolor: iconBg,
              flexShrink: 0,
            }}
          >
            {icon}
          </Box>
          <Typography
            sx={{
              fontSize: 12.5,
              fontWeight: 700,
              letterSpacing: '.06em',
              textTransform: 'uppercase',
              color: 'text.secondary',
            }}
          >
            {label}
          </Typography>
        </Box>
        <Typography
          sx={{
            ...TILE_VALUE_FONT,
            color: valueColor ?? 'text.primary',
            mb: 1.5,
          }}
        >
          {value}
        </Typography>
        {children}
      </CardContent>
    </Card>
  )
}

/** A ranked row: label + count on one line, a FULL-WIDTH bar beneath.
 *
 *  Two lines, not one. The single-line form squeezed the bar into a narrow
 *  middle column between a 40% label and the count, so even at full value it
 *  only spanned a third of the card. Stacking lets the bar run the whole width
 *  — which reads as a leaderboard and makes the top row's bar genuinely full.
 *
 *  Bars scale against the TOP ROW of their own panel, so the leader fills the
 *  track; a length is only comparable inside its own panel, which is why the
 *  exact count sits on the row and nothing depends on reading the bar. */
function RankedRow({
  label,
  count,
  max,
  fill,
}: {
  label: string
  count: number
  max: number
  fill: string
}) {
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.75 }}>
      <Box sx={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 2 }}>
        <Typography sx={{ fontSize: 13.5, minWidth: 0 }} noWrap title={label}>
          {label}
        </Typography>
        <Typography
          sx={{
            fontSize: 13.5,
            fontWeight: 600,
            fontVariantNumeric: 'tabular-nums',
            flexShrink: 0,
          }}
        >
          {count}
        </Typography>
      </Box>
      <Bar fraction={max > 0 ? count / max : 0} fill={fill} />
    </Box>
  )
}

/** A branch reads as "Al Quoz, Dubai"; a doorstep row reads as the area it
 *  covers. Composed HERE rather than stored, per the schema's no-display-text
 *  rule — the backend ships raw slugs and the location type. */
function placeRowLabel(p: RankedPlace): string {
  if (p.location_type === 'doorstep') {
    return p.name ? `Doorstep — ${p.name}` : 'Doorstep'
  }
  return placeLabel(p.city, p.emirate) ?? p.name ?? '—'
}

function FunnelStage({ label, value }: { label: string; value: number }) {
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography sx={{ fontSize: 13, color: 'text.secondary', mb: 0.75 }} noWrap>
        {label}
      </Typography>
      <Typography
        sx={{
          fontFamily: "'Space Grotesk', system-ui, sans-serif",
          fontSize: 21,
          fontWeight: 600,
          fontVariantNumeric: 'tabular-nums',
          lineHeight: 1,
        }}
      >
        {value.toLocaleString('en-US')}
      </Typography>
    </Box>
  )
}

export default function Overview() {
  // Resets to the default on every visit (decision 2026-07-23) — a demo stays
  // repeatable and nobody inherits a filter someone else left on.
  const [selected, setSelected] = useState<DashboardWindow>(DEFAULT_WINDOW)
  const [data, setData] = useState<Dashboard | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  // Bumped by the interval. Separate from the window so a background refresh
  // re-runs the fetch without pretending the window changed.
  const [tick, setTick] = useState(0)
  const abortRef = useRef<AbortController | null>(null)
  // Read inside the effect without making it a dependency: whether we already
  // have data decides only whether to show skeletons, and depending on it would
  // re-fire the fetch the moment the first response lands.
  const hasDataRef = useRef(false)
  hasDataRef.current = data != null

  useEffect(() => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    // Only the FIRST load of a window shows skeletons. A background refresh must
    // not blank the page every 30 seconds while someone is reading it.
    if (!hasDataRef.current) setLoading(true)
    fetchDashboard(selected, { signal: ctrl.signal }).then((res) => {
      if (ctrl.signal.aborted) return
      if (res.ok) {
        setData(res.data)
        setError(null)
      } else if (!isAbort(res)) {
        // Clear the figures too: leaving the previous window's numbers under an
        // error banner would show counts that no longer describe anything.
        setData(null)
        setError(res.error)
      }
      setLoading(false)
    })
    return () => ctrl.abort()
  }, [selected, tick])

  // Changing the window discards the old figures immediately, so a slow request
  // cannot leave last window's numbers on screen under the new label.
  useEffect(() => {
    setData(null)
  }, [selected])

  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') setTick((n) => n + 1)
    }, REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  const active = WINDOWS.find((w) => w.value === (data?.window ?? selected)) ?? WINDOWS[0]
  const maxPlace = Math.max(1, ...(data?.top_places ?? []).map((p) => p.bookings))
  const maxVehicle = Math.max(1, ...(data?.top_vehicles ?? []).map((v) => v.bookings))
  const change = data?.test_drives.change_pct ?? null

  return (
    <Box>
      {/* Shared white top bar (see components/ui TopBar) — the Bookings tab uses
          the same one, so the two stay identical. */}
      <TopBar title="Dashboard" subtitle="Live counters from real session activity." />

      <TextField
        select
        value={selected}
        onChange={(e) => setSelected(e.target.value as DashboardWindow)}
        sx={{ minWidth: 190, mb: 3, bgcolor: 'background.paper' }}
      >
        {WINDOWS.map((w) => (
          <MenuItem key={w.value} value={w.value}>
            {w.label}
          </MenuItem>
        ))}
      </TextField>

      {error && <ErrorState message={error} onRetry={() => setTick((n) => n + 1)} />}

      {loading && !data && (
        <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton
              key={i}
              variant="rounded"
              height={TILE_H}
              sx={{ flex: '1 1 0', maxWidth: TILE_W, minWidth: 260, borderRadius: `${CARD_RADIUS}px` }}
            />
          ))}
        </Box>
      )}

      {data && (
        <>
          {/* A flex row, not the shared AutoGrid: these three carry a fixed
              495x197 and AutoGrid's auto-fill columns would stretch them to
              whatever the container has. */}
          <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap', alignItems: 'stretch' }}>
            <Tile
              label="Test drives booked"
              value={data.test_drives.booked.toLocaleString('en-US')}
              valueColor={GREEN}
              iconBg="rgba(18, 133, 90, 0.12)"
              icon={
                <Box
                  component="img"
                  src="/TestDriveBooked.svg"
                  alt=""
                  sx={{ width: 30, height: 30, objectFit: 'contain' }}
                />
              }
            >
              {/* Only this tile carries a trend (decision 2026-07-23). Absent on
                  All time, which has no preceding period, and worded from the
                  window rather than hardcoded to "last month" — which would be
                  false for three of the four options. */}
              {active.previous &&
                (change == null ? (
                  <Typography sx={{ fontSize: 12.5, color: 'text.disabled' }}>
                    No activity in the {active.previous} to compare against
                  </Typography>
                ) : (
                  <Typography
                    sx={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: change >= 0 ? GREEN : 'error.main',
                    }}
                  >
                    {change >= 0 ? 'Up' : 'Down'} {Math.abs(change)}% on the {active.previous}
                  </Typography>
                ))}
            </Tile>

            <Tile
              label="Enquiries handled"
              value={data.enquiries.handled.toLocaleString('en-US')}
              iconBg="rgba(239, 35, 60, 0.12)"
              icon={
                <Box
                  component="img"
                  src="/EnqueriesHandled.svg"
                  alt=""
                  sx={{ width: 30, height: 30, objectFit: 'contain' }}
                />
              }
            >
              {/* The agent/human split now lives in its own Handoff card below
                  (escalations are recorded), so this tile carries conversion
                  instead — built from two numbers already on screen, and the
                  question a client actually asks. */}
              <Bar fraction={(data.enquiries.conversion_pct ?? 0) / 100} fill={AMBER} />
              <Typography sx={{ fontSize: 12.5, color: 'text.secondary', mt: 1.25 }}>
                {data.enquiries.conversion_pct == null
                  ? 'No enquiries in this period'
                  : `${data.enquiries.converted.toLocaleString('en-US')} of ${data.enquiries.handled.toLocaleString(
                      'en-US',
                    )} became a test drive · ${pct(data.enquiries.conversion_pct)}`}
              </Typography>
            </Tile>

            <Tile
              label="Resolved without a human"
              value={pct(data.handoffs.resolved_pct)}
              iconBg="rgba(99, 102, 241, 0.15)"
              icon={
                <Box
                  component="img"
                  src="/ResolvedWithoutHuman.svg"
                  alt=""
                  sx={{ width: 30, height: 30, objectFit: 'contain' }}
                />
              }
            >
              <Bar fraction={(data.handoffs.resolved_pct ?? 0) / 100} fill={AMBER} />
              <Typography sx={{ fontSize: 12.5, color: 'text.secondary', mt: 1.25 }}>
                {data.handoffs.resolved_pct == null
                  ? 'No enquiries in this period'
                  : `${data.handoffs.resolved_without_human.toLocaleString(
                      'en-US',
                    )} enquiries closed by the agent alone`}
              </Typography>
            </Tile>
          </Box>

          {/* Tighter gap under the divider — mt keeps it clear of the tiles
              above, mb is small so "KPIs" sits close to the line. */}
          <Divider sx={{ mt: 4, mb: 1.5 }} />
          <Typography variant="h5" sx={{ fontSize: 22, mb: 2.5 }}>
            KPIs
          </Typography>

          <Box
            sx={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 2,
              alignItems: 'flex-start',
            }}
          >
            {/* Map + branch leaderboard. The two can legitimately disagree: a
                doorstep row ranks in the list but has no branch to pin.
                432x547 with a 16px padding and a 16px gap, per spec. */}
            <Card
              sx={{
                ...cardSx,
                flex: '1 1 auto',
                maxWidth: MAP_CARD_W,
                minWidth: 300,
                height: MAP_CARD_H,
                p: '16px',
                display: 'flex',
                flexDirection: 'column',
                gap: '16px',
              }}
            >
              <BranchMap pins={data.map_pins} />
              {/* The list takes whatever the map leaves. It scrolls rather than
                  bursting the fixed height if the leaderboard ever runs long —
                  a card that quietly grows past its spec is the harder bug to
                  see than one that scrolls. */}
              <Box sx={{ minHeight: 0, overflowY: 'auto' }}>
                <Typography variant="h6" sx={{ ...KPI_TITLE_FONT, mb: 2 }}>
                  Most booked showrooms
                </Typography>
                {data.top_places.length === 0 ? (
                  <Typography sx={{ fontSize: 13, color: 'text.disabled' }}>
                    No bookings in this period.
                  </Typography>
                ) : (
                  <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.75 }}>
                    {data.top_places.map((p) => (
                      <RankedRow
                        key={`${p.location_type}-${p.showroom_id ?? p.name}`}
                        label={placeRowLabel(p)}
                        count={p.bookings}
                        max={maxPlace}
                        fill={RANK_BAR}
                      />
                    ))}
                  </Box>
                )}
              </Box>
            </Card>

            <Box
              sx={{
                display: 'flex',
                flexDirection: 'column',
                gap: 2,
                flex: '1 1 auto',
                maxWidth: FUNNEL_W,
                minWidth: 300,
              }}
            >
              <Card
                sx={{
                  ...cardSx,
                  ...amberRailSx,
                  height: FUNNEL_H,
                }}
              >
                {/* Extra left padding so the stages clear the amber double-line
                    (ends at 20px), matching the top tiles. */}
                <CardContent sx={{ p: 2.5, pl: 3.5 }}>
                  <Typography variant="h6" sx={{ ...KPI_TITLE_FONT, mb: 2.5 }}>
                    Booking funnel
                  </Typography>
                  <Box
                    sx={{
                      display: 'grid',
                      // 5.5rem, not 6.5: the six stages are one reading — six
                      // numbers left to right — and at the wider minimum
                      // "No show" wrapped to a second row on a laptop, which
                      // reads as a separate fact rather than the end of the
                      // funnel. They still wrap on a narrow window, where a
                      // single row would be unreadable anyway.
                      gridTemplateColumns: 'repeat(auto-fit, minmax(5.5rem, 1fr))',
                      gap: 2,
                    }}
                  >
                    <FunnelStage label="Enquiries" value={data.funnel.enquiries} />
                    <FunnelStage label="Booked" value={data.funnel.booked} />
                    <FunnelStage label="Completed" value={data.funnel.completed} />
                    <FunnelStage label="Rescheduled" value={data.funnel.rescheduled} />
                    <FunnelStage label="Cancelled" value={data.funnel.cancelled} />
                    <FunnelStage label="No show" value={data.funnel.noshow} />
                  </Box>
                  {/* Of drives with an OUTCOME, so an unmarked booking does not
                      drag the rate around. Absent until at least one drive has
                      been marked attended or no-show. */}
                  {data.funnel.noshow_rate_pct != null && (
                    <Typography sx={{ fontSize: 13, fontWeight: 600, color: GREEN, mt: 2.5 }}>
                      No-show rate {pct(data.funnel.noshow_rate_pct)}
                      {data.funnel.noshow_rate_previous_pct != null &&
                        ` — ${
                          data.funnel.noshow_rate_pct <= data.funnel.noshow_rate_previous_pct
                            ? 'down'
                            : 'up'
                        } from ${pct(data.funnel.noshow_rate_previous_pct)}`}
                    </Typography>
                  )}
                </CardContent>
              </Card>

              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 2, alignItems: 'flex-start' }}>
                {/* 523x228, 28px padding, and the card itself distributes its
                    own rows — which is what `justify-content: space-between`
                    buys here: the footnote sits on the floor of the card rather
                    than floating under a short list. */}
                <Card
                  sx={{
                    ...cardSx,
                    flex: '1 1 auto',
                    maxWidth: PANEL_W,
                    minWidth: 280,
                    height: PANEL_H,
                    p: '28px',
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: 'space-between',
                  }}
                >
                  <Box sx={{ minHeight: 0, overflowY: 'auto' }}>
                    <Box
                      sx={{
                        display: 'flex',
                        alignItems: 'baseline',
                        justifyContent: 'space-between',
                        gap: 1,
                        mb: 2.5,
                      }}
                    >
                      <Typography variant="h6" sx={{ ...KPI_TITLE_FONT }}>
                        Handoff to a specialist
                      </Typography>
                      {/* The headline is the COUNT (how many enquiries were
                          handed to a human), not a percentage: at demo volumes a
                          count reads truer than "100%" of one handoff. The pct
                          sits under it as context. */}
                      <Typography
                        sx={{
                          fontFamily: "'Space Grotesk', system-ui, sans-serif",
                          fontSize: 21,
                          fontWeight: 700,
                          color: INDIGO,
                        }}
                      >
                        {data.handoffs.count.toLocaleString('en-US')}
                      </Typography>
                    </Box>
                    <Typography sx={{ fontSize: 12.5, color: 'text.secondary', mb: 2.5, mt: -1.5 }}>
                      {data.handoffs.count === 0
                        ? 'No enquiries handed to a human'
                        : `${pct(data.handoffs.pct)} of enquiries · ${data.handoffs.resolved_without_human.toLocaleString(
                            'en-US',
                          )} resolved without a human`}
                    </Typography>
                    {/* A true zero (nothing escalated) rather than a hidden gap.
                        Reasons are counts of distinct conversations per reason. */}
                    {data.handoffs.reasons.length === 0 ? (
                      <Typography sx={{ fontSize: 13, color: 'text.disabled' }}>
                        No handoffs in this period.
                      </Typography>
                    ) : (
                      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.75 }}>
                        {data.handoffs.reasons.map((r) => (
                          <RankedRow
                            key={r.reason}
                            label={escalationReasonLabel(r.reason) ?? r.reason}
                            count={r.count}
                            max={Math.max(1, ...data.handoffs.reasons.map((x) => x.count))}
                            fill={INDIGO}
                          />
                        ))}
                      </Box>
                    )}
                  </Box>
                  <Typography sx={{ fontSize: 12, color: 'text.disabled', pt: 2 }}>
                    * Every handoff carried full conversational context
                  </Typography>
                </Card>

                <Card
                  sx={{
                    ...cardSx,
                    flex: '1 1 auto',
                    maxWidth: PANEL_W,
                    minWidth: 280,
                    height: PANEL_H,
                    p: '28px',
                    display: 'flex',
                    flexDirection: 'column',
                  }}
                >
                  <Box sx={{ minHeight: 0, overflowY: 'auto' }}>
                    <Typography variant="h6" sx={{ ...KPI_TITLE_FONT, mb: 2.5 }}>
                      Most booked vehicles
                    </Typography>
                    {data.top_vehicles.length === 0 ? (
                      <Typography sx={{ fontSize: 13, color: 'text.disabled' }}>
                        No bookings in this period.
                      </Typography>
                    ) : (
                      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.75 }}>
                        {data.top_vehicles.map((v) => (
                          <RankedRow
                            key={v.vehicle_id}
                            label={v.name}
                            count={v.bookings}
                            max={maxVehicle}
                            fill={RANK_BAR}
                          />
                        ))}
                      </Box>
                    )}
                  </Box>
                </Card>
              </Box>
            </Box>
          </Box>
        </>
      )}
    </Box>
  )
}
