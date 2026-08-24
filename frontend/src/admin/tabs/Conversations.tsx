// Conversations — one row per session, with the per-conversation audit drawer.
//
// THIS TAB IS WHERE THE CONTRACTED /api/console/audit ENDPOINT IS USED. The
// frontend rule in CLAUDE.md is explicit: "the audit drawer reads
// /api/console/audit for the current session". Selecting a conversation opens
// exactly that — the signed bare-list endpoint, scoped to one session. The Audit
// tab is a different thing: a cross-session browser over /audit-log.
//
// THE TRAIL RENDERS AS A TIMELINE, not a table. An audit trail is a sequence —
// what happened, then what happened next — and four columns hid the one property
// that matters most about it.
//
// THERE IS NO Conversation TABLE. session_id is a bare string shared by the
// checkpointer, the turns, the audit rows and the slot lock, so every column is
// derived: counts and timestamps aggregate conversation_turns, and `status` is
// resolved from the append-only trail rather than a stored boolean.

import { useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import CardActionArea from '@mui/material/CardActionArea'
import CardContent from '@mui/material/CardContent'
import Typography from '@mui/material/Typography'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import Drawer from '@mui/material/Drawer'
import IconButton from '@mui/material/IconButton'
import Button from '@mui/material/Button'
import Skeleton from '@mui/material/Skeleton'
import Tooltip from '@mui/material/Tooltip'

import CloseRoundedIcon from '@mui/icons-material/CloseRounded'
import RefreshRoundedIcon from '@mui/icons-material/RefreshRounded'
import MicRoundedIcon from '@mui/icons-material/MicRounded'
import ChatBubbleOutlineRoundedIcon from '@mui/icons-material/ChatBubbleOutlineRounded'
import TranslateRoundedIcon from '@mui/icons-material/TranslateRounded'

import {
  fetchConversations,
  fetchSessionAudit,
  type AuditRow,
  type ConversationRow,
} from '../adminApi'
import { useList, useResource } from '../useList'
import {
  Flex,
  AutoGrid,
  CardSkeletons,
  EmptyState,
  ErrorState,
  Field,
  FieldGrid,
  PageHeader,
  Payload,
  StatusChip,
} from '../components/ui'
import { fmtDateTime, shortSession } from '../format'
import { actorLabel, auditTypeLabel, langLabel } from '../labels'
import { statusColor } from '../theme'

function TrailDrawer({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const fetcher = useMemo(
    () => (opts: Parameters<typeof fetchSessionAudit>[1]) => fetchSessionAudit(sessionId, opts),
    [sessionId],
  )
  const { data, loading, error, reload } = useResource<AuditRow[]>(fetcher, sessionId)

  return (
    <Drawer
      anchor="right"
      open
      onClose={onClose}
      slotProps={{ paper: { sx: { width: { xs: '100%', sm: 520 } } } }}
    >
      <Box sx={{ p: 2.5, borderBottom: '1px solid', borderColor: 'divider', bgcolor: '#fafbfc' }}>
        <Flex direction="row" align="center" justify="space-between">
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" sx={{ fontSize: 16 }}>
              Audit trail
            </Typography>
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ fontFamily: 'ui-monospace, Menlo, monospace', wordBreak: 'break-all' }}
            >
              {sessionId}
            </Typography>
          </Box>
          <Flex direction="row" spacing={0.5}>
            <Tooltip title="Refresh">
              <IconButton size="small" onClick={reload}>
                <RefreshRoundedIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <IconButton size="small" onClick={onClose}>
              <CloseRoundedIcon fontSize="small" />
            </IconButton>
          </Flex>
        </Flex>
      </Box>

      <Box sx={{ p: 2.5, overflowY: 'auto' }}>
        {error && <ErrorState message={error} onRetry={reload} />}
        {loading && (
          <Flex spacing={1.5}>
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} variant="rounded" height={64} />
            ))}
          </Flex>
        )}

        {/* An empty trail is a real state, not a failure: a session can exist in
            the transcript before anything auditable has happened in it. */}
        {!loading && !error && data?.length === 0 && (
          <EmptyState message="No audit events for this conversation." />
        )}

        {!!data?.length && (
          <Box
            sx={{
              position: 'relative',
              pl: 3,
              '&::before': {
                content: '""',
                position: 'absolute',
                left: 5,
                top: 8,
                bottom: 8,
                width: 2,
                bgcolor: 'divider',
              },
            }}
          >
            {data.map((e) => (
              <Box
                key={e.id}
                sx={{
                  position: 'relative',
                  py: 1.75,
                  borderBottom: '1px solid',
                  borderColor: 'divider',
                  '&:last-of-type': { borderBottom: 'none' },
                  '&::before': {
                    content: '""',
                    position: 'absolute',
                    left: -23,
                    top: 22,
                    width: 10,
                    height: 10,
                    borderRadius: '50%',
                    bgcolor: 'background.paper',
                    border: '2px solid',
                    borderColor: `${statusColor(e.actor) === 'default' ? 'grey.400' : `${statusColor(e.actor)}.main`}`,
                  },
                }}
              >
                <Flex direction="row" align="center" spacing={1} sx={{ flexWrap: 'wrap' }}>
                  <Typography sx={{ fontWeight: 600, fontSize: 13.5 }}>
                    {auditTypeLabel(e.type)}
                  </Typography>
                  <StatusChip value={e.actor} label={actorLabel(e.actor)} />
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 'auto' }}>
                    {fmtDateTime(e.ts)}
                  </Typography>
                </Flex>
                <Box sx={{ mt: 0.5 }}>
                  <Payload payload={e.payload} />
                </Box>
              </Box>
            ))}
          </Box>
        )}
      </Box>
    </Drawer>
  )
}

