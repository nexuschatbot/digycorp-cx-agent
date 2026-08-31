// Showrooms — branches, and the opening hours slot generation actually reads.
//
// TWO HOURS FIELDS, AND THE DIFFERENCE MATTERS. `hours_by_day` is the SOURCE OF
// TRUTH the generator reads; `hours` is a display string. Both are shown,
// because "the showroom says it opens at 9 but no 9am slots exist" is a real
// demo-morning question and the answer is always that the two disagree.
//
// Friday reading 09:00–21:00 here is CORRECT and not a bug: the prayer-window
// block (S-22) lives in slot generation, not in showroom hours. This card is
// exactly where that distinction becomes legible.

import { useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import CardContent from '@mui/material/CardContent'
import Typography from '@mui/material/Typography'
import Chip from '@mui/material/Chip'
import TextField from '@mui/material/TextField'
import Divider from '@mui/material/Divider'
import Button from '@mui/material/Button'
import Accordion from '@mui/material/Accordion'
import AccordionSummary from '@mui/material/AccordionSummary'
import AccordionDetails from '@mui/material/AccordionDetails'
import ExpandMoreRoundedIcon from '@mui/icons-material/ExpandMoreRounded'
import PlaceRoundedIcon from '@mui/icons-material/PlaceRounded'
import CallRoundedIcon from '@mui/icons-material/CallRounded'
import MapRoundedIcon from '@mui/icons-material/MapRounded'

import { fetchShowrooms, type ShowroomRow } from '../adminApi'
import { useList } from '../useList'
import {
  Flex,
  AutoGrid,
  CardSkeletons,
  EmptyState,
  ErrorState,
  Field,
  RailCard,
  TopBar,
} from '../components/ui'
import { placeLabel } from '../labels'

// Matched on the first three letters, so both "mon" (what the seed ships) and
// "monday" sort. Keying on the full name alone silently made the comparator a
// no-op — every lookup missed and the list merely inherited insertion order.
const DAY_ORDER = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
const DAY_NAME: Record<string, string> = {
  mon: 'Monday',
  tue: 'Tuesday',
  wed: 'Wednesday',
  thu: 'Thursday',
  fri: 'Friday',
  sat: 'Saturday',
  sun: 'Sunday',
}

const dayKey = (k: string) => k.trim().toLowerCase().slice(0, 3)
const dayRank = (k: string) => {
  const i = DAY_ORDER.indexOf(dayKey(k))
  return i === -1 ? 99 : i
}

/** The seed stores a day as an [open, close] pair of 24h integers. Rendered as
 *  a clock range rather than raw JSON — FORMATTING numbers the UI was given,
 *  which is the division the schema wants. Anything that is not a 2-number pair
 *  falls back to its JSON, so an unexpected shape shows as-is rather than being
 *  silently mis-drawn as hours it is not. */
function hoursValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value) && value.length === 2 && value.every((n) => typeof n === 'number')) {
    const [open, close] = value as [number, number]
    const clock = (h: number) => {
      const suffix = h >= 12 ? 'pm' : 'am'
      const hour = h % 12 === 0 ? 12 : h % 12
      return `${hour}:00 ${suffix}`
    }
    return `${clock(open)} – ${clock(close)}`
  }
  return JSON.stringify(value)
}

function ShowroomCard({ s }: { s: ShowroomRow }) {
  const hours = s.hours_by_day ?? {}
  const days = Object.keys(hours).sort((a, b) => dayRank(a) - dayRank(b))

  return (
    <RailCard tone="info">
      <CardContent>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" sx={{ fontSize: 16 }}>
              {s.name}
            </Typography>
            <Flex direction="row" spacing={0.5} align="center" sx={{ mt: 0.25 }}>
              <PlaceRoundedIcon sx={{ fontSize: 14, color: 'text.disabled' }} />
              <Typography variant="body2" color="text.secondary" sx={{ fontSize: 12.5 }}>
                {placeLabel(s.city, s.emirate)}
              </Typography>
            </Flex>
          </Box>
          <Chip size="small" variant="outlined" label={`#${s.showroom_id}`} />
        </Box>

        <Divider sx={{ my: 2 }} />

        <Flex spacing={1.5}>
          <Field label="Address" value={s.address} />
          {/* Genuinely bilingual DATA — a street address cannot be composed at
              speaking time the way a price or a seat count can (LOC-01). */}
          <Field label="Address (Arabic)" value={s.address_ar} rtl />
        </Flex>

        <Flex direction="row" spacing={1} sx={{ mt: 2, flexWrap: 'wrap', gap: 1 }}>
          {s.phone && (
            <Chip
              size="small"
              variant="outlined"
              icon={<CallRoundedIcon />}
              label={s.phone}
              component="a"
              href={`tel:${s.phone}`}
              clickable
            />
          )}
          {s.map_link && (
            <Button
              size="small"
              startIcon={<MapRoundedIcon />}
              href={s.map_link}
              target="_blank"
              rel="noreferrer"
            >
              Map pin
            </Button>
          )}
        </Flex>
      </CardContent>

      <Accordion disableGutters elevation={0} sx={{ '&:before': { display: 'none' } }}>
        <AccordionSummary expandIcon={<ExpandMoreRoundedIcon />}>
          <Typography variant="body2" sx={{ fontSize: 13 }}>
            Opening hours{' '}
            <Typography component="span" variant="caption" color="text.secondary">
              ({days.length} days — source of truth)
            </Typography>
          </Typography>
        </AccordionSummary>
        <AccordionDetails>
          <Flex spacing={0.75}>
            {days.map((d) => (
              <Box key={d} sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
                <Typography variant="body2" color="text.secondary" sx={{ fontSize: 12.5 }}>
                  {DAY_NAME[dayKey(d)] ?? d}
                </Typography>
                <Typography variant="body2" sx={{ fontSize: 12.5, fontVariantNumeric: 'tabular-nums' }}>
                  {hoursValue(hours[d])}
                </Typography>
              </Box>
            ))}
            {s.hours && (
              <>
                <Divider sx={{ my: 1 }} />
                <Field label="Display string" value={s.hours} />
              </>
            )}
          </Flex>
        </AccordionDetails>
      </Accordion>
    </RailCard>
  )
}

export default function Showrooms() {
  const [emirate, setEmirate] = useState('')
  const filters = useMemo(() => ({ emirate }), [emirate])
  const list = useList<ShowroomRow>(fetchShowrooms, filters, 12)

  return (
    <Box>
      <TopBar
        title="Showrooms"
        subtitle="Branches, and the opening hours that drive slot generation."
        countLabel={`Total Showrooms: ${list.total.toLocaleString('en-US')}`}
      />

      <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', mb: 3 }}>
        <TextField
          label="Emirate"
          value={emirate}
          placeholder="All"
          onChange={(e) => setEmirate(e.target.value)}
          sx={{ minWidth: 180 }}
        />
      </Box>

      {list.error && <ErrorState message={list.error} onRetry={list.reload} />}
      {list.loading && !list.rows.length && <CardSkeletons count={4} height={300} />}
      {!list.loading && !list.error && !list.rows.length && (
        <EmptyState message="No showrooms match this filter." />
      )}

      {!!list.rows.length && (
        <AutoGrid min="21rem">
          {list.rows.map((s) => (
            <ShowroomCard key={s.showroom_id} s={s} />
          ))}
        </AutoGrid>
      )}
    </Box>
  )
}
