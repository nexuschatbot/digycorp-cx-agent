// Shared console primitives, on MUI.
//
// The first console rendered every entity as a wide table, and that was the
// core mistake: a booking is not a row, it is a record with a headline (the
// reference), a state (confirmed), and a dozen supporting facts. Forcing it into
// 15 columns meant nothing could be emphasised and everything needed horizontal
// scrolling.
//
// So the rule is: DataGrid ONLY where the data is genuinely tabular and
// high-volume (slots, audit — thousands of rows, scanned by column). Everything
// else is cards, where hierarchy is possible.

import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import Chip from '@mui/material/Chip'
import Typography from '@mui/material/Typography'
import Skeleton from '@mui/material/Skeleton'
import Alert from '@mui/material/Alert'
import AlertTitle from '@mui/material/AlertTitle'
import Button from '@mui/material/Button'
import Accordion from '@mui/material/Accordion'
import AccordionSummary from '@mui/material/AccordionSummary'
import AccordionDetails from '@mui/material/AccordionDetails'
import ExpandMoreRoundedIcon from '@mui/icons-material/ExpandMoreRounded'
import InboxRoundedIcon from '@mui/icons-material/InboxRounded'

import type { SxProps, Theme } from '@mui/material/styles'

import { dirFor } from '../../rtl'
import { statusColor } from '../theme'
import { statusLabel } from '../labels'

/** Flex row/column with alignment.
 *
 *  MUI v9 removed the system props from <Flex> — its own props are now only
 *  children/direction/spacing/divider/useFlexGap, so `alignItems` and
 *  `justifyContent` no longer type-check on it. Rather than push those into
 *  `sx` at every one of ~18 call sites (and re-learn that at every new one),
 *  the alignment-carrying layout box lives here once. <Flex> is still used
 *  directly where only spacing is needed. */
export function Flex({
  // COLUMN, matching <Stack>. This defaulted to 'row' and silently turned every
  // call site that relied on Stack's default into a row — stacked label/value
  // pairs collapsed onto one line. Anything that wants a row says so.
  direction = 'column',
  spacing = 0,
  align,
  justify,
  wrap,
  sx,
  children,
}: {
  direction?: 'row' | 'column'
  spacing?: number
  align?: 'flex-start' | 'center' | 'flex-end' | 'baseline' | 'stretch'
  justify?: 'flex-start' | 'center' | 'flex-end' | 'space-between'
  wrap?: boolean
  sx?: SxProps<Theme>
  children: ReactNode
}) {
  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: direction,
        gap: spacing,
        alignItems: align,
        justifyContent: justify,
        flexWrap: wrap ? 'wrap' : undefined,
        minWidth: 0,
        ...sx,
      }}
    >
      {children}
    </Box>
  )
}

/* -------------------------------------------------------------------------- */

export function PageHeader({
  title,
  subtitle,
  count,
  actions,
}: {
  title: string
  subtitle?: string
  count?: number | null
  actions?: ReactNode
}) {
  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        gap: 2,
        flexWrap: 'wrap',
        mb: 3,
      }}
    >
      <Box>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1.5 }}>
          <Typography variant="h4" sx={{ fontSize: 26, letterSpacing: '-.015em' }}>
            {title}
          </Typography>
          {count != null && (
            <Chip size="small" variant="outlined" label={count.toLocaleString('en-US')} />
          )}
        </Box>
        {subtitle && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            {subtitle}
          </Typography>
        )}
      </Box>
      {actions && (
        <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          {actions}
        </Box>
      )}
    </Box>
  )
}

/** The white top bar over the grey page — the console's page header.
 *
 *  It BLEEDS to the edges of the <main> region by cancelling that region's
 *  padding with negative margins, then re-insets its own content, so the white
 *  band and its 1px underline run full width while the title lines up with the
 *  content below. Shared so every tab that adopts it stays identical (the
 *  Dashboard and Bookings both use it); PageHeader above is the older in-grey
 *  header the remaining tabs still use.
 *
 *  `countLabel` is a full string ("Total Bookings: 24"), not a bare number, so a
 *  tab decides its own wording. Archivo type per the design. */
