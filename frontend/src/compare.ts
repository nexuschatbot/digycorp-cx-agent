/**
 * compare_vehicles output → a two-column table, or nothing.
 *
 * THE DIFFERING ROWS ARE FLAGGED SERVER-SIDE, not worked out here. "Are these
 * two strings the same?" is trivial until one side says "5" and the other
 * "5 seats" — and a table that highlights a row identical in both columns is
 * worse than one that highlights nothing, because it teaches the reader the
 * marks mean nothing.
 */

export interface CompareSide {
  vehicleId: number
  name: string
  imageUrl: string | null
  features: string[]
}

export interface CompareRow {
  label: string
  left: string
  right: string
  differs: boolean
}

export interface Comparison {
  left: CompareSide
  right: CompareSide
  rows: CompareRow[]
}

function side(o: unknown): CompareSide | null {
  if (!o || typeof o !== 'object') return null
  const s = o as Record<string, unknown>
  if (typeof s.vehicle_id !== 'number' || typeof s.name !== 'string') return null
  return {
    vehicleId: s.vehicle_id,
    name: s.name,
    imageUrl: typeof s.image_url === 'string' ? s.image_url : null,
    features: Array.isArray(s.features) ? (s.features as unknown[]).filter((f): f is string => typeof f === 'string') : [],
  }
}

export function extractComparison(output: unknown): Comparison | null {
  if (!output || typeof output !== 'object') return null
  const o = output as Record<string, unknown>
  if (o.compared !== true || !Array.isArray(o.rows)) return null
  const left = side(o.left)
  const right = side(o.right)
  if (!left || !right) return null
  const rows = (o.rows as unknown[])
    .map((r) => (r && typeof r === 'object' ? (r as Record<string, unknown>) : null))
    .filter((r): r is Record<string, unknown> => !!r)
    .filter((r) => typeof r.label === 'string')
    .map((r) => ({
      label: String(r.label),
      left: String(r.left ?? '—'),
      right: String(r.right ?? '—'),
      differs: r.differs === true,
    }))
  return rows.length ? { left, right, rows } : null
}
