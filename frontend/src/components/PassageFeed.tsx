import type { PassageRow } from '@/api/types'
import { money, time } from '@/lib/format'
import { Badge } from './ui/badge'
import { Empty } from './ui/feedback'

const tone = {
  COMPLETED: 'ok',
  AUTHORIZED: 'brand',
  PENDING_PAYMENT: 'warn',
  REJECTED: 'bad',
  EXPIRED: 'neutral',
  ABORTED: 'neutral',
} as const

export function PassageFeed({ items, tz }: { items: PassageRow[]; tz?: string }) {
  if (items.length === 0) return <Empty>No passages yet.</Empty>
  return (
    <ul className="divide-y divide-line text-sm" aria-live="polite">
      {items.map((p) => (
        <li key={p.id} className="flex items-center gap-3 py-2">
          <span className="tabular w-20 shrink-0 text-xs text-muted">{time(p.completed_at ?? p.authorized_at, tz)}</span>
          <span className="w-10 shrink-0 font-medium">{p.gate_code}</span>
          <Badge tone={tone[p.status]}>{p.status.replace('_', ' ').toLowerCase()}</Badge>
          <span className="min-w-0 flex-1 truncate text-xs text-muted">
            {p.reject_reason
              ? p.reject_reason.replaceAll('_', ' ').toLowerCase()
              : [p.ticket_type === 'PREBOOKED' ? 'pre-booked' : p.ticket_type ? 'drive-up' : null, p.zone && `zone ${p.zone}`]
                  .filter(Boolean)
                  .join(' · ')}
            {p.manual && ' · manual'}
            {!!p.amount_due_minor && ` · ${money(p.amount_due_minor)}`}
          </span>
        </li>
      ))}
    </ul>
  )
}
