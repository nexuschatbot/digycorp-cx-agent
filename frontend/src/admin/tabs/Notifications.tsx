// Notifications — the WhatsApp simulator feed (contracted endpoint, bare list).
//
// NOT PAGINATED, because the contract does not paginate it: the shape is signed
// in CLAUDE.md as a plain array and the console adapts to it rather than
// reshaping a signed response for the convenience of a shared hook.
//
// RENDERED AS A SCHEDULE, because that is what it is: messages queued against a
// booking, in send order. A table of seven columns hid the sequence.
//
// AN EMPTY `body` IS HONEST, NOT MISSING. Rows carry `payload` — the structured,
// language-neutral facts — and `body`, the rendered customer-facing text, which
// stays "" until the template phase renders it. Substituting English prose here
// would be display text composed at read time, and wrong the moment the
// conversation is Arabic (LOC-01).
//
// SIMULATED, AND SAID SO (rule 16). Nothing here was sent to a phone.

import { useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Typography from '@mui/material/Typography'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Button from '@mui/material/Button'
import Alert from '@mui/material/Alert'

import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import ScheduleSendRoundedIcon from '@mui/icons-material/ScheduleSendRounded'

import { fetchNotifications, type NotificationRow } from '../adminApi'
import { useResource } from '../useList'
import {
  Flex,
  CardSkeletons,
  EmptyState,
  ErrorState,
  Field,
  FieldGrid,
  PageHeader,
  Payload,
  StatusChip,
} from '../components/ui'
import { fmtDateTime } from '../format'
import { kindLabel, langLabel } from '../labels'

export default function Notifications() {
  const [status, setStatus] = useState('')
  const [kind, setKind] = useState('')

  const key = `${status}|${kind}`
  const fetcher = useMemo(
    () => (opts: Parameters<typeof fetchNotifications>[1]) =>
      fetchNotifications({ status, kind }, opts),
    [status, kind],
  )
  const { data, loading, error, reload } = useResource<NotificationRow[]>(fetcher, key)
  const rows = data ?? []

  return (
    <Box>
      <PageHeader
        title="Notifications"
        subtitle="Scheduled WhatsApp messages, in send order."
        count={rows.length}
        actions={
          <>
            <TextField select label="Status" value={status} onChange={(e) => setStatus(e.target.value)} sx={{ minWidth: 140 }}>
              <MenuItem value="">All</MenuItem>
              <MenuItem value="scheduled">Scheduled</MenuItem>
              <MenuItem value="sent">Sent</MenuItem>
              <MenuItem value="cancelled">Cancelled</MenuItem>
            </TextField>
            <TextField select label="Kind" value={kind} onChange={(e) => setKind(e.target.value)} sx={{ minWidth: 160 }}>
              <MenuItem value="">All</MenuItem>
              {['confirmation', 't24', 't2', 'reschedule', 'cancellation'].map((k) => (
                <MenuItem key={k} value={k}>
                  {kindLabel(k)}
                </MenuItem>
              ))}
            </TextField>
            <Button startIcon={<RefreshRoundedIcon />} onClick={reload} disabled={loading}>
              Refresh
            </Button>
          </>
        }
      />

      <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
        Simulated feed — nothing is sent to a real phone.
      </Alert>

      {error && <ErrorState message={error} onRetry={reload} />}
      {loading && !rows.length && <CardSkeletons count={3} height={160} />}
      {!loading && !error && !rows.length && (
        <EmptyState message="No notifications match these filters." />
      )}

      <Flex spacing={2}>
        {rows.map((n) => (
          <Card key={n.id}>
            <CardContent>
              <Flex
                direction="row"
                align="center"
                spacing={1.5}
                sx={{ flexWrap: 'wrap', gap: 1 }}
              >
                <ScheduleSendRoundedIcon sx={{ color: 'text.disabled' }} />
                <Typography sx={{ fontWeight: 600, fontSize: 14.5 }}>
                  {kindLabel(n.kind)}
                </Typography>
                <Chip
                  size="small"
                  variant="outlined"
                  label={n.ref}
                  sx={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 11 }}
                />
                <Chip size="small" variant="outlined" label={langLabel(n.lang)} />
                <Box sx={{ ml: 'auto' }}>
                  <StatusChip value={n.status} />
                </Box>
              </Flex>

              <Divider sx={{ my: 1.75 }} />

              <FieldGrid min="11rem">
                <Field label="Send at" value={fmtDateTime(n.send_at)} />
                <Field
                  label="Message body"
                  value={
                    n.body ? (
                      <Box dir="auto">{n.body}</Box>
                    ) : (
                      <Typography variant="body2" color="text.disabled" sx={{ fontSize: 13 }}>
                        Not rendered yet
                      </Typography>
                    )
                  }
                />
              </FieldGrid>

              <Box sx={{ mt: 1.5 }}>
                <Typography variant="overline" sx={{ fontSize: 10, color: 'text.disabled' }}>
                  Payload
                </Typography>
                <Payload payload={n.payload} />
              </Box>
            </CardContent>
          </Card>
        ))}
      </Flex>
    </Box>
  )
}
