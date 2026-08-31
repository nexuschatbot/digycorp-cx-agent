// Routing — what Aisha understood and who answered it (T8.2).
//
// EVERY FIGURE HERE WAS ALREADY BEING RECORDED AND NONE OF IT WAS VISIBLE.
// turn_trace has carried the routing decision since the router shipped; Phase 3
// added the per-step record (which agent, chosen by which layer, at what margin,
// how long, whether it worked) and Phase 2 added which prompt version answered.
// This screen is a read. Nothing new is computed and nothing new is stored.
//
// THE NUMBER THIS SCREEN EXISTS FOR is the fast-lane share. MIN_MARGIN is 0.02
// because 32 SYNTHETIC card utterances said so, twelve of them at the boundary.
// The calibration predicted the cheap embedding path would settle about a third
// of briefs. This is the only place that says what it settles on REAL ones — and
// if it is far lower, the honest move is to delete the fast lane and always ask
// the judge.
//
// AN EMPTY `steps` LIST MEANS "SINGLE-AGENT TURN", NOT "FAILURE". Until
// MULTI_AGENT_ENABLED is flipped that is every turn, so the table says so
// rather than rendering a row of blanks that reads like a bug.

import { useEffect, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import FormControlLabel from '@mui/material/FormControlLabel'
import Pagination from '@mui/material/Pagination'
import Switch from '@mui/material/Switch'
import Typography from '@mui/material/Typography'

import { fetchRouting, type RoutingRow, type RoutingStep, type RoutingSummary } from '../adminApi'
import { useList } from '../useList'
import { CardSkeletons, EmptyState, ErrorState, RailCard, TopBar } from '../components/ui'

/** How an agent was picked. The distinction is the whole point of the screen:
 *  `fast_lane` cost nothing and took no time; `judge` cost a Haiku call. */
function layerChip(layer: string | null) {
  if (layer === 'fast_lane') return { label: 'fast lane', color: 'success' as const }
  if (layer === 'judge') return { label: 'judged', color: 'info' as const }
  return { label: layer ?? 'unrouted', color: 'default' as const }
}

function Summary() {
  const [data, setData] = useState<RoutingSummary | null>(null)
  useEffect(() => {
    fetch('/api/console/routing/summary')
      .then((r) => r.json())
      .then(setData)
      .catch(() => setData(null))
  }, [])

  if (!data) return null
  if (!data.steps) {
    return (
      <RailCard tone="default">
        <Box sx={{ p: 2 }}>
          <Typography variant="body2" color="text.secondary">
            No multi-agent turns recorded yet. The cascade is behind
            MULTI_AGENT_ENABLED and every turn so far ran single-agent.
          </Typography>
        </Box>
      </RailCard>
    )
  }
  const share = data.fast_lane_share == null ? '—' : `${Math.round(data.fast_lane_share * 100)}%`
  return (
    <RailCard tone="info">
      <Box sx={{ p: 2, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
        {[
          ['Turns', String(data.turns)],
          ['Steps', String(data.steps)],
          // The one that decides whether the fast lane keeps its place.
          ['Settled without a model call', share],
          ['Failed steps', String(data.failed)],
        ].map(([label, value]) => (
          <Box key={label}>
            <Typography variant="caption" color="text.secondary"
                        sx={{ textTransform: 'uppercase', letterSpacing: 0.6, fontSize: 10.5 }}>
              {label}
            </Typography>
            <Typography sx={{ fontFamily: "'Space Grotesk'", fontWeight: 600, fontSize: 20 }}>
              {value}
            </Typography>
          </Box>
        ))}
      </Box>
    </RailCard>
  )
}

function StepChips({ steps }: { steps: RoutingStep[] }) {
  if (!steps.length) {
    return (
      <Typography variant="caption" color="text.disabled">
        single-agent turn
      </Typography>
    )
  }
  return (
    <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap' }}>
      {steps.map((s, i) => {
        const chip = layerChip(s.layer)
        const failed = s.status !== 'ok'
        return (
          <Chip
            key={i}
            size="small"
            variant={failed ? 'filled' : 'outlined'}
            color={failed ? 'error' : chip.color}
            label={
              // The margin is shown because it is what a threshold gets tuned
              // against. A step routed at 0.021 is one bad brief away from
              // needing the judge; a step at 0.09 is not.
              `${s.agent ?? 'unrouted'} · ${chip.label}` +
              (s.margin != null ? ` · ${s.margin.toFixed(3)}` : '') +
              (failed ? ` · ${s.status}` : '')
            }
            sx={{ height: 22, fontSize: 11 }}
          />
        )
      })}
    </Box>
  )
}

/** Turns grouped into the conversation they belong to.
 *
 * ONE CARD PER TURN READS AS A LIST OF STRANGERS. Routing is only legible in
 * sequence: a `sticky` decision means nothing without the `classifier` one
 * before it, and a slow turn is judged against the other turns of the same
 * chat, not against a different customer's. The session id was already on every
 * card — as ten grey characters at the bottom, which is not a grouping.
 *
 * INSERTION ORDER IS PRESERVED, so conversations stay in the newest-first order
 * the API returned; the turns INSIDE each one are reversed to read oldest-first,
 * the way the conversation actually happened.
 *
 * A CONVERSATION CAN STILL BE SPLIT ACROSS PAGES. Paging is server-side over
 * turns, not sessions, so the last group on a page may continue on the next.
 * The turn count in each header says what the group holds HERE, which is why it
 * is labelled per-page rather than as the conversation's length.
 */
function groupBySession(rows: RoutingRow[]): { session: string; turns: RoutingRow[] }[] {
  const order: string[] = []
  const bySession = new Map<string, RoutingRow[]>()
  for (const row of rows) {
    if (!bySession.has(row.session)) {
      bySession.set(row.session, [])
      order.push(row.session)
    }
    bySession.get(row.session)!.push(row)
  }
  return order.map((session) => ({
    session,
    turns: [...(bySession.get(session) ?? [])].reverse(),
  }))
}

function ConversationHeader({ session, turns }: { session: string; turns: RoutingRow[] }) {
  const totalMs = turns.reduce((sum, t) => sum + (t.latency_ms ?? 0), 0)
  const failed = turns.reduce((sum, t) => sum + (t.failed_steps ?? 0), 0)
  // Delegated turns are what this screen exists for; a conversation of nothing
  // but single-agent turns should say so at a glance.
  const delegated = turns.filter((t) => t.steps.length > 0).length

  return (
    <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1.5, flexWrap: 'wrap', px: 0.5, pb: 1 }}>
      <Typography variant="caption" sx={{ fontFamily: 'ui-monospace, Menlo, monospace', fontWeight: 600 }}>
        {session.slice(0, 12)}…
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {turns.length} turn{turns.length === 1 ? '' : 's'} on this page
      </Typography>
      {delegated > 0 && (
        <Typography variant="caption" color="text.secondary">
          · {delegated} delegated
        </Typography>
      )}
      {totalMs > 0 && (
        <Typography variant="caption" color="text.secondary">
          · {(totalMs / 1000).toFixed(1)}s total
        </Typography>
      )}
      {failed > 0 && (
        <Typography variant="caption" color="error.main">
          · {failed} failed step{failed === 1 ? '' : 's'}
        </Typography>
      )}
    </Box>
  )
}

export default function Routing() {
  const [onlyMulti, setOnlyMulti] = useState(false)
  const filters = useMemo(
    () => ({ only_multi_agent: onlyMulti ? 'true' : '' }),
    [onlyMulti],
  )
  const list = useList<RoutingRow>(fetchRouting, filters, 25)
  const pageCount = Math.ceil(list.total / list.limit)
  const groups = useMemo(() => groupBySession(list.rows), [list.rows])

  return (
    <Box>
      <TopBar
        title="Routing"
        subtitle="What each turn was understood to be, who answered it, and how the agent was chosen."
        countLabel={`Turns: ${list.total.toLocaleString('en-US')}`}
      />

      <Box sx={{ mb: 3 }}>
        <Summary />
      </Box>

      <FormControlLabel
        sx={{ mb: 2 }}
        control={
          <Switch checked={onlyMulti} onChange={(e) => setOnlyMulti(e.target.checked)} />
        }
        label="Only multi-agent turns"
      />

      {list.error && <ErrorState message={list.error} onRetry={list.reload} />}
      {list.loading && !list.rows.length && <CardSkeletons count={4} height={90} />}
      {!list.loading && !list.error && !list.rows.length && (
        <EmptyState message="No turns recorded for this filter." />
      )}

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
        {groups.map((group) => (
          <Box
            key={group.session}
            sx={{
              border: '1px solid', borderColor: 'divider', borderRadius: 2,
              p: 1.5, bgcolor: 'action.hover',
            }}
          >
            <ConversationHeader session={group.session} turns={group.turns} />
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        {group.turns.map((t) => (
          <RailCard key={t.trace_id} tone={t.failed_steps ? 'error' : 'default'}>
            <Box sx={{ p: 2 }}>
              <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
                <Typography variant="body2" sx={{ fontWeight: 500, minWidth: 0 }}>
                  {t.user_message ?? <em>(no message)</em>}
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: 'nowrap' }}>
                  {t.latency_ms != null ? `${t.latency_ms} ms` : '—'}
                </Typography>
              </Box>

              <Box sx={{ mt: 1.25 }}>
                <StepChips steps={t.steps} />
              </Box>

              <Box sx={{ mt: 1.25, display: 'flex', gap: 1.5, flexWrap: 'wrap' }}>
                {t.routed_to && (
                  <Typography variant="caption" color="text.secondary">
                    router: {t.routed_to}
                    {t.router_latency_ms != null ? ` (${t.router_latency_ms} ms)` : ''}
                  </Typography>
                )}
                {t.prompt_versions && (
                  // Prompts are editable without a deploy, so this is what pins
                  // a quality change to a prompt change.
                  <Typography variant="caption" color="text.secondary">
                    prompts:{' '}
                    {Object.entries(t.prompt_versions)
                      .map(([a, v]) => `${a}=${v}`)
                      .join(' ')}
                  </Typography>
                )}
              </Box>
            </Box>
          </RailCard>
        ))}
            </Box>
          </Box>
        ))}
      </Box>

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
