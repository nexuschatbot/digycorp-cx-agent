// Two Jetours side by side, in the thread.
//
// FACTS, NOT A VERDICT. There is no winner badge, no score and no "recommended"
// ribbon — deliberately. A comparison that always crowns the dearer car is not a
// comparison, and a customer reads that in one glance. The rows that DIFFER are
// marked so the eye lands on what actually separates them; Aisha says which of
// those matters in her own words, and only if she knows enough about the person
// to say it honestly.
//
// TWO COLUMNS ONLY, because the decision being made is binary and three columns
// on a phone is a horizontal scroll and a squint.
//
// READ-ONLY. Nothing here is tappable: this is something to look at while
// deciding, not another commit. Booking stays a conversation, which is where it
// belongs.

import type { Comparison } from '../compare'
import { VehiclePhoto } from './VehiclePhoto'

export function CompareCard({ data, dir }: { data: Comparison; dir: 'rtl' | 'ltr' }) {
  const { left, right, rows } = data
  return (
    <div className={`cmp cmp--${dir}`} dir={dir}>
      <div className="cmp-heads">
        {[left, right].map((s) => (
          <div className="cmp-head" key={s.vehicleId}>
            {s.imageUrl ? (
              <VehiclePhoto className="cmp-img" src={s.imageUrl} alt="" />
            ) : (
              <div className="cmp-img cmp-img--empty" aria-hidden="true">◆</div>
            )}
            {/* Latin model names inside an Arabic panel keep their own direction. */}
            <bdi className="cmp-name">{s.name}</bdi>
          </div>
        ))}
      </div>

      <table className="cmp-table">
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className={r.differs ? 'cmp-row cmp-row--differs' : 'cmp-row'}>
              <th scope="row" className="cmp-label">{r.label}</th>
              <td className="cmp-val"><bdi>{r.left}</bdi></td>
              <td className="cmp-val"><bdi>{r.right}</bdi></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
