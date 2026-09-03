// Bookings — every test drive, active and cancelled.
//
// THE CARD IS AN EXACT REPRODUCTION of the Figma (developer, 23 Jul 2026):
// amber double-rail, reference + status pill, booked date, customer, then
// Vehicle / When / Location rows, and a WhatsApp confirmation footer. The richer
// earlier card (map pin, phone chip, companions/child-seat/consultant chips,
// driver row, address blocks) was dropped to match it.
//
// TWO BEHAVIOURS WORTH NAMING, because the Figma shows only appearance:
//   • The green pill shows the booking STATUS ("Confirmed"), and its chevron
//     opens the ATTENDANCE menu — the console's only write (2026-07-23). Rather
//     than delete that action to match a static mock, it lives behind the pill;
//     the confirm-before-no-show safeguard is kept (invisible until triggered).
//     A cancelled booking shows the red status pill with NO chevron: attendance
//     does not apply to a drive that was called off, and the service refuses it.
//   • "WhatsApp confirmation sent" shows for confirmed/rescheduled bookings —
//     every booking triggers a confirmation notification in this system. It is
//     omitted on a cancelled booking. It reflects status, not a per-message
//     delivery flag (that field is not on the booking row).
//
// ⚠️ PII, UNGATED (decision 2026-07-21). Customer name and +971 mobile are on
// screen for every booking. The customer-facing GET /api/booking/{ref} gates
// exactly these fields; this tab does not — it is on the D1 PDPL checklist, and
// the fix is one dependency on the console router.

import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import Divider from '@mui/material/Divider'
import MenuItem from '@mui/material/MenuItem'
import Select from '@mui/material/Select'
import CircularProgress from '@mui/material/CircularProgress'
import Menu from '@mui/material/Menu'
import Avatar from '@mui/material/Avatar'
import Button from '@mui/material/Button'
import Tooltip from '@mui/material/Tooltip'

import DirectionsCarRoundedIcon from '@mui/icons-material/DirectionsCarRounded'
import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import GridViewRoundedIcon from '@mui/icons-material/GridViewRounded'
import FormatListBulletedRoundedIcon from '@mui/icons-material/FormatListBulletedRounded'
import ScheduleRoundedIcon from '@mui/icons-material/ScheduleRounded'
import StorefrontRoundedIcon from '@mui/icons-material/StorefrontRounded'
import HomeWorkRoundedIcon from '@mui/icons-material/HomeWorkRounded'
import WhatsAppIcon from '@mui/icons-material/WhatsApp'
import KeyboardArrowDownRoundedIcon from '@mui/icons-material/KeyboardArrowDownRounded'
import StarRoundedIcon from '@mui/icons-material/StarRounded'
import FormatQuoteRoundedIcon from '@mui/icons-material/FormatQuoteRounded'

import {
  fetchBookings,
  fetchShowrooms,
  fetchVehicles,
  setBookingAttendance,
  type Attendance,
  type BookingRow,
  type FeedbackSummary,
  type ShowroomRow,
  type VehicleRow,
} from '../adminApi'
import { useList } from '../useList'
import { CardSkeletons, EmptyState, ErrorState, TopBar } from '../components/ui'
import { fmtDate, fmtRange } from '../format'
import {
  FEEDBACK_QUESTIONS,
  feedbackScoreLabel,
  feedbackStatusLabel,
  placeLabel,
  statusLabel,
} from '../labels'

// Card chrome, matching the dashboard cards + the amber double-rail.
const AMBER = 'rgba(255, 179, 0, 1)'
const CARD_BORDER = '1px solid rgba(226, 232, 240, 1)'
const CARD_SHADOW = '0px 4px 12px 0px rgba(0, 0, 0, 0.02)'
const LABEL_GREY = 'rgba(92, 100, 107, 1)'
const VALUE_DARK = 'rgba(30, 41, 59, 1)'
const GREEN = 'rgba(18, 133, 90, 1)'

/** Two 2px amber lines just inside the left edge, full height — the shared
 *  accent from the dashboard, inlined here (a small sx, not worth a cross-file
 *  import). overflow:hidden clips them to the rounded corners. */
const railSx = {
  position: 'relative',
  overflow: 'hidden',
  border: CARD_BORDER,
  borderRadius: '12px',
  boxShadow: CARD_SHADOW,
  bgcolor: '#fff',
  '&::before': {
    content: '""',
    position: 'absolute',
    insetBlock: 0,
    insetInlineStart: 8,
    width: 6,
    background: `linear-gradient(to right, ${AMBER} 0 2px, transparent 2px 4px, ${AMBER} 4px 6px)`,
  },
} as const

