// Vehicles — the catalogue as the agent's tools see it.
//
// CARDS WITH THE REAL IMAGERY, not a row of numbers. The catalogue already
// ships photographs (rule 18 renders them to customers in chat), the backend
// already serves them at /media, and a car is the one entity here that is
// genuinely visual. A grid of them is also how an operator actually recognises
// a model — nobody scans for "JET-X70FL".
//
// PRICE IS THE POINT OF CARE. price_aed_min/max are BOTH nullable and null means
// NOT PUBLISHED — never zero. It must render as a visible absence, because the
// agent is forbidden from stating an unpublished price (FR-CX-07) and an
// operator asking "why won't Aisha quote this car?" reads the answer here.
//
// TRIMS, ADDED AT T0.11, AND WHY THE RANGE ALONE WAS NOT ENOUGH. The catalogue
// is variant-level now: a card reading "AED 169,000 – 240,450" tells an operator
// nothing about WHICH cars sit at either end, and the G700 spans four trims from
// a 5-seat Comfort to a 6-seat Flagship plus one that cannot be sold at all.
// The trim table is where "why won't Aisha quote this car?" is actually answered
// — the Top Fire renders as COMING SOON with no price, which is a different
// state from sold out and from a data-entry mistake, and the operator needs to
// tell those apart at a glance.
//
// ORDERED BY PRICE, NEVER BY NAME. The T1's Luxury is its four-wheel-drive
// range-topper and costs more than its Flagship, so a name sort would draw a
// ladder that does not exist. The backend sorts; this only renders.

import { useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import CardContent from '@mui/material/CardContent'
import Typography from '@mui/material/Typography'
import Chip from '@mui/material/Chip'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'

import LocalGasStationRoundedIcon from '@mui/icons-material/LocalGasStationRounded'
import AirlineSeatReclineNormalRoundedIcon from '@mui/icons-material/AirlineSeatReclineNormalRounded'
import StorefrontRoundedIcon from '@mui/icons-material/StorefrontRounded'
import ImageNotSupportedRoundedIcon from '@mui/icons-material/ImageNotSupportedRounded'
import Divider from '@mui/material/Divider'

import { fetchVehicles, type VehicleRow, type VehicleVariantRow } from '../adminApi'
import { useList } from '../useList'
import {
  Flex,
  AutoGrid,
  CardSkeletons,
  EmptyState,
  ErrorState,
  RailCard,
  TopBar,
} from '../components/ui'
import { fuelLabel, priceLabel, seatsLabel } from '../labels'
import Pagination from '@mui/material/Pagination'

function TrimRow({ t, modelName }: { t: VehicleVariantRow; modelName: string | null }) {
  const price = priceLabel(t.price_aed, t.price_aed)
  const seats = seatsLabel(t.seats_min, t.seats_max)

  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        gap: 1,
        py: 0.5,
        opacity: t.coming_soon ? 0.75 : 1,
      }}
    >
      <Box sx={{ minWidth: 0 }}>
        <Typography variant="body2" sx={{ fontSize: 13, fontWeight: 500 }}>
          {/* A null name means "the only version", not "unknown" — show the
              model rather than the word None. */}
          {t.name ?? modelName ?? '—'}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ fontSize: 11 }}>
          {[seats, t.km_per_l_max ? `${t.km_per_l_min}–${t.km_per_l_max} km/L` : null]
            .filter(Boolean)
            .join(' · ') || '\u00a0'}
        </Typography>
      </Box>

      <Box sx={{ textAlign: 'right', flexShrink: 0 }}>
        {t.coming_soon ? (
          // NOT "sold out" and NOT a missing figure. This chip is the answer to
          // "why won't Aisha quote this car?".
          <Chip size="small" color="warning" variant="outlined" label="Coming soon"
                sx={{ height: 20, fontSize: 10.5 }} />
        ) : price ? (
          <>
            <Typography sx={{ fontFamily: "'Space Grotesk'", fontWeight: 600, fontSize: 13.5 }}>
              {price}
            </Typography>
            {t.on_road_aed != null && (
              <Typography variant="caption" color="text.secondary" sx={{ fontSize: 10.5 }}>
                {/* Price + insurance + registration, or nothing. A partial sum
                    would land below the list price and read as a discount. */}
                on-road {t.on_road_aed.toLocaleString('en-US')}
              </Typography>
            )}
          </>
        ) : (
          <Typography variant="caption" color="error.main" sx={{ fontSize: 11 }}>
            No price
          </Typography>
        )}
      </Box>
    </Box>
  )
}