export function TopBar({
  title,
  subtitle,
  countLabel,
}: {
  title: string
  subtitle?: string
  countLabel?: string
}) {
  return (
    <Box
      sx={{
        mt: { xs: -2, md: -4 },
        mx: { xs: -2, md: -4 },
        mb: 3,
        px: { xs: 2, md: 4 },
        py: 1.5,
        minHeight: 76,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        gap: 1,
        bgcolor: 'rgba(255, 255, 255, 1)',
        borderBottom: '1px solid rgba(226, 232, 240, 1)',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <Typography
          component="h1"
          sx={{
            fontFamily: "'Archivo', system-ui, sans-serif",
            fontWeight: 700,
            fontSize: 26,
            lineHeight: '100%',
            letterSpacing: 0,
            m: 0,
          }}
        >
          {title}
        </Typography>
        {countLabel && (
          <Chip size="small" variant="outlined" label={countLabel} sx={{ borderRadius: 999, height: 26 }} />
        )}
      </Box>
      {subtitle && (
        <Typography
          sx={{
            fontFamily: "'Archivo', system-ui, sans-serif",
            fontWeight: 400,
            fontSize: 13,
            lineHeight: '100%',
            letterSpacing: 0,
            color: 'rgba(92, 100, 107, 1)',
          }}
        >
          {subtitle}
        </Typography>
      )}
    </Box>
  )
}

/** Status as a coloured chip, everywhere, from one mapping. */
export function StatusChip({
  value,
  label,
}: {
  value: string | null | undefined
  label?: string | null
}) {
  if (!value) return <Blank />
  return (
    <Chip
      size="small"
      color={statusColor(value)}
      variant={statusColor(value) === 'default' ? 'outlined' : 'filled'}
      label={label ?? statusLabel(value)}
      sx={{ '& .MuiChip-label': { px: 1 } }}
    />
  )
}

/** An explicit dash, so "we hold no value" is visibly different from a render
 *  bug that dropped one. */
export function Blank() {
  return <Box component="span" sx={{ color: 'text.disabled' }}>—</Box>
}

/** A labelled fact. `rtl` gives the value its own direction so an Arabic name or
 *  address renders correctly inside an otherwise LTR panel (rule 3, LOC-01). */
export function Field({
  label,
  value,
  rtl,
  mono,
}: {
  label: string
  value: ReactNode
  rtl?: boolean
  mono?: boolean
}) {
  const empty = value === null || value === undefined || value === ''
  const text = typeof value === 'string' ? value : ''
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography
        variant="overline"
        sx={{ fontSize: 10, color: 'text.disabled', display: 'block', lineHeight: 1.8 }}
      >
        {label}
      </Typography>
      <Typography
        variant="body2"
        // A DIV, not the <p> body2 defaults to. `value` is a ReactNode and
        // callers legitimately pass a Flex (icon + text) — a <div> inside a <p>
        // is invalid HTML, which React warns about and browsers silently
        // restructure, so the fix belongs here rather than at every call site.
        component="div"
        dir={rtl && text ? dirFor(text) : undefined}
        sx={{
          fontSize: 13.5,
          overflowWrap: 'anywhere',
          fontFamily: mono ? 'ui-monospace, SFMono-Regular, Menlo, monospace' : undefined,
        }}
      >
        {empty ? <Blank /> : value}
      </Typography>
    </Box>
  )
}

/** Responsive auto-fitting grid. Used for both card lists and field groups —
 *  CSS grid rather than MUI <Grid> so a card can declare its own minimum and
 *  the browser decides the column count. */
export function AutoGrid({
  children,
  min = '20rem',
  gap = 2,
}: {
  children: ReactNode
  min?: string
  gap?: number
}) {
  return (
    <Box
      sx={{
        display: 'grid',
        gridTemplateColumns: `repeat(auto-fill, minmax(${min}, 1fr))`,
        gap,
      }}
    >
      {children}
    </Box>
  )
}

export function FieldGrid({ children, min = '8.5rem' }: { children: ReactNode; min?: string }) {
  return (
    <Box
      sx={{
        display: 'grid',
        gridTemplateColumns: `repeat(auto-fit, minmax(${min}, 1fr))`,
        gap: 2,
      }}
    >
      {children}
    </Box>
  )
}

/** A card with a coloured rail keyed to state — lets a cancelled booking be
 *  findable in a grid at a glance, before a single word is read. */
export function RailCard({
  tone,
  children,
}: {
  tone: 'success' | 'warning' | 'error' | 'info' | 'default'
  children: ReactNode
}) {
  const color =
    tone === 'default' ? '#cbd1db' : `var(--mui-palette-${tone}-main)`
  return (
    <Card sx={{ borderLeft: `3px solid ${color}`, display: 'flex', flexDirection: 'column' }}>
      {children}
    </Card>
  )
}

/* -------------------------------------------------------------------------- */
/* States — the four stay distinct everywhere. Collapsing "empty" into "error"
   is the specific failure that would let a down backend read as "no bookings". */
/* -------------------------------------------------------------------------- */

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Alert
      severity="error"
      variant="outlined"
      action={
        onRetry && (
          <Button color="inherit" size="small" onClick={onRetry}>
            Retry
          </Button>
        )
      }
    >
      <AlertTitle>Could not load this view</AlertTitle>
      {message}
    </Alert>
  )
}

