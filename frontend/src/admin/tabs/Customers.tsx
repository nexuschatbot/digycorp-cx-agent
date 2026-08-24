// Customers — the identity and consent picture.
//
// ⚠️ PII, UNGATED. Names, +971 mobiles and emails for every customer at once.
// See the Bookings header and the D1 PDPL checklist; same decision, same fix.
//
// CONSENT IS THE POINT OF THIS TAB, not contact details. The question it answers
// on demo morning is "why did Aisha remember nothing about this person?" — and
// the answer is always one of these chips. Memory consent gates recall on READ
// as well as write, so a customer with a stored profile but withdrawn consent is
// correctly invisible to the agent, and that combination is only legible if both
// facts sit together.
//
// WHETHER A PROFILE EXISTS, NEVER ITS CONTENTS. `notes` is deliberately not
// exposed by the API: gating the writers but printing the row in a console would
// be a second reader that forgot the gate.

import Box from '@mui/material/Box'
import CardContent from '@mui/material/CardContent'
import Typography from '@mui/material/Typography'
import Chip from '@mui/material/Chip'
import Divider from '@mui/material/Divider'
import Avatar from '@mui/material/Avatar'
import Tooltip from '@mui/material/Tooltip'

import CheckCircleRoundedIcon from '@mui/icons-material/CheckCircleRounded'
import DoNotDisturbOnRoundedIcon from '@mui/icons-material/DoNotDisturbOnRounded'
import PsychologyRoundedIcon from '@mui/icons-material/PsychologyRounded'
import ReplayRoundedIcon from '@mui/icons-material/ReplayRounded'
import EmailRoundedIcon from '@mui/icons-material/EmailRounded'
import PhoneRoundedIcon from '@mui/icons-material/PhoneRounded'

import { fetchCustomers, type CustomerRow } from '../adminApi'
import { useList } from '../useList'
import {
  Flex,
  AutoGrid,
  CardSkeletons,
  EmptyState,
  ErrorState,
  Field,
  FieldGrid,
  PageHeader,
  RailCard,
} from '../components/ui'
import { fmtDateTime } from '../format'
import { emirateLabel, langLabel } from '../labels'

function initials(name: string | null): string {
  if (!name) return '?'
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? '')
    .join('')
}

/** Consent as a dated chip. The timestamp matters: consent is a PDPL record,
 *  and "yes" without when it was given is not one. */
function ConsentChip({
  granted,
  at,
  label,
}: {
  granted: boolean
  at: string | null
  label: string
}) {
  const when = fmtDateTime(at)
  return (
    <Tooltip title={granted ? (when ? `Granted ${when}` : 'Granted') : 'Not granted'}>
      <Chip
        size="small"
        icon={granted ? <CheckCircleRoundedIcon /> : <DoNotDisturbOnRoundedIcon />}
        label={label}
        color={granted ? 'success' : 'default'}
        variant={granted ? 'filled' : 'outlined'}
        sx={{ opacity: granted ? 1 : 0.7 }}
      />
    </Tooltip>
  )
}

function CustomerCard({ c }: { c: CustomerRow }) {
  return (
    <RailCard tone={c.memory_consent ? 'success' : 'default'}>
      <CardContent>
        <Flex direction="row" spacing={1.5} align="flex-start">
          <Avatar
            sx={{ width: 44, height: 44, bgcolor: '#e7edfb', color: 'info.main', fontWeight: 600, fontSize: 15 }}
          >
            {initials(c.name_en)}
          </Avatar>
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Typography variant="h6" sx={{ fontSize: 15.5 }}>
              {c.title ? `${c.title} ` : ''}
              {c.name_en}
            </Typography>
            {c.name_ar && (
              <Typography variant="body2" color="text.secondary" dir="rtl" sx={{ fontSize: 13 }}>
                {c.name_ar}
              </Typography>
            )}
          </Box>
          {c.winback_flag && (
            <Tooltip title="Flagged for win-back after a cancellation (rule 11)">
              <Chip size="small" color="warning" icon={<ReplayRoundedIcon />} label="Win-back" />
            </Tooltip>
          )}
        </Flex>

        <Flex spacing={0.75} sx={{ mt: 2 }}>
          <Flex direction="row" spacing={1} align="center">
            <PhoneRoundedIcon sx={{ fontSize: 15, color: 'text.disabled' }} />
            <Typography variant="body2" sx={{ fontSize: 13 }}>
              {c.mobile}
            </Typography>
          </Flex>
          {c.email && (
            <Flex direction="row" spacing={1} align="center">
              <EmailRoundedIcon sx={{ fontSize: 15, color: 'text.disabled' }} />
              <Typography variant="body2" sx={{ fontSize: 13, overflowWrap: 'anywhere' }}>
                {c.email}
              </Typography>
            </Flex>
          )}
        </Flex>

        <Divider sx={{ my: 2 }} />

        <FieldGrid min="7rem">
          <Field label="Emirate" value={emirateLabel(c.emirate)} />
          <Field label="Language" value={langLabel(c.language_pref)} />
          <Field label="Bookings" value={c.bookings} />
        </FieldGrid>

        <Flex direction="row" spacing={1} sx={{ mt: 2, flexWrap: 'wrap', gap: 1 }}>
          <ConsentChip
            granted={c.memory_consent}
            at={c.memory_consent_at}
            label="Memory"
          />
          <ConsentChip
            granted={c.whatsapp_consent}
            at={c.whatsapp_consent_at}
            label="WhatsApp"
          />
          {/* WHETHER a profile exists — never its contents. */}
          {c.has_memory_profile && (
            <Tooltip title="A memory profile row exists. Its contents are consent-gated and deliberately not exposed to this console.">
              <Chip size="small" variant="outlined" icon={<PsychologyRoundedIcon />} label="Profile stored" />
            </Tooltip>
          )}
        </Flex>
      </CardContent>
    </RailCard>
  )
}

export default function Customers() {
  const list = useList<CustomerRow>(fetchCustomers, {}, 12)

  return (
    <Box>
      <PageHeader
        title="Customers"
        subtitle="Identity and consent. Recall is gated on consent for reads as well as writes."
        count={list.total}
      />

      {list.error && <ErrorState message={list.error} onRetry={list.reload} />}
      {list.loading && !list.rows.length && <CardSkeletons count={4} height={300} />}
      {!list.loading && !list.error && !list.rows.length && (
        <EmptyState message="No customers." />
      )}

      {!!list.rows.length && (
        <AutoGrid min="20rem">
          {list.rows.map((c) => (
            <CustomerCard key={c.customer_id} c={c} />
          ))}
        </AutoGrid>
      )}
    </Box>
  )
}