function ConversationCard({
  c,
  onOpen,
}: {
  c: ConversationRow
  onOpen: () => void
}) {
  return (
    <Card sx={{ borderLeft: '3px solid', borderLeftColor: c.status === 'active' ? 'success.main' : 'grey.300' }}>
      <CardActionArea onClick={onOpen}>
        <CardContent>
          <Flex direction="row" align="center" justify="space-between" spacing={1}>
            <Typography
              sx={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 13.5, fontWeight: 600 }}
            >
              {shortSession(c.session_id)}
            </Typography>
            <StatusChip value={c.status} />
          </Flex>

          <Divider sx={{ my: 1.75 }} />

          <FieldGrid min="7rem">
            <Field
              label="Turns"
              value={
                <Flex direction="row" spacing={0.75} align="center">
                  <ChatBubbleOutlineRoundedIcon sx={{ fontSize: 15, color: 'text.disabled' }} />
                  {c.turns}
                </Flex>
              }
            />
            <Field
              label="Voice notes"
              value={
                c.voice_turns > 0 ? (
                  <Flex direction="row" spacing={0.75} align="center">
                    <MicRoundedIcon sx={{ fontSize: 15, color: 'text.disabled' }} />
                    {c.voice_turns}
                  </Flex>
                ) : null
              }
            />
          </FieldGrid>

          <Flex direction="row" spacing={1} sx={{ mt: 1.75, flexWrap: 'wrap', gap: 1 }}>
            {c.langs.map((l) => (
              <Chip key={l} size="small" variant="outlined" icon={<TranslateRoundedIcon />} label={langLabel(l)} />
            ))}
            {/* Both languages in one session is a mid-conversation EN↔AR switch
                (S-19) — the scenario the demo runs on purpose, worth finding. */}
            {c.langs.length > 1 && (
              <Tooltip title="Mid-conversation language switch (S-19)">
                <Chip size="small" color="info" label="Code-switch" />
              </Tooltip>
            )}
          </Flex>

          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.75 }}>
            Last activity {fmtDateTime(c.last_ts)}
          </Typography>
        </CardContent>
      </CardActionArea>
    </Card>
  )
}

export default function Conversations() {
  const [selected, setSelected] = useState<string | null>(null)
  const list = useList<ConversationRow>(fetchConversations, {}, 12)

  return (
    <Box>
      <PageHeader
        title="Conversations"
        subtitle="Select a conversation to open its audit trail."
        count={list.total}
        actions={
          <Button startIcon={<RefreshRoundedIcon />} onClick={list.reload} disabled={list.loading}>
            Refresh
          </Button>
        }
      />

      {list.error && <ErrorState message={list.error} onRetry={list.reload} />}
      {list.loading && !list.rows.length && <CardSkeletons count={6} height={220} />}
      {!list.loading && !list.error && !list.rows.length && (
        <EmptyState message="No conversations yet." />
      )}

      {!!list.rows.length && (
        <AutoGrid min="19rem">
          {list.rows.map((c) => (
            <ConversationCard key={c.session_id} c={c} onOpen={() => setSelected(c.session_id)} />
          ))}
        </AutoGrid>
      )}

      {selected && <TrailDrawer sessionId={selected} onClose={() => setSelected(null)} />}
    </Box>
  )
}