export function EmptyState({ message }: { message: string }) {
  return (
    <Card
      sx={{
        p: 6,
        textAlign: 'center',
        border: '1px dashed',
        borderColor: 'divider',
        boxShadow: 'none',
        '&:hover': { boxShadow: 'none' },
      }}
    >
      <InboxRoundedIcon sx={{ fontSize: 40, color: 'text.disabled', mb: 1 }} />
      <Typography variant="body2" color="text.secondary">
        {message}
      </Typography>
    </Card>
  )
}

/** Shaped placeholders rather than the word "Loading…". On a fast local backend
 *  the difference is invisible; over a conference network it is the difference
 *  between a screen that looks broken and one that looks busy. */
export function CardSkeletons({ count = 6, height = 190 }: { count?: number; height?: number }) {
  return (
    <AutoGrid>
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} variant="rounded" height={height} />
      ))}
    </AutoGrid>
  )
}

/** Collapsible raw JSON, for audit and notification payloads. Free-shaped by
 *  design — each event carries what it needs — so picking "interesting" keys
 *  would hide the one an investigation actually wants. */
export function Payload({ payload }: { payload: Record<string, unknown> }) {
  const keys = Object.keys(payload ?? {})
  if (keys.length === 0) return <Blank />
  return (
    <Accordion
      disableGutters
      elevation={0}
      sx={{ bgcolor: 'transparent', '&:before': { display: 'none' } }}
    >
      <AccordionSummary
        expandIcon={<ExpandMoreRoundedIcon fontSize="small" />}
        sx={{ minHeight: 0, p: 0, '& .MuiAccordionSummary-content': { my: 0.5 } }}
      >
        <Typography variant="body2" color="primary" sx={{ fontSize: 12.5 }}>
          {keys.length} field{keys.length === 1 ? '' : 's'}
        </Typography>
      </AccordionSummary>
      <AccordionDetails sx={{ p: 0, pb: 1 }}>
        <Box
          component="pre"
          sx={{
            m: 0,
            p: 1.5,
            bgcolor: '#f8f9fb',
            border: '1px solid',
            borderColor: 'divider',
            borderRadius: 1.5,
            fontSize: 11.5,
            lineHeight: 1.6,
            maxHeight: 260,
            overflow: 'auto',
          }}
        >
          {JSON.stringify(payload, null, 2)}
        </Box>
      </AccordionDetails>
    </Accordion>
  )
}
