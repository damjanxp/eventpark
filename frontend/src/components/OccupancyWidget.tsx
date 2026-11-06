/**
 * Occupancy per zone, live (learning path Phase 8). One bar per zone, split into its two pools:
 *   [ drive-up occupied | drive-up free ][ pre-booked inside | pre-booked held, not arrived | quota free ]
 * Drive-up and pre-booked are separate pools (docs/01 §7): a full drive-up pool can't borrow pre-booked places.
 * The numbers come from the read model (eventually consistent: may trail the database by a moment).
 */
import type { ZoneOccupancy } from '@/api/types'
import { cn } from '@/lib/cn'

function pct(part: number, whole: number) {
  return whole > 0 ? `${(100 * part) / whole}%` : '0%'
}

export function OccupancyBar({ code, z }: { code: string; z: ZoneOccupancy }) {
  const total = z.capacity || 1
  const driveUpRatio = z.drive_up_capacity > 0 ? z.drive_up_occupied / z.drive_up_capacity : 0
  const tone = driveUpRatio >= 1 ? 'text-bad' : driveUpRatio >= 0.9 ? 'text-warn' : 'text-muted'
  const heldNotInside = Math.max(0, z.prebook_held - z.prebook_inside)
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <span className="font-medium">Zone {code}</span>
        <span className="tabular text-sm">
          <strong className="text-base">{z.inside}</strong>
          <span className="text-muted"> / {z.capacity} inside</span>
          {z.entering > 0 && <span className="ml-2 text-xs text-muted">+{z.entering} entering</span>}
        </span>
      </div>
      <div
        className="flex h-4 overflow-hidden rounded bg-raised"
        role="img"
        aria-label={`Zone ${code}: drive-up ${z.drive_up_occupied} of ${z.drive_up_capacity}, pre-booked ${z.prebook_inside} inside and ${heldNotInside} expected of ${z.prebook_quota}`}
      >
        <div className="bg-brand" style={{ width: pct(z.drive_up_occupied, total) }} />
        <div className="border-r-2 border-surface" style={{ width: pct(z.drive_up_capacity - z.drive_up_occupied, total) }} />
        <div className="bg-prebook" style={{ width: pct(z.prebook_inside, total) }} />
        <div className="bg-prebook/35" style={{ width: pct(heldNotInside, total) }} />
      </div>
      <div className="mt-1.5 flex flex-wrap justify-between gap-x-4 text-xs">
        <span className={cn('tabular', tone)}>
          Drive-up {z.drive_up_occupied}/{z.drive_up_capacity} · {z.available_drive_up} free
        </span>
        {z.prebook_quota > 0 && (
          <span className="tabular text-muted">
            Pre-booked {z.prebook_inside} in · {heldNotInside} expected · {z.available_prebook} unsold
          </span>
        )}
      </div>
    </div>
  )
}

export function OccupancyWidget({ zones }: { zones: Record<string, ZoneOccupancy> }) {
  const entries = Object.entries(zones).sort(([a], [b]) => a.localeCompare(b))
  if (entries.length === 0) return <p className="text-sm text-muted">No zones configured for this event.</p>
  return (
    <div className="space-y-5">
      {entries.map(([code, z]) => (
        <OccupancyBar key={code} code={code} z={z} />
      ))}
      <div className="flex flex-wrap gap-4 text-xs text-muted">
        <Legend className="bg-brand" label="Drive-up" />
        <Legend className="bg-prebook" label="Pre-booked inside" />
        <Legend className="bg-prebook/35" label="Pre-booked, not arrived" />
      </div>
    </div>
  )
}

function Legend({ className, label }: { className: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={cn('size-2.5 rounded-sm', className)} aria-hidden /> {label}
    </span>
  )
}
