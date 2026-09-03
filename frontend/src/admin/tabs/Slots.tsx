// Slots — the rolling inventory window, and the biggest table in the console.
//
// A DATAGRID HERE, CARDS ELSEWHERE, AND THE SPLIT IS THE POINT. ~3,800 rows
// across 14 days and 6 showrooms are genuinely tabular: they are scanned by
// column, sorted, and filtered. Cards would be absurd. Conversely a booking is
// a record, not a row, which is why that tab is cards.
//
// SERVER-SIDE PAGING. The grid is told the real row count and asked to fetch a
// page at a time; loading 3,800 rows to let the client sort them would be a
// waste that grows with the seed.
//
// THE LOCK COLUMNS ARE OPERATOR-ONLY. Rule 14's corollary forbids the AGENT ever
// learning a lock's expiry — a model told "this expires at 19:42" will
// eventually pressure a customer with it — but that rule is about what the model
// is TOLD, not what an operator may inspect. Releasing a stuck lock correctly
// requires knowing who holds it and since when.

import { useEffect, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { DataGrid, type GridColDef } from '@mui/x-data-grid'

import {
  fetchShowrooms,
  fetchSlots,
  fetchVehicles,
  type ShowroomRow,
  type SlotRow,
  type VehicleRow,
} from '../adminApi'
import { useList } from '../useList'
import { ErrorState, StatusChip, TopBar } from '../components/ui'
import { fmtDate, fmtTime, shortSession } from '../format'
import { locationLabel, slotStatusLabel } from '../labels'

const PAGE = 50

// Today as a LOCAL yyyy-mm-dd (never toISOString, which is UTC and rolls the
// date early/late by the timezone offset — wrong for a UAE operator near
// midnight). Used as the default "From" filter below.
function todayLocal(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export default function Slots() {
  const [status, setStatus] = useState('')
  const [locationType, setLocationType] = useState('')
  const [vehicleId, setVehicleId] = useState('')
  const [showroomId, setShowroomId] = useState('')
  // DEFAULTS TO TODAY: an inventory browser opening on days of already-spent
  // slots reads as stale data, so past slots are hidden by default — but by a
  // VISIBLE filter value, not hidden server-side magic. The operator sees the
  // date in the field and can clear it or pick an earlier one to see history.
  const [dateFrom, setDateFrom] = useState(todayLocal)
  const [dateTo, setDateTo] = useState('')

  // Pickers populated from the catalogue rather than typed as raw ids: nine
  // models and six branches are a dropdown, and making an operator remember
  // that Deira is showroom 3 is how wrong filters happen.
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

  const filters = useMemo(
    () => ({
      status,
      location_type: locationType,
      vehicle_id: vehicleId,
      showroom_id: showroomId,
      date_from: dateFrom,
      date_to: dateTo,
    }),
    [status, locationType, vehicleId, showroomId, dateFrom, dateTo],
  )

  const list = useList<SlotRow>(fetchSlots, filters, PAGE)

  const columns: GridColDef<SlotRow>[] = [
    { field: 'slot_id', headerName: 'ID', width: 80, type: 'number' },
    {
      field: 'start',
      headerName: 'Date',
      width: 150,
      valueGetter: (_v, row) => fmtDate(row.start) ?? '',
    },
    {
      field: 'time',
      headerName: 'Time',
      width: 150,
      sortable: false,
      valueGetter: (_v, row) => `${fmtTime(row.start) ?? ''} – ${fmtTime(row.end) ?? ''}`,
    },
    {
      field: 'status',
      headerName: 'Status',
      width: 120,
      renderCell: (p) => (
        <StatusChip value={p.row.status} label={slotStatusLabel(p.row.status)} />
      ),
    },
    { field: 'vehicle_name', headerName: 'Vehicle', width: 150 },
    // Inventory is per demo car: a branch's nine models are fourteen cars.
    // Without this column the multi-trim models look like duplicate rows at
    // the same time on the same day.
    { field: 'variant_name', headerName: 'Trim', width: 120 },
    {
      field: 'location_type',
      headerName: 'Type',
      width: 110,
      valueGetter: (_v, row) => locationLabel(row.location_type) ?? '',
    },
    {
      field: 'where',
      headerName: 'Where',
      width: 180,
      sortable: false,
      // A slot is EITHER at a showroom OR a doorstep service area, never both;
      // one column reflects that rather than two mostly-empty ones.
      valueGetter: (_v, row) => row.showroom_name ?? row.service_area ?? '',
    },
    {
      field: 'locked_by_session',
      headerName: 'Held by',
      width: 130,
      renderCell: (p) =>
        p.row.locked_by_session ? (
          <Tooltip title={p.row.locked_by_session}>
            <Box component="span" sx={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 12 }}>
              {shortSession(p.row.locked_by_session)}
            </Box>
          </Tooltip>
        ) : (
          <Box component="span" sx={{ color: 'text.disabled' }}>—</Box>
        ),
    },
    {
      field: 'licence_ack',
      headerName: 'Licence ack',
      width: 120,
      type: 'boolean',
    },
  ]

  return (
    <Box>
      <TopBar
        title="Slots"
        subtitle="Rolling inventory window. Calendar rules are enforced server-side."
        countLabel={`Total Slots: ${list.total.toLocaleString('en-US')}`}
      />

      <Card sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap' }}>
          <TextField select label="Status" value={status} onChange={(e) => setStatus(e.target.value)} sx={{ minWidth: 130 }}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="open">Available</MenuItem>
            <MenuItem value="locked">Locked</MenuItem>
            <MenuItem value="booked">Booked</MenuItem>
          </TextField>
          <TextField select label="Type" value={locationType} onChange={(e) => setLocationType(e.target.value)} sx={{ minWidth: 130 }}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="showroom">Showroom</MenuItem>
            <MenuItem value="doorstep">Doorstep</MenuItem>
          </TextField>
          <TextField select label="Vehicle" value={vehicleId} onChange={(e) => setVehicleId(e.target.value)} sx={{ minWidth: 170 }}>
            <MenuItem value="">All</MenuItem>
            {vehicles.map((v) => (
              <MenuItem key={v.vehicle_id} value={String(v.vehicle_id)}>
                {v.name}
              </MenuItem>
            ))}
          </TextField>
          <TextField select label="Showroom" value={showroomId} onChange={(e) => setShowroomId(e.target.value)} sx={{ minWidth: 190 }}>
            <MenuItem value="">All</MenuItem>
            {showrooms.map((s) => (
              <MenuItem key={s.showroom_id} value={String(s.showroom_id)}>
                {s.name}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            type="date"
            label="From"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
            slotProps={{ inputLabel: { shrink: true } }}
          />
          <TextField
            type="date"
            label="To"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
            slotProps={{ inputLabel: { shrink: true } }}
          />
        </Box>
      </Card>

      {list.error ? (
        <ErrorState message={list.error} onRetry={list.reload} />
      ) : (
        <Card>
          <DataGrid
            rows={list.rows}
            columns={columns}
            getRowId={(r) => r.slot_id}
            loading={list.loading}
            // Server-side: the grid renders the page it was handed and reports
            // the true total, rather than pretending to hold 3,800 rows.
            paginationMode="server"
            rowCount={list.total}
            paginationModel={{ page: Math.floor(list.offset / PAGE), pageSize: PAGE }}
            onPaginationModelChange={(m) => list.setOffset(m.page * PAGE)}
            pageSizeOptions={[PAGE]}
            disableRowSelectionOnClick
            density="compact"
            showToolbar
            sx={{
              border: 'none',
              '& .MuiDataGrid-columnHeaders': { bgcolor: '#fafbfc' },
              '& .MuiDataGrid-cell:focus, & .MuiDataGrid-cell:focus-within': { outline: 'none' },
            }}
            slotProps={{
              loadingOverlay: { variant: 'skeleton', noRowsVariant: 'skeleton' },
            }}
            localeText={{ noRowsLabel: 'No slots match these filters.' }}
          />
        </Card>
      )}

      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>
        Sorting applies to the current page. Narrow with the filters above to sort
        across the whole window.
      </Typography>
    </Box>
  )
}