/** Status → pill tint. Its own map, not theme.statusColor, because this needs a
 *  soft background + solid text pair (the green "Confirmed" pill), which a
 *  single MUI colour name does not express.
 *
 *  `rescheduled` IS GREEN, the same green as `confirmed` — see bookingStatus-
 *  Label below for why the two read as one state here. */
const STATUS_TINT: Record<string, { bg: string; fg: string }> = {
  confirmed: { bg: 'rgba(18, 133, 90, 0.12)', fg: GREEN },
  sent: { bg: 'rgba(18, 133, 90, 0.12)', fg: GREEN },
  rescheduled: { bg: 'rgba(18, 133, 90, 0.12)', fg: GREEN },
  cancelled: { bg: 'rgba(192, 57, 47, 0.12)', fg: 'rgba(192, 57, 47, 1)' },
}

/** The status as an OPERATOR reads it: `rescheduled` shows as "Confirmed"
 *  (product decision 2026-07-28).
 *
 *  DISPLAY ONLY — the stored status stays `rescheduled` and MUST. It is
 *  load-bearing in the backend: the partial unique index on slot_id covers
 *  WHERE status IN ('confirmed','rescheduled'), reschedule and cancel both read
 *  it, and the dashboard funnel counts reschedules as their own stage. Nothing
 *  here writes; this maps one word to another at the moment of drawing.
 *
 *  WHY: `status` answers "is this booking alive?", and a moved booking is just
 *  as alive as one never moved. Showing two different words for one state made
 *  an operator scanning the grid look for a difference that has no consequence
 *  for them, and the blue pill beside the green ones read as a warning.
 *
 *  LOCAL, not a change to labels.statusLabel, which is shared with the other
 *  tabs and with ui.tsx's StatusChip. Slots, notifications and conversations
 *  all have their own `rescheduled`-ish vocabulary, and collapsing the word
 *  everywhere would rewrite states this decision says nothing about. */
function bookingStatusLabel(status: string | null | undefined): string | null {
  return status === 'rescheduled' ? 'Confirmed' : statusLabel(status)
}

const ATTENDANCE_VALUES: Attendance[] = ['pending', 'completed', 'noshow']
const ATTENDANCE_LABEL: Record<Attendance, string> = {
  pending: 'Not recorded',
  completed: 'Attended',
  noshow: 'No-show',
}

interface Option {
  value: string
  label: string
}

/** A rounded pill Select showing "Label: Value" (or just the label when unset),
 *  matching the Figma filter row. Built on Select rather than TextField so
 *  renderValue can compose the "Label: Value" text; the outline is rounded to
 *  999 to read as a pill. */
function FilterPill({
  label,
  value,
  onChange,
  options,
  allLabel,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: Option[]
  /** Shown when nothing is selected, e.g. "Location: All" or just "Vehicle". */
  allLabel: string
}) {
  return (
    <Select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      displayEmpty
      size="small"
      IconComponent={KeyboardArrowDownRoundedIcon}
      renderValue={(v) => {
        const sel = options.find((o) => o.value === v)
        return v && sel ? `${label}: ${sel.label}` : allLabel
      }}
      sx={{
        bgcolor: '#fff',
        borderRadius: '10px',
        fontSize: 14,
        '& .MuiOutlinedInput-notchedOutline': {
          borderRadius: '10px',
          borderColor: 'rgba(226, 232, 240, 1)',
        },
        '& .MuiSelect-select': { py: 1, pl: 2, pr: '2.2rem !important' },
      }}
    >
      <MenuItem value="">{allLabel}</MenuItem>
      {options.map((o) => (
        <MenuItem key={o.value} value={o.value} sx={{ fontSize: 13.5 }}>
          {o.label}
        </MenuItem>
      ))}
    </Select>
  )
}

// Date presets → a slot-start range. Computed against the browser clock; on an
// operator machine that is UAE time, which is the frame these dates are in.
// Naive ISO (no 'Z') so it string-compares cleanly against the naive, Dubai
// wall-clock Slot.start the backend stores.
const DATE_OPTIONS: Option[] = [
  { value: 'today', label: 'Today' },
  { value: 'upcoming', label: 'Upcoming' },
  { value: 'past', label: 'Past' },
]

