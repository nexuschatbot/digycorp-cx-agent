// Audit — the cross-session trail browser (FR-AD-02, rule 15).
//
// NOT THE CONTRACTED ENDPOINT. This reads /audit-log, the console's own
// paginated view; the signed per-conversation endpoint (/audit?session_id=) is
// what the Conversations drawer opens. Two views over one table, kept apart so
// the contracted shape stays untouched by whatever this one grows.
//
// EVERY WRITE IN THE SYSTEM PASSES THROUGH HERE. The trail is append-only and
// has no update or delete path — an audit trail you can edit is not evidence —
// so this is the one place the agent path and the direct path are visible as one
// history. The actor filter is what makes that legible:
//
//   Customer  a person tapped a chip, or sent a turn
//   Agent     the model chose a tool
//   System    the scheduler or the app acted on its own
//   Admin     a human operator acted through this console
//
// Admin reads zero until the console's write actions ship — correct, not a bug.

import { useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Chip from '@mui/material/Chip'
import { DataGrid, type GridColDef } from '@mui/x-data-grid'

import { fetchAuditLog, type AuditRow } from '../adminApi'
import { useList } from '../useList'
import { ErrorState, PageHeader, Payload, StatusChip } from '../components/ui'
import { fmtDateTime, shortSession } from '../format'
import { actorLabel, auditTypeLabel } from '../labels'

const PAGE = 50

export default function Audit() {
  const [actor, setActor] = useState('')
  const [type, setType] = useState('')
  const [session, setSession] = useState('')

  const filters = useMemo(() => ({ actor, type, session }), [actor, type, session])
  const list = useList<AuditRow>(fetchAuditLog, filters, PAGE)

  const columns: GridColDef<AuditRow>[] = [
    {
      field: 'ts',
      headerName: 'Time',
      width: 200,
      valueGetter: (_v, row) => fmtDateTime(row.ts) ?? '',
    },
    {
      field: 'session',
      headerName: 'Session',
      width: 140,
      renderCell: (p) => (
        <Tooltip title={p.row.session}>
          {/* Clicking narrows to that conversation — the common move when a row
              looks wrong is "show me everything else in this session". */}
          <Chip
            size="small"
            variant="outlined"
            label={shortSession(p.row.session)}
            onClick={() => setSession(p.row.session)}
            sx={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 11 }}
          />
        </Tooltip>
      ),
    },
    {
      field: 'actor',
      headerName: 'Actor',
      width: 120,
      renderCell: (p) => <StatusChip value={p.row.actor} label={actorLabel(p.row.actor)} />,
    },
    {
      field: 'type',
      headerName: 'Type',
      width: 140,
      valueGetter: (_v, row) => auditTypeLabel(row.type) ?? '',
    },
    {
      field: 'correlation',
      headerName: 'Correlation',
      width: 150,
      sortable: false,
      // One tool invocation writes two rows — the choke-point tool_call and the
      // service's semantic event — sharing a correlation_id so the console can
      // group them instead of showing what reads as double-logging.
      renderCell: (p) => {
        const id = p.row.payload?.correlation_id as string | undefined
        return id ? (
          <Tooltip title={id}>
            <Box component="span" sx={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 11 }}>
              {id.slice(0, 10)}…
            </Box>
          </Tooltip>
        ) : (
          <Box component="span" sx={{ color: 'text.disabled' }}>—</Box>
        )
      },
    },
    {
      field: 'payload',
      headerName: 'Payload',
      flex: 1,
      minWidth: 200,
      sortable: false,
      renderCell: (p) => <Payload payload={p.row.payload} />,
    },
  ]

  return (
    <Box>
      <PageHeader
        title="Audit trail"
        subtitle="Append-only. Every intent, tool call, notification, consent change and erasure."
        count={list.total}
      />

      <Card sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', alignItems: 'center' }}>
          <TextField select label="Actor" value={actor} onChange={(e) => setActor(e.target.value)} sx={{ minWidth: 140 }}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="customer">Customer</MenuItem>
            <MenuItem value="agent">Agent</MenuItem>
            <MenuItem value="system">System</MenuItem>
            <MenuItem value="admin">Admin</MenuItem>
          </TextField>
          <TextField select label="Type" value={type} onChange={(e) => setType(e.target.value)} sx={{ minWidth: 150 }}>
            <MenuItem value="">All</MenuItem>
            {['intent', 'tool_call', 'notification', 'consent', 'handoff', 'escalation', 'erasure', 'voice'].map((t) => (
              <MenuItem key={t} value={t}>
                {auditTypeLabel(t)}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Session"
            value={session}
            placeholder="All"
            onChange={(e) => setSession(e.target.value)}
            sx={{ minWidth: 260 }}
          />
          {session && (
            <Chip label="Clear session" onDelete={() => setSession('')} size="small" />
          )}
        </Box>
      </Card>

      {list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : (
        <Card>
          <DataGrid
            rows={list.rows}
            columns={columns}
            getRowId={(r) => r.id}
            loading={list.loading}
            paginationMode="server"
            rowCount={list.total}
            paginationModel={{ page: Math.floor(list.offset / PAGE), pageSize: PAGE }}
            onPaginationModelChange={(m) => list.setOffset(m.page * PAGE)}
            pageSizeOptions={[PAGE]}
            disableRowSelectionOnClick
            getRowHeight={() => 'auto'}
            showToolbar
            sx={{
              border: 'none',
              '& .MuiDataGrid-columnHeaders': { bgcolor: '#fafbfc' },
              '& .MuiDataGrid-cell': { py: 1 },
              '& .MuiDataGrid-cell:focus, & .MuiDataGrid-cell:focus-within': { outline: 'none' },
            }}
            slotProps={{ loadingOverlay: { variant: 'skeleton', noRowsVariant: 'skeleton' } }}
            localeText={{ noRowsLabel: 'No audit events match these filters.' }}
          />
        </Card>
      )}
    </Box>
  )
}
