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
// THE ELAPSED-TIME COLUMN IS GONE (2026-09-07), and the reason is worth keeping
// so nobody adds it back unguarded. It rendered "3 h ago" from `h.ts`, which
// console.py serialises with a bare `.isoformat()` — no offset. The value is
// UTC; JavaScript parses a date-time with no offset as LOCAL. So the age was a
// UTC instant subtracted from a local `Date.now()`, and every row read high by
// exactly the viewer's own offset: measured against live data, a handoff
// written seven minutes earlier rendered as "5 h ago".
//
// The ordering below is unaffected — the server sorts, and it sorts correctly.
// Only the rendered age was wrong, so only the rendered age was removed.
//
// IF IT COMES BACK, parse the timestamp as UTC first. Do NOT reach for a shared
// fix in format.ts: slot times in Slots and Bookings are stored as local
// wall-clock and are already correct, so a blanket "append Z" would shift every
// appointment on the console by the same offset this column was wrong by.

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
import TakeoverDialog from '../components/TakeoverDialog'

const REASONS = ['price', 'finance', 'complaint', 'requested_human', 'repeated_failure', 'other']

export default function Handoffs() {
  const [reason, setReason] = useState('')
  const filters = useMemo(() => ({ reason }), [reason])
  const list = useList<HandoffRow>(fetchHandoffs, filters, 25)
  const pageCount = Math.ceil(list.total / list.limit)
  // The row whose live-chat dialog is open (human takeover, 2026-08-31).
  const [takingOver, setTakingOver] = useState<HandoffRow | null>(null)

  return (
    <Box>
      <TopBar
        title="Handoffs"
        subtitle="Callbacks Aisha promised a person would make. Oldest first."
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
                <TableCell>Customer</TableCell>
                <TableCell>Reason</TableCell>
                <TableCell>What they asked about</TableCell>
                <TableCell>Session</TableCell>
                <TableCell>Live chat</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {list.rows.map((h) => (
                <TableRow key={h.audit_id} hover>
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
                        someone holds says who; opening it again rejoins.
                        THE BUTTON IS EARNED, NOT DEFAULT: only a chat that is
                        still open AND where the customer explicitly asked for
                        a person (reason explicit_request) offers a live
                        takeover. Every other row — closed chats, and
                        escalations Aisha raised herself for price, finance or
                        a complaint — is a callback promise, and showing a
                        dead-end button on those would only invite refusals. */}
                    {h.taken_over_by ? (
                      <Chip
                        size="small"
                        color={h.session_active ? 'success' : 'default'}
                        variant="outlined"
                        label={h.session_active
                          ? `Held by ${h.taken_over_by}`
                          : `Closed — was ${h.taken_over_by}'s`}
                        onClick={() => setTakingOver(h)}
                      />
                    ) : h.session_active && h.reason === 'explicit_request' ? (
                      <Button size="small" variant="outlined"
                              onClick={() => setTakingOver(h)}>
                        Take over
                      </Button>
                    ) : (
                      <Typography variant="caption" color="text.disabled">
                        {h.session_active ? 'callback only' : 'chat closed'}
                      </Typography>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      )}

      {takingOver && (
        <TakeoverDialog
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
