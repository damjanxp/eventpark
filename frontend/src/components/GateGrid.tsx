import { AlertTriangle, ArrowDownToLine, ArrowUpFromLine, Printer, Wifi, WifiOff } from 'lucide-react'
import type { ReactNode } from 'react'
import type { GateActivity } from '@/api/types'
import { cn } from '@/lib/cn'
import { ago } from '@/lib/format'
import { Badge } from './ui/badge'

/** One card per gate: online status (icon + label, never color alone), last heartbeat, last-15-min activity. */
export function GateGrid({ gates, action }: { gates: GateActivity[]; action?: (g: GateActivity) => ReactNode }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(13rem,1fr))] gap-3">
      {gates.map((g) => {
        const Dir = g.direction === 'ENTRY' ? ArrowDownToLine : ArrowUpFromLine
        const printerProblem = g.printer_status === 'PAPER_LOW' || g.printer_status === 'PAPER_OUT'
        return (
          <div key={g.gate_id} className={cn('rounded-lg border border-line bg-surface p-3', !g.online && 'border-dashed')}>
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 font-semibold">
                <Dir className="size-4 text-muted" aria-label={g.direction.toLowerCase()} /> {g.code}
              </span>
              {g.device_status === 'DISABLED' ? (
                <Badge>disabled</Badge>
              ) : g.online ? (
                <Badge tone="ok">
                  <Wifi className="size-3" aria-hidden /> online
                </Badge>
              ) : (
                <Badge tone="bad">
                  <WifiOff className="size-3" aria-hidden /> offline
                </Badge>
              )}
            </div>
            <p className="mt-1 truncate text-xs text-muted">{g.name} · heartbeat {ago(g.last_heartbeat_at)}</p>
            <dl className="tabular mt-3 grid grid-cols-3 gap-1 text-center">
              <Stat label="passed" value={g.passages_15m} />
              <Stat label="rejected" value={g.rejections_15m} />
              <Stat label="decision" value={g.avg_decision_ms_15m == null ? '-' : `${Math.round(g.avg_decision_ms_15m)}ms`} />
            </dl>
            {Object.keys(g.rejections_by_reason).length > 0 && (
              <p className="mt-2 text-xs text-muted">
                {Object.entries(g.rejections_by_reason)
                  .map(([r, n]) => `${r.replaceAll('_', ' ').toLowerCase()} ${n}`)
                  .join(' · ')}
              </p>
            )}
            {printerProblem && (
              <p className="mt-2 flex items-center gap-1 text-xs font-medium text-bad">
                <Printer className="size-3.5" aria-hidden />
                <AlertTriangle className="size-3.5" aria-hidden /> {g.printer_status === 'PAPER_OUT' ? 'Paper out' : 'Paper low'}
              </p>
            )}
            {action && <div className="mt-3">{action(g)}</div>}
          </div>
        )
      })}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded bg-raised px-1 py-1.5">
      <dd className="text-sm font-semibold">{value}</dd>
      <dt className="text-[11px] text-muted">{label}</dt>
    </div>
  )
}
