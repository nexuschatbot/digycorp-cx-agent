// Handoffs — the human-callback queue (T8.1).
//
// WHY THIS SCREEN EXISTS NOW. services/escalation.py has always said so in its
// own docstring: it writes an audit row and "there is no supervisor UI and no
// real callback is placed". That was defensible while a handoff was a rare dead
// end. From Phase 6 it is the FINISH LINE of the highest-value conversations in
// the system — someone who has decided to buy, or wants a number negotiated, is
// handed here and nowhere else. A queue nobody can see is a promise nobody keeps.
//
// OLDEST FIRST, AND IT IS THE OPPOSITE OF EVERY OTHER TABLE HERE. Newest-first
// is right for an audit log, which is read to find out what just happened. This
// is a WORK QUEUE: the person waiting longest is the one being let down most,
// and sorting them to the bottom is how a queue quietly starves its oldest entry.
//
// AGE IS THE COLUMN THAT MATTERS, so it is rendered as elapsed time rather than
// a timestamp. "3 hours ago" is actionable; "2026-08-29T11:04:22" has to be
// subtracted from now before it means anything.

import { useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import MenuItem from '@mui/material/MenuItem'
import Pagination from '@mui/material/Pagination'
import Table from '@mui/material/Table'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'

import { fetchHandoffs, type HandoffRow } from '../adminApi'
import { useList } from '../useList'
import { CardSkeletons, EmptyState, ErrorState, TopBar } from '../components/ui'
import TakeoverDrawer from '../components/TakeoverDrawer'

/** Elapsed time, in the words an operator would use. */
function ageOf(ts: string | null): { label: string; hours: number } {
  if (!ts) return { label: '—', hours: 0 }
  const then = new Date(ts).getTime()
  if (Number.isNaN(then)) return { label: '—', hours: 0 }
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000))
  const hours = mins / 60
  if (mins < 60) return { label: `${mins} min ago`, hours }
  if (hours < 24) return { label: `${Math.round(hours)} h ago`, hours }
  return { label: `${Math.round(hours / 24)} d ago`, hours }
}

/** Waiting longer reads louder. Not a severity — nobody set a priority; it is
 *  simply how long somebody has been waiting for a call. */
function ageTone(hours: number): 'default' | 'warning' | 'error' {
  if (hours >= 24) return 'error'
  if (hours >= 4) return 'warning'
  return 'default'
}

const REASONS = ['price', 'finance', 'complaint', 'requested_human', 'repeated_failure', 'other']

export default function Handoffs() {
  const [reason, setReason] = useState('')
  const filters = useMemo(() => ({ reason }), [reason])
  const list = useList<HandoffRow>(fetchHandoffs, filters, 25)
  const pageCount = Math.ceil(list.total / list.limit)
  // The row whose live-chat drawer is open (human takeover, 2026-08-31).
  const [takingOver, setTakingOver] = useState<HandoffRow | null>(null)

  return (
    <Box>
      <TopBar
        title="Handoffs"
        subtitle="Callbacks Aisha promised a person would make. Oldest first — the longest wait is the most overdue."
        countLabel={`Waiting: ${list.total.toLocaleString('en-US')}`}
      />

      <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', mb: 3 }}>
        <TextField
          select
          label="Reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          sx={{ minWidth: 200 }}
        >
          <MenuItem value="">All</MenuItem>
          {REASONS.map((r) => (
            <MenuItem key={r} value={r}>
              {r.replace(/_/g, ' ')}
            </MenuItem>
          ))}
        </TextField>
      </Box>

      {list.error && <ErrorState message={list.error} onRetry={list.reload} />}
      {list.loading && !list.rows.length && <CardSkeletons count={3} height={80} />}
      {!list.loading && !list.error && !list.rows.length && (
        <EmptyState message="Nobody is waiting for a callback." />
      )}

      {!!list.rows.length && (
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Waiting</TableCell>
                <TableCell>Customer</TableCell>
                <TableCell>Reason</TableCell>
                <TableCell>What they asked about</TableCell>
                <TableCell>Session</TableCell>
                <TableCell>Live chat</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {list.rows.map((h) => {
                const age = ageOf(h.ts)
                return (
                  <TableRow key={h.audit_id} hover>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>
                      <Chip size="small" label={age.label} color={ageTone(age.hours)}
                            variant={ageTone(age.hours) === 'default' ? 'outlined' : 'filled'} />
                    </TableCell>
                    <TableCell>
                      {h.customer_name ? (
                        <>
                          <Typography variant="body2">{h.customer_name}</Typography>
                          <Typography variant="caption" color="text.secondary"
                                      sx={{ fontFamily: 'ui-monospace, Menlo, monospace' }}>
                            {h.customer_mobile}
                          </Typography>
                        </>
                      ) : (
                        // A callback needs somebody to call. Rule 13 makes Aisha
                        // ask for a name and number BEFORE handing off, so this
                        // should not happen — and if it does, it is the row an
                        // operator most needs to notice.
                        <Typography variant="body2" color="error.main">
                          not identified
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>
                      <Chip size="small" variant="outlined" label={h.reason.replace(/_/g, ' ')} />
                    </TableCell>
                    <TableCell sx={{ maxWidth: 360 }}>
                      {/* The customer's own words, never spoken back as policy. */}
                      <Typography variant="body2" color={h.topic ? 'text.primary' : 'text.disabled'}>
                        {h.topic ?? 'not recorded'}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Tooltip title={h.session}>
                        <Typography variant="caption"
                                    sx={{ fontFamily: 'ui-monospace, Menlo, monospace' }}>
                          {h.session.slice(0, 10)}…
                        </Typography>
                      </Tooltip>
                    </TableCell>
                    <TableCell sx={{ whiteSpace: 'nowrap' }}>
                      {/* Human takeover (2026-08-31): step into the chat as a
                          person — the AI is refused from that moment on. A row
                          someone holds says who; opening it again rejoins. */}
                      {h.taken_over_by ? (
                        <Chip
                          size="small"
                          color="success"
                          variant="outlined"
                          label={`Held by ${h.taken_over_by}`}
                          onClick={() => setTakingOver(h)}
                        />
                      ) : (
                        <Button size="small" variant="outlined"
                                onClick={() => setTakingOver(h)}>
                          Take over
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </Box>
      )}

      {takingOver && (
        <TakeoverDrawer
          row={takingOver}
          onClose={() => setTakingOver(null)}
          onChanged={list.reload}
        />
      )}

      {pageCount > 1 && (
        <Box sx={{ display: 'flex', justifyContent: 'center', mt: 3 }}>
          <Pagination
            count={pageCount}
            page={Math.floor(list.offset / list.limit) + 1}
            onChange={(_, p) => list.setOffset((p - 1) * list.limit)}
            color="primary"
          />
        </Box>
      )}
    </Box>
  )
}
