import type { EventOut } from '@/api/types'
import { Select } from './ui/input'

export function EventPicker({
  events,
  value,
  onChange,
}: {
  events: EventOut[]
  value: string | undefined
  onChange: (id: string) => void
}) {
  return (
    <Select aria-label="Event" value={value ?? ''} onChange={(e) => onChange(e.target.value)} className="w-auto min-w-64">
      {events.map((e) => (
        <option key={e.id} value={e.id}>
          {e.name} · {e.status.toLowerCase()}
        </option>
      ))}
    </Select>
  )
}

export function StatusDot({ status }: { status: 'connecting' | 'live' | 'polling' }) {
  const label = { live: 'Live', polling: 'Polling every 5 s', connecting: 'Connecting…' }[status]
  const color = { live: 'bg-ok', polling: 'bg-warn', connecting: 'bg-muted' }[status]
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted" title="WebSocket status" aria-live="polite">
      <span className={`size-2 rounded-full ${color} ${status === 'live' ? 'animate-pulse' : ''}`} aria-hidden />
      {label}
    </span>
  )
}
