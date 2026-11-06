import { BellRing } from 'lucide-react'
import { useState } from 'react'
import type { ZoneOccupancy } from '@/api/types'
import { EntriesExitsChart, HourlyTable, RevenueChart } from '@/components/Charts'
import { EventPicker, StatusDot } from '@/components/EventPicker'
import { GateGrid } from '@/components/GateGrid'
import { OccupancyWidget } from '@/components/OccupancyWidget'
import { PassageFeed } from '@/components/PassageFeed'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardBody, CardHeader } from '@/components/ui/card'
import { Empty, ErrorMessage, Loading } from '@/components/ui/feedback'
import { Select } from '@/components/ui/input'
import { Table, Td, Th } from '@/components/ui/table'
import { useGateActivity, useHourly, useInside, useOccupancy, usePassages, useVenue } from '@/hooks/useEventData'
import { useEventStream } from '@/hooks/useEventStream'
import { useSelectedEvent } from '@/hooks/useSelectedEvent'
import { dateTime, duration, money, time } from '@/lib/format'

export function Dashboard() {
  const { events, event, select, isLoading, error } = useSelectedEvent()
  const { status, alerts } = useEventStream(event?.id)
  const venue = useVenue(event?.venue_id)
  const tz = venue.data?.timezone
  const occupancy = useOccupancy(event?.id, status)
  const activity = useGateActivity(event?.id)
  const hourly = useHourly(event?.id, status)
  const passages = usePassages(event?.id, status)

  if (isLoading) return <Loading />
  if (error) return <ErrorMessage error={error} />
  if (!event) return <Empty>No events yet. Create one under Setup (or run `make seed`).</Empty>

  const totals = occupancy.data?.totals ?? {}
  const zones = (occupancy.data?.zones ?? {}) as unknown as Record<string, ZoneOccupancy>
  const online = activity.data?.filter((g) => g.online).length ?? 0

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="mr-auto text-xl font-semibold">Dashboard</h1>
        <StatusDot status={status} />
        <EventPicker events={events} value={event.id} onChange={select} />
      </div>
      <p className="-mt-4 text-sm text-muted">
        {venue.data?.name} · gates open {dateTime(event.gates_open_at, tz)} – close {dateTime(event.gates_close_at, tz)}
        {tz && ` (${tz})`}
      </p>

      {alerts.length > 0 && (
        <Card className="border-warn/50">
          <CardBody className="space-y-1 py-3">
            {alerts.slice(0, 5).map((a) => (
              <p key={a.id} className="flex items-center gap-2 text-sm">
                <BellRing className="size-4 text-warn" aria-hidden /> <span className="font-medium">{a.text}</span>
                <span className="text-xs text-muted">{time(new Date(a.at).toISOString(), tz)}</span>
              </p>
            ))}
          </CardBody>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi label="Cars inside" value={totals.inside ?? 0} sub={`of ${totals.capacity ?? 0} places · ${totals.entering ?? 0} entering`} />
        <Kpi label="Drive-up places free" value={totals.available_drive_up ?? 0} sub={`${totals.available_prebook ?? 0} pre-booked unsold`} />
        <Kpi label="Entries / exits" value={`${hourly.data?.total_entries ?? 0} / ${hourly.data?.total_exits ?? 0}`} sub={`${hourly.data?.total_rejections ?? 0} rejections`} />
        <Kpi
          label="Revenue"
          value={money(hourly.data?.total_revenue_minor ?? 0)}
          sub={`${money(hourly.data?.total_released_unpaid_minor ?? 0)} released unpaid`}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-[3fr_2fr]">
        <Card>
          <CardHeader
            title="Occupancy by zone"
            sub={occupancy.data?.source === 'postgres' ? 'from Postgres (read model unavailable)' : 'live read model'}
          />
          <CardBody>{occupancy.isLoading ? <Loading /> : <OccupancyWidget zones={zones} />}</CardBody>
        </Card>
        <Card>
          <CardHeader title="Latest passages" sub="newest first" />
          <CardBody className="max-h-96 overflow-y-auto py-1">
            <PassageFeed items={passages.data?.items ?? []} tz={tz} />
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader title="Gates" sub={`${online} of ${activity.data?.length ?? 0} online · counts over the last 15 minutes`} />
        <CardBody>{activity.data ? <GateGrid gates={activity.data} /> : <Loading />}</CardBody>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Entries and exits per hour" sub={`venue time${tz ? ` (${tz})` : ''}`} />
          <CardBody>
            {hourly.data && hourly.data.hours.length > 0 ? (
              <>
                <EntriesExitsChart stats={hourly.data} tz={tz} />
                <HourlyTable stats={hourly.data} tz={tz} />
              </>
            ) : (
              <Empty>No completed passages yet.</Empty>
            )}
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Revenue per hour" sub="succeeded payments (exit fees, lost tickets)" />
          <CardBody>
            {hourly.data && hourly.data.hours.some((h) => h.revenue_minor > 0) ? (
              <RevenueChart stats={hourly.data} tz={tz} />
            ) : (
              <Empty>No revenue yet.</Empty>
            )}
          </CardBody>
        </Card>
      </div>

      <CarsInside eventId={event.id} zones={Object.keys(zones).sort()} tz={tz} />
    </div>
  )
}

function Kpi({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <Card className="px-4 py-3">
      <p className="text-xs text-muted">{label}</p>
      <p className="tabular mt-1 text-2xl font-semibold">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-muted">{sub}</p>}
    </Card>
  )
}

function CarsInside({ eventId, zones, tz }: { eventId: string; zones: string[]; tz?: string }) {
  const [zone, setZone] = useState('')
  const inside = useInside(eventId, zone)
  const rows = inside.data?.pages.flatMap((p) => p.items) ?? []
  return (
    <Card>
      <CardHeader
        title="Vehicles inside"
        action={
          <Select aria-label="Filter by zone" value={zone} onChange={(e) => setZone(e.target.value)} className="w-32">
            <option value="">All zones</option>
            {zones.map((z) => (
              <option key={z} value={z}>
                Zone {z}
              </option>
            ))}
          </Select>
        }
      />
      <CardBody className="p-0">
        {inside.isLoading ? (
          <Loading />
        ) : rows.length === 0 ? (
          <Empty>Nobody inside.</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Ticket</Th>
                <Th>Type</Th>
                <Th>Zone</Th>
                <Th>Plate</Th>
                <Th>Entered</Th>
                <Th>Inside for</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.ticket_id}>
                  <Td className="font-mono text-xs">{r.code}</Td>
                  <Td>
                    <Badge tone={r.type === 'PREBOOKED' ? 'brand' : 'neutral'}>
                      {r.type === 'PREBOOKED' ? 'pre-booked' : 'drive-up'}
                    </Badge>
                    {r.status === 'EXITING' && <Badge tone="warn" className="ml-1">exiting</Badge>}
                  </Td>
                  <Td>
                    {r.zone}
                    {r.space && ` · ${r.space}`}
                  </Td>
                  <Td>{r.plate ?? '-'}</Td>
                  <Td className="tabular">{time(r.entered_at, tz)}</Td>
                  <Td className="tabular">{duration(r.entered_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        {inside.hasNextPage && (
          <div className="p-3 text-center">
            <Button variant="secondary" size="sm" onClick={() => void inside.fetchNextPage()} disabled={inside.isFetchingNextPage}>
              Load more
            </Button>
          </div>
        )}
      </CardBody>
    </Card>
  )
}