function dateRange(key: string): { date_from?: string; date_to?: string } {
  const pad = (n: number) => String(n).padStart(2, '0')
  const fmt = (d: Date) =>
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(
      d.getMinutes(),
    )}:${pad(d.getSeconds())}`
  const now = new Date()
  if (key === 'today') {
    const s = new Date(now)
    s.setHours(0, 0, 0, 0)
    const e = new Date(now)
    e.setHours(23, 59, 59, 0)
    return { date_from: fmt(s), date_to: fmt(e) }
  }
  if (key === 'upcoming') return { date_from: fmt(now) }
  if (key === 'past') return { date_to: fmt(now) }
  return {}
}

function initials(name: string | null): string {
  if (!name) return '?'
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '')
    .join('')
}

/** The status pill (top-right). Shows the booking STATUS; the chevron opens the
 *  attendance menu — the console's only write. Rendered without a chevron, and
 *  with no menu, on a cancelled booking (attendance does not apply). */
function StatusPill({ b }: { b: BookingRow }) {
  const [attendance, setAttendance] = useState<Attendance>(
    (b.attendance as Attendance) ?? 'pending',
  )
  const [notifiedAt, setNotifiedAt] = useState<string | null>(b.noshow_notified_at)
  const [busy, setBusy] = useState(false)
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const tint = STATUS_TINT[b.status ?? ''] ?? { bg: 'rgba(0,0,0,0.06)', fg: 'rgba(0,0,0,0.6)' }
  const editable = b.status !== 'cancelled'

  async function apply(next: Attendance) {
    setAnchor(null)
    if (next === attendance || busy) return
    if (
      next === 'noshow' &&
      !notifiedAt &&
      !window.confirm(
        `Mark ${b.ref} as a no-show?\n\n` +
          `${b.customer_name_en ?? 'The customer'} will be sent a WhatsApp ` +
          `asking if everything is alright and offering another time.`,
      )
    ) {
      return
    }
    setBusy(true)
    const res = await setBookingAttendance(b.ref, next)
    setBusy(false)
    if (!res.ok) return
    setAttendance(next)
    if (res.data.notified) setNotifiedAt(new Date().toISOString())
  }

  return (
    <>
      <Box
        component={editable ? 'button' : 'div'}
        onClick={editable && !busy ? (e: React.MouseEvent<HTMLElement>) => setAnchor(e.currentTarget) : undefined}
        sx={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 0.5,
          px: 1.5,
          py: 0.75,
          border: 'none',
          borderRadius: 999,
          bgcolor: tint.bg,
          color: tint.fg,
          fontFamily: "'Archivo', system-ui, sans-serif",
          fontWeight: 600,
          fontSize: 14,
          lineHeight: 1,
          cursor: editable ? 'pointer' : 'default',
          flexShrink: 0,
        }}
        title={editable ? `Attendance: ${ATTENDANCE_LABEL[attendance]}` : undefined}
      >
        {bookingStatusLabel(b.status)}
        {editable &&
          (busy ? (
            <CircularProgress size={13} sx={{ color: tint.fg }} />
          ) : (
            <KeyboardArrowDownRoundedIcon sx={{ fontSize: 18 }} />
          ))}
      </Box>
      {editable && (
        <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
          {ATTENDANCE_VALUES.map((v) => (
            <MenuItem
              key={v}
              selected={v === attendance}
              onClick={() => apply(v)}
              sx={{ fontSize: 13.5 }}
            >
              {ATTENDANCE_LABEL[v]}
            </MenuItem>
          ))}
        </Menu>
      )}
    </>
  )
}

/** A labelled detail row: small grey label, then icon + bold value. `dense`
 *  shrinks it for the list view, where the rows pack tighter than the cards. */
function DetailRow({
  label,
  icon,
  value,
  dense,
}: {
  label: string
  icon: ReactNode
  value: ReactNode
  dense?: boolean
}) {
  return (
    <Box>
      <Typography sx={{ fontSize: dense ? 11.5 : 13, color: LABEL_GREY, mb: 0.5 }}>{label}</Typography>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
        <Box sx={{ display: 'inline-flex', color: LABEL_GREY }}>{icon}</Box>
        <Typography sx={{ fontSize: dense ? 13 : 15, fontWeight: 600, color: VALUE_DARK }}>
          {value}
        </Typography>
      </Box>
    </Box>
  )
}

// --------------------------------------------------------------------------- //
// Feedback survey (2026-07-26)
// --------------------------------------------------------------------------- //
// A badge on the collapsed row so an operator can SCAN which completed drives
// have feedback, and an expandable panel with the four scores and the comment.
//
// TWO RULES THE RENDERING FOLLOWS, both of which matter more than they look:
//   • An unanswered question is an EM-DASH, never a zero and never an empty bar.
//     A zero is a score nobody gave; showing one would misreport a partial survey
//     as a bad one, and would be the fake dashboard data rule 16 forbids.
//   • The badge appears only when at least one question is ANSWERED. A survey
//     that was sent and ignored gets its status in the panel, not a badge with
//     nothing behind it.
const SCORE_TINT: Record<number, string> = {
  5: 'rgba(18, 133, 90, 1)',
  4: 'rgba(18, 133, 90, 0.75)',
  3: 'rgba(255, 179, 0, 1)',
  2: 'rgba(232, 124, 30, 1)',
  1: 'rgba(200, 55, 55, 1)',
}

/** The average-score badge for a collapsed row. Null when nothing is answered. */
function FeedbackBadge({ fb, dense }: { fb: FeedbackSummary; dense?: boolean }) {
  if (fb.average == null) return null
  // Tinted by the ROUNDED average, so a 2.4 reads as a warning colour rather
  // than being rounded up into reassurance.
  const tint = SCORE_TINT[Math.round(fb.average)] ?? LABEL_GREY
  return (
    <Box
      sx={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 0.4,
        px: 0.9,
        py: 0.2,
        borderRadius: 999,
        bgcolor: `${tint}14`,
        color: tint,
        flexShrink: 0,
      }}
      title={
        fb.answered < FEEDBACK_QUESTIONS.length
          ? `${fb.answered} of ${FEEDBACK_QUESTIONS.length} answered`
          : 'All four answered'
      }
    >
      <StarRoundedIcon sx={{ fontSize: dense ? 13 : 15 }} />
      <Typography sx={{ fontSize: dense ? 11.5 : 12.5, fontWeight: 700 }}>
        {fb.average.toFixed(1)}
      </Typography>
      {/* Partial surveys say so, so "3.0" from one answer is never mistaken for
          a settled verdict across four. */}
      {fb.answered < FEEDBACK_QUESTIONS.length && (
        <Typography sx={{ fontSize: dense ? 10.5 : 11, opacity: 0.8 }}>
          ({fb.answered}/{FEEDBACK_QUESTIONS.length})
        </Typography>
      )}
    </Box>
  )
}

/** One question: label, a 5-segment meter, and the score in words. */
function ScoreBar({ label, value }: { label: string; value: number | null }) {
  const tint = value == null ? LABEL_GREY : (SCORE_TINT[value] ?? LABEL_GREY)
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
      <Typography sx={{ fontSize: 12.5, color: LABEL_GREY, width: 118, flexShrink: 0 }}>
        {label}
      </Typography>
      <Box sx={{ display: 'flex', gap: 0.4, flexShrink: 0 }} aria-hidden>
        {[1, 2, 3, 4, 5].map((n) => (
          <Box
            key={n}
            sx={{
              width: 16,
              height: 6,
              borderRadius: 999,
              // An unanswered question shows five EMPTY segments, which reads as
              // "no answer". Filling them low would read as a bad answer.
              bgcolor: value != null && n <= value ? tint : 'rgba(226, 232, 240, 1)',
            }}
          />
        ))}
      </Box>
      <Typography sx={{ fontSize: 12.5, fontWeight: 600, color: value == null ? LABEL_GREY : VALUE_DARK }}>
        {value == null ? '—' : `${value} · ${feedbackScoreLabel(value)}`}
      </Typography>
    </Box>
  )
}

/** The expanded panel: four scores, the open comment, and the survey's state. */
function FeedbackPanel({ fb }: { fb: FeedbackSummary }) {
  return (
    <Box sx={{ mt: 2, pt: 2, borderTop: CARD_BORDER }}>
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {FEEDBACK_QUESTIONS.map((q) => (
          <ScoreBar key={q.key} label={q.label} value={fb.scores[q.key]} />
        ))}
      </Box>

      {/* The open close. Rendered with dir="auto" so an Arabic comment lays out
          right-to-left without the surrounding chrome flipping (LOC-01). */}
      {fb.comment ? (
        <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
          <FormatQuoteRoundedIcon sx={{ fontSize: 16, color: LABEL_GREY, flexShrink: 0, mt: 0.2 }} />
          <Typography
            dir="auto"
            sx={{ fontSize: 13.5, color: VALUE_DARK, fontStyle: 'italic', lineHeight: 1.5 }}
          >
            {fb.comment}
          </Typography>
        </Box>
      ) : (
        // "" and null are DIFFERENT facts: they were asked and had nothing to
        // add, versus the survey never got that far.
        <Typography sx={{ fontSize: 12.5, color: LABEL_GREY, mt: 2, fontStyle: 'italic' }}>
          {fb.comment === '' ? 'No comment added' : 'Comment not reached'}
        </Typography>
      )}

      <Typography sx={{ fontSize: 11.5, color: LABEL_GREY, mt: 1.5 }}>
        {feedbackStatusLabel(fb.status) ?? fb.status}
        {' · '}
        {fb.closed_at
          ? fmtDate(fb.closed_at)
          : fb.sent_at
            ? `asked ${fmtDate(fb.sent_at)}`
            : 'not sent yet'}
      </Typography>
    </Box>
  )
}

/** The collapsed-row control. Three states, and the third is why this is not
 *  simply `fb && <button>`: a completed drive whose survey was ERASED (rule 12)
 *  must not look like a drive we never surveyed, so the stamp on the booking is
 *  reported in words when the survey row itself is gone. */
function FeedbackToggle({
  b,
  open,
  onToggle,
  dense,
}: {
  b: BookingRow
  open: boolean
  onToggle: () => void
  dense?: boolean
}) {
  if (!b.feedback) {
    if (!b.feedback_requested_at) return null
    return (
      <Typography sx={{ fontSize: dense ? 11.5 : 12.5, color: LABEL_GREY, whiteSpace: 'nowrap' }}>
        Feedback erased
      </Typography>
    )
  }
  const fb = b.feedback
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexShrink: 0 }}>
      <FeedbackBadge fb={fb} dense={dense} />
      <Button
        onClick={onToggle}
        size="small"
        endIcon={
          <KeyboardArrowDownRoundedIcon
            sx={{
              fontSize: 16,
              transform: open ? 'rotate(180deg)' : 'none',
              transition: 'transform 120ms',
            }}
          />
        }
        sx={{
          textTransform: 'none',
          fontSize: dense ? 11.5 : 12.5,
          color: LABEL_GREY,
          minWidth: 0,
          px: 0.75,
        }}
      >
        {/* When nothing is answered there is no badge, so the button carries the
            state instead of saying "Feedback" over an empty panel. */}
        {fb.average == null ? (feedbackStatusLabel(fb.status) ?? 'Feedback') : 'Feedback'}
      </Button>
    </Box>
  )
}

/** The location line: "Branch, City, Emirate" for a showroom, or the doorstep
 *  service area / address. Composed here from the raw slugs (no display text in
 *  data fields). */
function locationText(b: BookingRow): string {
  if (b.showroom_name) {
    const place = placeLabel(b.showroom_city, b.showroom_emirate)
    return place ? `${b.showroom_name}, ${place}` : b.showroom_name
  }
  return b.service_area ?? b.doorstep_address ?? 'Doorstep'
}

/** The card's chat bubble: an oval with a bottom-left tail and a plus, drawn
 *  inline to match the design exactly — no MUI icon has this shape. */
function ChatPlusIcon() {
  return (
    <svg
      width={28}
      height={26}
      viewBox="0 0 24 24"
      preserveAspectRatio="none"
      fill="none"
      aria-hidden="true"
    >
      <ellipse cx="12.8" cy="10.2" rx="9.2" ry="7.7" fill="#4a90e8" />
      <path
        d="M7 15.2 C6.7 17.3 5.5 19 3.8 20.2 C6.4 20.1 8.6 19 10 17.3 Z"
        fill="#4a90e8"
      />
      <path
        d="M12.8 7.7 V12.7 M10.3 10.2 H15.3"
        stroke="#fff"
        strokeWidth="1.9"
        strokeLinecap="round"
      />
    </svg>
  )
}

/** The visit-personalisation notes (rule 8) behind the card's chat icon: who is
 *  coming, and anything arranged for the visit. Companions are a free string
 *  captured in conversation ("wife and two kids"); absent means the customer
 *  said nothing about company, which the demo reads as visiting alone. */
function visitLines(b: BookingRow): string[] {
  return [
    b.companions ? `Will visit with ${b.companions}` : 'Will visit alone',
    ...(b.child_seat ? ['Child seat arranged'] : []),
    ...(b.consultant_pref ? [`Consultant preference: ${b.consultant_pref}`] : []),
    ...(b.driver_name
      ? [
          `Driver: ${b.driver_name}${
            b.driver_relationship ? ` (${b.driver_relationship})` : ''
          }`,
        ]
      : []),
  ]
}

function BookingCard({ b }: { b: BookingRow }) {
  const doorstep = !b.showroom_name
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  return (
    // Fixed 365px wide (developer, 23 Jul 2026); maxWidth:100% so it still fits
    // a viewport narrower than one card. The list is a plain flex-wrap rather
    // than AutoGrid, whose stretching columns would override this width.
    <Box sx={{ ...railSx, width: 365, maxWidth: '100%', p: 2.5, pl: 3 }}>
      {/* Reference + status */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 1 }}>
        <Box sx={{ minWidth: 0 }}>
          <Typography
            sx={{
              fontFamily: "'Archivo', system-ui, sans-serif",
              fontWeight: 700,
              fontSize: 20,
              color: VALUE_DARK,
              lineHeight: 1.2,
            }}
          >
            {b.ref}
          </Typography>
          <Typography sx={{ fontSize: 13, color: LABEL_GREY, mt: 0.5 }}>
            Booked {fmtDate(b.created_at)}
          </Typography>
        </Box>
        <StatusPill b={b} />
      </Box>

      <Divider sx={{ my: 2 }} />

      {/* Customer */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
        <Avatar sx={{ width: 44, height: 44, bgcolor: AMBER, color: VALUE_DARK, fontSize: 15, fontWeight: 700 }}>
          {initials(b.customer_name_en)}
        </Avatar>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography sx={{ fontSize: 15, fontWeight: 600, color: VALUE_DARK }}>
            {b.customer_name_en ?? '—'}
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
            {b.customer_mobile && (
              <Typography sx={{ fontSize: 13.5, color: LABEL_GREY }}>{b.customer_mobile}</Typography>
            )}
            {b.customer_name_ar && (
              <Typography component="span" dir="rtl" sx={{ fontSize: 13.5, color: LABEL_GREY }}>
                <bdi>{b.customer_name_ar}</bdi>
              </Typography>
            )}
          </Box>
        </Box>
        <Tooltip
          arrow
          placement="top"
          title={
            <Box sx={{ py: 0.25 }}>
              {visitLines(b).map((line) => (
                <Typography key={line} sx={{ fontSize: 12.5, lineHeight: 1.6 }}>
                  {line}
                </Typography>
              ))}
            </Box>
          }
        >
          <Box sx={{ display: 'flex', flexShrink: 0, cursor: 'default' }}>
            <ChatPlusIcon />
          </Box>
        </Tooltip>
      </Box>

      <Divider sx={{ my: 2 }} />

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <DetailRow
          label="Vehicle"
          icon={<DirectionsCarRoundedIcon sx={{ fontSize: 18 }} />}
          value={[b.vehicle_name, b.variant_name].filter(Boolean).join(' ') || '—'}
        />
        <DetailRow
          label="When"
          icon={<ScheduleRoundedIcon sx={{ fontSize: 18 }} />}
          value={fmtRange(b.start, b.end) ?? '—'}
        />
        <DetailRow
          label="Location"
          icon={
            doorstep ? (
              <HomeWorkRoundedIcon sx={{ fontSize: 18 }} />
            ) : (
              <StorefrontRoundedIcon sx={{ fontSize: 18 }} />
            )
          }
          value={locationText(b)}
        />
      </Box>

      {/* WhatsApp confirmation — shown for a live booking (a confirmation
          notification is generated when the booking is made); omitted on a
          cancelled one. */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 1,
          mt: 2.5,
          flexWrap: 'wrap',
        }}
      >
        {b.status !== 'cancelled' ? (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <WhatsAppIcon sx={{ fontSize: 18, color: GREEN }} />
            <Typography sx={{ fontSize: 14, color: GREEN }}>WhatsApp confirmation sent</Typography>
          </Box>
        ) : (
          <Box />
        )}
        <FeedbackToggle
          b={b}
          open={feedbackOpen}
          onToggle={() => setFeedbackOpen((v) => !v)}
        />
      </Box>

      {feedbackOpen && b.feedback && <FeedbackPanel fb={b.feedback} />}
    </Box>
  )
}

/** TWO OPTIONS, NOT THREE (2026-07-28). "Rescheduled" is gone from the filter
 *  for the same reason it is gone from the pill: the console shows one live
 *  state. `confirmed` is resolved SERVER-SIDE to confirmed-or-rescheduled (see
 *  services/console.list_bookings), so picking Confirmed returns every card that
 *  says Confirmed — the filter and the badge cannot disagree.
 *
 *  The backend still accepts an explicit `rescheduled`, so "which bookings were
 *  moved" remains answerable; it is just not a question this filter row asks.
 *  The Overview funnel still counts reschedules as their own stage. */
const STATUS_OPTIONS: Option[] = [
  { value: 'confirmed', label: 'Confirmed' },
  { value: 'cancelled', label: 'Cancelled' },
]

/** One booking as a full-width horizontal row (list view). Same double-rail and
 *  same StatusPill / DetailRow pieces as the card, laid out in columns; sections
 *  wrap on a narrow window rather than overflowing. */
function BookingListRow({ b }: { b: BookingRow }) {
  const doorstep = !b.showroom_name
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  return (
    // flexDirection column + an inner row, so the expanded feedback panel spans
    // the full width beneath the row rather than becoming another flex column
    // squeezed in beside Location.
    <Box sx={{ ...railSx, p: 2, pl: 3 }}>
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 3, flexWrap: 'wrap' }}>
      <Box sx={{ width: 168, flexShrink: 0 }}>
        <Typography
          sx={{ fontFamily: "'Archivo', system-ui, sans-serif", fontWeight: 700, fontSize: 15, color: VALUE_DARK }}
        >
          {b.ref}
        </Typography>
        <Typography sx={{ fontSize: 11.5, color: LABEL_GREY, mt: 0.25 }}>
          Booked {fmtDate(b.created_at)}
        </Typography>
      </Box>

      <Box sx={{ flexShrink: 0 }}>
        <StatusPill b={b} />
      </Box>

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, width: 250, flexShrink: 0 }}>
        <Avatar sx={{ width: 38, height: 38, bgcolor: AMBER, color: VALUE_DARK, fontSize: 13, fontWeight: 700 }}>
          {initials(b.customer_name_en)}
        </Avatar>
        <Box sx={{ minWidth: 0 }}>
          <Typography sx={{ fontSize: 13, fontWeight: 600, color: VALUE_DARK }} noWrap>
            {b.customer_name_en ?? '—'}
          </Typography>
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            {b.customer_mobile && (
              <Typography sx={{ fontSize: 11.5, color: LABEL_GREY }}>{b.customer_mobile}</Typography>
            )}
            {b.customer_name_ar && (
              <Typography component="span" dir="rtl" sx={{ fontSize: 11.5, color: LABEL_GREY }}>
                <bdi>{b.customer_name_ar}</bdi>
              </Typography>
            )}
          </Box>
        </Box>
      </Box>

      <Box sx={{ minWidth: 120 }}>
        <DetailRow
          label="Vehicle"
          icon={<DirectionsCarRoundedIcon sx={{ fontSize: 16 }} />}
          value={[b.vehicle_name, b.variant_name].filter(Boolean).join(' ') || '—'}
          dense
        />
      </Box>
      <Box sx={{ minWidth: 210 }}>
        <DetailRow
          label="When"
          icon={<ScheduleRoundedIcon sx={{ fontSize: 16 }} />}
          value={fmtRange(b.start, b.end) ?? '—'}
          dense
        />
      </Box>
      <Box sx={{ minWidth: 180 }}>
        <DetailRow
          label="Location"
          icon={
            doorstep ? (
              <HomeWorkRoundedIcon sx={{ fontSize: 16 }} />
            ) : (
              <StorefrontRoundedIcon sx={{ fontSize: 16 }} />
            )
          }
          value={locationText(b)}
          dense
        />
      </Box>

      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, ml: 'auto' }}>
        {b.status !== 'cancelled' && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <WhatsAppIcon sx={{ fontSize: 16, color: GREEN }} />
            <Typography sx={{ fontSize: 12.5, color: GREEN, whiteSpace: 'nowrap' }}>
              WhatsApp confirmation sent
            </Typography>
          </Box>
        )}
        <FeedbackToggle
          b={b}
          open={feedbackOpen}
          onToggle={() => setFeedbackOpen((v) => !v)}
          dense
        />
      </Box>
    </Box>

    {feedbackOpen && b.feedback && <FeedbackPanel fb={b.feedback} />}
    </Box>
  )
}

type ViewMode = 'grid' | 'list'

/** Grid / list segmented toggle. The active segment carries the amber fill; the
 *  other stays white. Grid is the default (see the Bookings state). */
function ViewToggle({ value, onChange }: { value: ViewMode; onChange: (v: ViewMode) => void }) {
  const seg = (mode: ViewMode, icon: ReactNode, label: string) => {
    const active = value === mode
    return (
      <Box
        component="button"
        type="button"
        aria-label={label}
        aria-pressed={active}
        onClick={() => onChange(mode)}
        sx={{
          display: 'grid',
          placeItems: 'center',
          width: 46,
          height: 38,
          border: 'none',
          cursor: 'pointer',
          bgcolor: active ? AMBER : '#fff',
          color: active ? '#fff' : 'text.secondary',
        }}
      >
        {icon}
      </Box>
    )
  }
  return (
    <Box
      sx={{
        display: 'inline-flex',
        borderRadius: '10px',
        overflow: 'hidden',
        border: CARD_BORDER,
        flexShrink: 0,
      }}
    >
      {seg('grid', <GridViewRoundedIcon sx={{ fontSize: 18 }} />, 'Grid view')}
      {seg('list', <FormatListBulletedRoundedIcon sx={{ fontSize: 18 }} />, 'List view')}
    </Box>
  )
}

export default function Bookings() {
  const [status, setStatus] = useState('')
  const [vehicleId, setVehicleId] = useState('')
  const [showroomId, setShowroomId] = useState('')
  const [dateKey, setDateKey] = useState('')
  const [view, setView] = useState<ViewMode>('grid') // grid by default

  // Vehicle and Location options come from the catalogue, so the pills offer
  // exactly the models and branches that exist rather than free text.
  const [vehicles, setVehicles] = useState<VehicleRow[]>([])
  const [showrooms, setShowrooms] = useState<ShowroomRow[]>([])
  useEffect(() => {
    const ctrl = new AbortController()
    fetchVehicles({ limit: 200 }, { signal: ctrl.signal }).then((r) => {
      if (r.ok) setVehicles(r.data.rows)
    })
    fetchShowrooms({ limit: 200 }, { signal: ctrl.signal }).then((r) => {
      if (r.ok) setShowrooms(r.data.rows)
    })
    return () => ctrl.abort()
  }, [])

  const vehicleOptions = useMemo<Option[]>(
    () => vehicles.map((v) => ({ value: String(v.vehicle_id), label: v.name ?? `#${v.vehicle_id}` })),
    [vehicles],
  )
  const showroomOptions = useMemo<Option[]>(
    () => showrooms.map((s) => ({ value: String(s.showroom_id), label: s.name ?? `#${s.showroom_id}` })),
    [showrooms],
  )

  const filters = useMemo(
    () => ({
      status,
      vehicle_id: vehicleId,
      showroom_id: showroomId,
      ...dateRange(dateKey),
    }),
    [status, vehicleId, showroomId, dateKey],
  )
  const list = useList<BookingRow>(fetchBookings, filters, 12)

  return (
    <Box>
      <TopBar
        title="Bookings"
        subtitle="Every test drive, active and cancelled."
        countLabel={`Total Bookings: ${list.total}`}
      />

      {/* Filter pills on the left, the grid/list toggle on the right — one row,
          aligned. Order matches the Figma: Status, Vehicle, Date, Location. */}
      <Box
        sx={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 2,
          mb: 3,
        }}
      >
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}>
        <FilterPill
          label="Status"
          value={status}
          onChange={setStatus}
          options={STATUS_OPTIONS}
          allLabel="Status: All"
        />
        <FilterPill
          label="Vehicle"
          value={vehicleId}
          onChange={setVehicleId}
          options={vehicleOptions}
          allLabel="Vehicle"
        />
        <FilterPill
          label="Date"
          value={dateKey}
          onChange={setDateKey}
          options={DATE_OPTIONS}
          allLabel="Date"
        />
        <FilterPill
          label="Location"
          value={showroomId}
          onChange={setShowroomId}
          options={showroomOptions}
          allLabel="Location: All"
        />
        {/* Only shown once at least one filter is set — a permanent "Clear" with
            nothing to clear reads as a dead control. One tap resets all four. */}
        {(status || vehicleId || showroomId || dateKey) && (
          <Button
            variant="text"
            startIcon={<CloseRoundedIcon />}
            onClick={() => {
              setStatus('')
              setVehicleId('')
              setShowroomId('')
              setDateKey('')
            }}
            sx={{ color: 'text.secondary', alignSelf: 'center' }}
          >
            Clear filters
          </Button>
        )}
        </Box>

        <ViewToggle value={view} onChange={setView} />
      </Box>

      {list.error && <ErrorState message={list.error} onRetry={list.reload} />}
      {list.loading && !list.rows.length && <CardSkeletons count={3} height={420} />}
      {!list.loading && !list.error && !list.rows.length && (
        <EmptyState message="No bookings match this filter." />
      )}

      {!!list.rows.length &&
        (view === 'grid' ? (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 2 }}>
            {list.rows.map((b) => (
              <BookingCard key={b.booking_id} b={b} />
            ))}
          </Box>
        ) : (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {list.rows.map((b) => (
              <BookingListRow key={b.booking_id} b={b} />
            ))}
          </Box>
        ))}
    </Box>
  )
}