function VehicleCard({ v }: { v: VehicleRow }) {
  const price = priceLabel(v.price_aed_min, v.price_aed_max)
  const seats = seatsLabel(v.seats_min, v.seats_max)

  return (
    <RailCard tone={v.fuel_type === 'phev' ? 'success' : 'default'}>
      <Box
        sx={{
          aspectRatio: '16/9',
          background: 'linear-gradient(135deg,#eef1f6,#e3e8f0)',
          display: 'grid',
          placeItems: 'center',
          overflow: 'hidden',
        }}
      >
        {v.image_url ? (
          // A PLAIN <img>, not the customer side's <picture> (VehiclePhoto).
          // The stored URL is AVIF and the WebP sibling beside it exists for
          // pre-16.4 Safari — a browser the staff console does not have to
          // serve, and wrapping this in a <picture> means re-doing the sx
          // layout below for a case that cannot arise here. Deliberate.
          <Box
            component="img"
            src={v.image_url}
            alt={v.name ?? ''}
            loading="lazy"
            sx={{ width: '100%', height: '100%', objectFit: 'cover' }}
          />
        ) : (
          // No image is a real state — render the absence, never a broken <img>.
          <Flex align="center" spacing={0.5} sx={{ color: 'text.disabled' }}>
            <ImageNotSupportedRoundedIcon />
            <Typography variant="caption">No image</Typography>
          </Flex>
        )}
      </Box>

      <CardContent sx={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="h6" sx={{ fontSize: 16 }}>
              {v.name}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ fontSize: 12.5 }}>
              {v.body_type}
            </Typography>
          </Box>
          <Tooltip title="Model code">
            <Chip
              size="small"
              variant="outlined"
              label={v.model_code}
              sx={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 11 }}
            />
          </Tooltip>
        </Box>

        <Flex direction="row" spacing={1} sx={{ mt: 1.5, flexWrap: 'wrap', gap: 1 }}>
          {v.fuel_type && (
            <Chip
              size="small"
              icon={<LocalGasStationRoundedIcon />}
              label={fuelLabel(v.fuel_type)}
              color={v.fuel_type === 'phev' ? 'success' : 'default'}
              variant={v.fuel_type === 'phev' ? 'filled' : 'outlined'}
            />
          )}
          {seats && (
            <Chip
              size="small"
              variant="outlined"
              icon={<AirlineSeatReclineNormalRoundedIcon />}
              label={seats}
            />
          )}
          <Chip
            size="small"
            variant="outlined"
            icon={<StorefrontRoundedIcon />}
            label={`${v.showroom_ids.length} showrooms`}
          />
        </Flex>

        <Box sx={{ mt: 'auto', pt: 2 }}>
          {price ? (
            <Typography sx={{ fontFamily: "'Space Grotesk'", fontWeight: 600, fontSize: 16 }}>
              {price}
            </Typography>
          ) : (
            <Typography variant="body2" color="text.disabled">
              Price not published
            </Typography>
          )}

          {!!v.variants.length && (
            <>
              <Divider sx={{ my: 1.5 }} />
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{ fontSize: 10.5, letterSpacing: 0.6, textTransform: 'uppercase' }}
              >
                {v.variants.length === 1 ? 'Trim' : `${v.variants.length} trims`}
              </Typography>
              <Box sx={{ mt: 0.5 }}>
                {v.variants.map((t) => (
                  <TrimRow key={t.variant_id} t={t} modelName={v.name} />
                ))}
              </Box>
            </>
          )}

          {!!v.interior_colours.length && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: 'block', mt: 1.5, fontSize: 10.5 }}
            >
              {/* A model-level palette, NOT paired with the exterior colour and
                  not stocked. Labelled "Interiors", never "available in". */}
              Interiors: {v.interior_colours.join(', ')}
            </Typography>
          )}
        </Box>
      </CardContent>
    </RailCard>
  )
}

export default function Vehicles() {
  const [fuel, setFuel] = useState('')
  const filters = useMemo(() => ({ fuel }), [fuel])
  const list = useList<VehicleRow>(fetchVehicles, filters, 12)

  const pageCount = Math.ceil(list.total / list.limit)

  return (
    <Box>
      <TopBar
        title="Vehicles"
        subtitle="The catalogue the agent searches. Images are the same assets shown in chat."
        countLabel={`Total Vehicles: ${list.total.toLocaleString('en-US')}`}
      />

      <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', mb: 3 }}>
        <TextField
          select
          label="Fuel"
          value={fuel}
          onChange={(e) => setFuel(e.target.value)}
          sx={{ minWidth: 160 }}
        >
          <MenuItem value="">All</MenuItem>
          <MenuItem value="petrol">Petrol</MenuItem>
          <MenuItem value="phev">Plug-in hybrid</MenuItem>
        </TextField>
      </Box>

      {list.error && <ErrorState message={list.error} onRetry={list.reload} />}
      {list.loading && !list.rows.length && <CardSkeletons count={6} height={320} />}
      {!list.loading && !list.error && !list.rows.length && (
        <EmptyState message="No vehicles match this filter." />
      )}

      {!!list.rows.length && (
        <AutoGrid min="19rem">
          {list.rows.map((v) => (
            <VehicleCard key={v.vehicle_id} v={v} />
          ))}
        </AutoGrid>
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
