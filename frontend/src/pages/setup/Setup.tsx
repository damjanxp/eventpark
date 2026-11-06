/**
 * Minimal admin setup (ADMIN, EVENT_MANAGER): venues -> zones, gates, devices; events -> pricing, zone capacities,
 * lifecycle; reports and documents. Plain forms on purpose: the interesting parts live in the API.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, KeyRound, Upload } from 'lucide-react'
import { useState, type FormEvent, type ReactNode } from 'react'
import { must, staffApi } from '@/api/client'
import type { EventDetail, Gate, Venue } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardBody, CardHeader } from '@/components/ui/card'
import { Empty, ErrorMessage, Loading } from '@/components/ui/feedback'
import { Field, Input, Select } from '@/components/ui/input'
import { Table, Td, Th } from '@/components/ui/table'
import { cn } from '@/lib/cn'
import { ago, dateTime, money } from '@/lib/format'
import { fromMinor, toMinor } from '@/lib/money'

export function Setup() {
  const [tab, setTab] = useState<'events' | 'venues'>('events')
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <h1 className="mr-auto text-xl font-semibold">Setup</h1>
        {(['events', 'venues'] as const).map((t) => (
          <Button key={t} variant={tab === t ? 'primary' : 'secondary'} size="sm" onClick={() => setTab(t)}>
            {t === 'events' ? 'Events' : 'Venues & gates'}
          </Button>
        ))}
      </div>
      {tab === 'events' ? <EventsSetup /> : <VenuesSetup />}
    </div>
  )
}

function useVenues() {
  return useQuery({
    queryKey: ['venues'],
    queryFn: () => must(staffApi.GET('/admin/v1/venues')),
  })
}

function Pill({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cn('w-full rounded-md px-3 py-2 text-left text-sm hover:bg-raised', active && 'bg-raised font-medium')}
    >
      {children}
    </button>
  )
}

// --- venues, zones, gates, devices -----------------------------------------------------------------------------------

function VenuesSetup() {
  const venues = useVenues()
  const [selected, setSelected] = useState<string | null>(null)
  const venue = venues.data?.find((v) => v.id === selected) ?? venues.data?.[0]
  return (
    <div className="grid gap-6 lg:grid-cols-[16rem_1fr]">
      <Card>
        <CardHeader title="Venues" />
        <CardBody className="space-y-1 p-2">
          {venues.data?.map((v) => (
            <Pill key={v.id} active={v.id === venue?.id} onClick={() => setSelected(v.id)}>
              {v.name}
              <span className="block text-xs text-muted">{v.timezone}</span>
            </Pill>
          ))}
          <NewVenue onCreated={setSelected} />
        </CardBody>
      </Card>
      {venue ? (
        <div className="space-y-6">
          <ZonesCard venue={venue} />
          <GatesCard venue={venue} />
        </div>
      ) : (
        <Empty>Create a venue to start.</Empty>
      )}
    </div>
  )
}

function NewVenue({ onCreated }: { onCreated: (id: string) => void }) {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const [timezone, setTimezone] = useState('Europe/Belgrade')
  const create = useMutation({
    mutationFn: () => must(staffApi.POST('/admin/v1/venues', { body: { name, timezone, address: '' } })),
    onSuccess: (v) => {
      void client.invalidateQueries({ queryKey: ['venues'] })
      setName('')
      onCreated(v.id)
    },
  })
  return (
    <form
      className="space-y-2 border-t border-line p-2 pt-3"
      onSubmit={(e) => {
        e.preventDefault()
        create.mutate()
      }}
    >
      <Input required placeholder="New venue name" value={name} onChange={(e) => setName(e.target.value)} />
      <Input required placeholder="Time zone (IANA)" value={timezone} onChange={(e) => setTimezone(e.target.value)} />
      <ErrorMessage error={create.error} />
      <Button type="submit" size="sm" variant="secondary" className="w-full" disabled={create.isPending}>
        Add venue
      </Button>
    </form>
  )
}

function ZonesCard({ venue }: { venue: Venue }) {
  const client = useQueryClient()
  const zones = useQuery({
    queryKey: ['zones', venue.id],
    queryFn: () => must(staffApi.GET('/admin/v1/venues/{venue_id}/zones', { params: { path: { venue_id: venue.id } } })),
  })
  const [form, setForm] = useState({ code: '', name: '', total_capacity: '100' })
  const create = useMutation({
    mutationFn: () =>
      must(
        staffApi.POST('/admin/v1/venues/{venue_id}/zones', {
          params: { path: { venue_id: venue.id } },
          body: { code: form.code, name: form.name, total_capacity: Number(form.total_capacity) },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['zones', venue.id] })
      setForm({ code: '', name: '', total_capacity: '100' })
    },
  })
  const addSpaces = useMutation({
    mutationFn: ({ zoneId, count, prefix }: { zoneId: string; count: number; prefix: string }) =>
      must(
        staffApi.POST('/admin/v1/zones/{zone_id}/spaces', {
          params: { path: { zone_id: zoneId } },
          body: { count, prefix, kind: 'VIP', start: 1 },
        }),
      ),
  })
  return (
    <Card>
      <CardHeader title={`Zones at ${venue.name}`} sub="physical areas; per-event capacity is set on the event" />
      <CardBody className="space-y-3">
        {zones.isLoading ? (
          <Loading />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Code</Th>
                <Th>Name</Th>
                <Th className="text-right">Total capacity</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {zones.data?.map((z) => (
                <tr key={z.id}>
                  <Td className="font-medium">{z.code}</Td>
                  <Td>{z.name}</Td>
                  <Td className="tabular text-right">{z.total_capacity}</Td>
                  <Td className="text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        const n = Number(prompt(`How many numbered spaces to add to ${z.code}?`, '10'))
                        if (n > 0) addSpaces.mutate({ zoneId: z.id, count: n, prefix: `${z.code}-` })
                      }}
                    >
                      + spaces
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        {addSpaces.isSuccess && <p className="text-xs text-ok">Spaces added.</p>}
        <ErrorMessage error={addSpaces.error} />
        <form
          className="grid gap-2 sm:grid-cols-[6rem_1fr_8rem_auto]"
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <Input required placeholder="Code" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />
          <Input required placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Input required type="number" min={1} value={form.total_capacity} onChange={(e) => setForm({ ...form, total_capacity: e.target.value })} />
          <Button type="submit" variant="secondary" disabled={create.isPending}>
            Add zone
          </Button>
        </form>
        <ErrorMessage error={create.error} />
      </CardBody>
    </Card>
  )
}

function GatesCard({ venue }: { venue: Venue }) {
  const client = useQueryClient()
  const gates = useQuery({
    queryKey: ['gates', venue.id],
    queryFn: () => must(staffApi.GET('/admin/v1/venues/{venue_id}/gates', { params: { path: { venue_id: venue.id } } })),
  })
  const [form, setForm] = useState({ code: '', name: '', direction: 'ENTRY' as 'ENTRY' | 'EXIT' })
  const create = useMutation({
    mutationFn: () =>
      must(
        staffApi.POST('/admin/v1/venues/{venue_id}/gates', {
          params: { path: { venue_id: venue.id } },
          body: { ...form, has_printer: form.direction === 'ENTRY', has_terminal: form.direction === 'EXIT' },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['gates', venue.id] })
      setForm({ ...form, code: '', name: '' })
    },
  })
  return (
    <Card>
      <CardHeader title="Gates and devices" sub="each lane has one device (the Raspberry Pi) with its own API key" />
      <CardBody className="space-y-3">
        {gates.isLoading ? <Loading /> : gates.data?.map((g) => <GateRow key={g.id} gate={g} />)}
        <form
          className="grid gap-2 sm:grid-cols-[6rem_1fr_8rem_auto]"
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <Input required placeholder="Code" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />
          <Input required placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Select value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value as 'ENTRY' | 'EXIT' })}>
            <option value="ENTRY">Entry</option>
            <option value="EXIT">Exit</option>
          </Select>
          <Button type="submit" variant="secondary" disabled={create.isPending}>
            Add gate
          </Button>
        </form>
        <ErrorMessage error={create.error} />
      </CardBody>
    </Card>
  )
}

function GateRow({ gate }: { gate: Gate }) {
  const client = useQueryClient()
  const [key, setKey] = useState<string | null>(null)
  const device = useQuery({
    queryKey: ['device', gate.id],
    retry: false,
    queryFn: async () => {
      const { data, response } = await staffApi.GET('/admin/v1/gates/{gate_id}/device', { params: { path: { gate_id: gate.id } } })
      if (response.status === 404) return null
      if (!response.ok) throw new Error('Could not load the device')
      return data ?? null
    },
  })
  const onKey = (result: { api_key: string }) => {
    setKey(result.api_key)
    void client.invalidateQueries({ queryKey: ['device', gate.id] })
  }
  const create = useMutation({
    mutationFn: () =>
      must(staffApi.POST('/admin/v1/gates/{gate_id}/device', { params: { path: { gate_id: gate.id } }, body: { name: `${gate.code}-pi` } })),
    onSuccess: onKey,
  })
  const rotate = useMutation({
    mutationFn: () => must(staffApi.POST('/admin/v1/gates/{gate_id}/device/rotate-key', { params: { path: { gate_id: gate.id } } })),
    onSuccess: onKey,
  })
  const d = device.data
  return (
    <div className="rounded-md border border-line p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{gate.code}</span>
        <span className="text-muted">{gate.name}</span>
        <Badge>{gate.direction.toLowerCase()}</Badge>
        {d ? (
          <>
            <Badge tone={d.status === 'ONLINE' ? 'ok' : d.status === 'DISABLED' ? 'neutral' : 'bad'}>{d.status.toLowerCase()}</Badge>
            <span className="text-xs text-muted">
              key epd_{d.api_key_prefix}_… · heartbeat {ago(d.last_heartbeat_at)}
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto"
              onClick={() => confirm('Rotate the key? The old one stops working immediately.') && rotate.mutate()}
            >
              <KeyRound className="size-4" aria-hidden /> Rotate key
            </Button>
          </>
        ) : (
          d === null && (
            <Button size="sm" variant="secondary" className="ml-auto" onClick={() => create.mutate()}>
              Create device
            </Button>
          )
        )}
      </div>
      {key && (
        <div className="mt-2 rounded-md bg-warn/15 p-2 text-xs">
          <p className="font-medium">Copy this key now: it is shown only once (only its hash is stored).</p>
          <code className="mt-1 block break-all font-mono">{key}</code>
        </div>
      )}
      <ErrorMessage error={create.error ?? rotate.error} />
    </div>
  )
}

// --- events ----------------------------------------------------------------------------------------------------------

function useEventsList() {
  return useQuery({
    queryKey: ['events'],
    queryFn: () => must(staffApi.GET('/admin/v1/events', { params: { query: { limit: 200 } } })),
  })
}

function EventsSetup() {
  const events = useEventsList()
  const [selected, setSelected] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const list = [...(events.data?.items ?? [])].sort((a, b) => b.starts_at.localeCompare(a.starts_at))
  const current = selected ?? list[0]?.id
  return (
    <div className="grid gap-6 lg:grid-cols-[16rem_1fr]">
      <Card>
        <CardHeader title="Events" action={<Button size="sm" variant="secondary" onClick={() => setCreating(true)}>New</Button>} />
        <CardBody className="space-y-1 p-2">
          {list.map((e) => (
            <Pill
              key={e.id}
              active={!creating && e.id === current}
              onClick={() => {
                setCreating(false)
                setSelected(e.id)
              }}
            >
              {e.name}
              <span className="block text-xs text-muted">
                {e.status.toLowerCase()} · {dateTime(e.starts_at)}
              </span>
            </Pill>
          ))}
        </CardBody>
      </Card>
      {creating ? (
        <NewEvent
          onCreated={(id) => {
            setCreating(false)
            setSelected(id)
          }}
        />
      ) : current ? (
        <EventEditor key={current} eventId={current} />
      ) : (
        <Empty>No events yet.</Empty>
      )}
    </div>
  )
}

function localInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** <input type="datetime-local"> has no zone; we send it as an aware ISO time in the browser's zone (API rejects naive). */
function toIso(local: string): string {
  return new Date(local).toISOString()
}

function NewEvent({ onCreated }: { onCreated: (id: string) => void }) {
  const client = useQueryClient()
  const venues = useVenues()
  const [form, setForm] = useState(() => {
    // Defaults: next week 20:00, gates open 2 h before, booking from now until 1 h before.
    const start = new Date(Date.now() + 7 * 86_400_000)
    start.setHours(20, 0, 0, 0)
    const h = (n: number) => localInput(new Date(start.getTime() + n * 3_600_000))
    return {
      name: '',
      venue_id: '',
      starts_at: h(0),
      ends_at: h(3),
      gates_open_at: h(-2),
      gates_close_at: h(5),
      booking_opens_at: localInput(new Date()),
      booking_closes_at: h(-1),
    }
  })
  const create = useMutation({
    mutationFn: () =>
      must(
        staffApi.POST('/admin/v1/events', {
          body: {
            name: form.name,
            venue_id: form.venue_id || venues.data![0].id,
            starts_at: toIso(form.starts_at),
            ends_at: toIso(form.ends_at),
            gates_open_at: toIso(form.gates_open_at),
            gates_close_at: toIso(form.gates_close_at),
            booking_opens_at: toIso(form.booking_opens_at),
            booking_closes_at: toIso(form.booking_closes_at),
          },
        }),
      ),
    onSuccess: (e) => {
      void client.invalidateQueries({ queryKey: ['events'] })
      onCreated(e.id)
    },
  })
  const time = (key: keyof typeof form, label: string) => (
    <Field label={label}>
      <Input type="datetime-local" required value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
    </Field>
  )
  return (
    <Card>
      <CardHeader title="New event" sub="created as DRAFT; set pricing and zones, then publish" />
      <CardBody>
        <form
          className="grid gap-3 sm:grid-cols-2"
          onSubmit={(e: FormEvent) => {
            e.preventDefault()
            create.mutate()
          }}
        >
          <Field label="Name">
            <Input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Venue">
            <Select value={form.venue_id} onChange={(e) => setForm({ ...form, venue_id: e.target.value })}>
              {venues.data?.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name}
                </option>
              ))}
            </Select>
          </Field>
          {time('starts_at', 'Starts')}
          {time('ends_at', 'Ends')}
          {time('gates_open_at', 'Gates open')}
          {time('gates_close_at', 'Gates close')}
          {time('booking_opens_at', 'Booking opens')}
          {time('booking_closes_at', 'Booking closes')}
          <div className="sm:col-span-2">
            <ErrorMessage error={create.error} />
            <Button type="submit" disabled={create.isPending} className="mt-2">
              Create event
            </Button>
          </div>
        </form>
      </CardBody>
    </Card>
  )
}

const ACTIONS: Record<string, ('publish' | 'go-live' | 'end' | 'archive' | 'cancel')[]> = {
  DRAFT: ['publish', 'cancel'],
  PUBLISHED: ['go-live', 'cancel'],
  LIVE: ['end'],
  ENDED: ['archive'],
}

function EventEditor({ eventId }: { eventId: string }) {
  const client = useQueryClient()
  const detail = useQuery({
    queryKey: ['event', eventId],
    queryFn: () => must(staffApi.GET('/admin/v1/events/{event_id}', { params: { path: { event_id: eventId } } })),
  })
  const lifecycle = useMutation({
    mutationFn: (action: 'publish' | 'go-live' | 'end' | 'archive' | 'cancel') =>
      must(staffApi.POST(`/admin/v1/events/{event_id}/${action}`, { params: { path: { event_id: eventId } } })),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['event', eventId] })
      void client.invalidateQueries({ queryKey: ['events'] })
    },
  })
  if (detail.isLoading) return <Loading />
  if (!detail.data) return <ErrorMessage error={detail.error} />
  const e = detail.data
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title={e.name}
          sub={`${dateTime(e.starts_at)} → ${dateTime(e.ends_at)} · gates ${dateTime(e.gates_open_at)} – ${dateTime(e.gates_close_at)}`}
          action={<Badge tone={e.status === 'LIVE' ? 'ok' : 'neutral'}>{e.status.toLowerCase()}</Badge>}
        />
        <CardBody className="flex flex-wrap gap-2">
          {(ACTIONS[e.status] ?? []).map((a) => (
            <Button
              key={a}
              size="sm"
              variant={a === 'cancel' ? 'danger' : 'secondary'}
              disabled={lifecycle.isPending}
              onClick={() => (a !== 'cancel' || confirm('Cancel this event?')) && lifecycle.mutate(a)}
            >
              {a.replace('-', ' ')}
            </Button>
          ))}
          <ErrorMessage error={lifecycle.error} />
        </CardBody>
      </Card>
      <PricingCard event={e} />
      <EventZonesCard event={e} />
      <ReportsCard eventId={e.id} />
      <DocumentsCard eventId={e.id} />
    </div>
  )
}

function PricingCard({ event }: { event: EventDetail }) {
  const client = useQueryClient()
  const p = event.pricing
  const [form, setForm] = useState({
    free: String(p?.drive_up_free_minutes ?? 15),
    hourly: fromMinor(p?.drive_up_hourly_minor ?? 30_000),
    cap: fromMinor(p?.drive_up_daily_cap_minor ?? 150_000),
    lost: fromMinor(p?.lost_ticket_fee_minor ?? 200_000),
    grace: String(p?.post_payment_exit_grace_minutes ?? 15),
  })
  const save = useMutation({
    mutationFn: () =>
      must(
        staffApi.PUT('/admin/v1/events/{event_id}/pricing', {
          params: { path: { event_id: event.id } },
          body: {
            currency: 'RSD',
            drive_up_free_minutes: Number(form.free),
            drive_up_hourly_minor: toMinor(form.hourly),
            drive_up_daily_cap_minor: toMinor(form.cap),
            lost_ticket_fee_minor: toMinor(form.lost),
            post_payment_exit_grace_minutes: Number(form.grace),
          },
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['event', event.id] }),
  })
  const input = (key: keyof typeof form, label: string, money = false) => (
    <Field label={label}>
      <Input
        required
        inputMode={money ? 'decimal' : 'numeric'}
        pattern={money ? '\\d+(\\.\\d{1,2})?' : '\\d+'}
        value={form[key]}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
    </Field>
  )
  return (
    <Card>
      <CardHeader title="Drive-up pricing" sub="amounts in RSD; stored as integer para" />
      <CardBody>
        <form
          className="grid gap-3 sm:grid-cols-5"
          onSubmit={(e) => {
            e.preventDefault()
            save.mutate()
          }}
        >
          {input('free', 'Free minutes')}
          {input('hourly', 'Per started hour', true)}
          {input('cap', 'Daily cap', true)}
          {input('lost', 'Lost ticket fee', true)}
          {input('grace', 'Exit grace (min)')}
          <div className="sm:col-span-5">
            <ErrorMessage error={save.error} />
            <Button type="submit" size="sm" disabled={save.isPending}>
              Save pricing
            </Button>
            {save.isSuccess && <span className="ml-3 text-xs text-ok">Saved.</span>}
          </div>
        </form>
      </CardBody>
    </Card>
  )
}

type ZoneRow = { zone_id: string; code: string; capacity: string; prebook_quota: string; price: string; priority: string }

function EventZonesCard({ event }: { event: EventDetail }) {
  const client = useQueryClient()
  const venueZones = useQuery({
    queryKey: ['zones', event.venue_id],
    queryFn: () => must(staffApi.GET('/admin/v1/venues/{venue_id}/zones', { params: { path: { venue_id: event.venue_id } } })),
  })
  const [rows, setRows] = useState<ZoneRow[] | null>(null)
  const current: ZoneRow[] =
    rows ??
    (venueZones.data ?? []).map((z) => {
      const ez = event.zones.find((x) => x.zone_id === z.id)
      return {
        zone_id: z.id,
        code: z.code,
        capacity: String(ez?.capacity ?? 0),
        prebook_quota: String(ez?.prebook_quota ?? 0),
        price: fromMinor(ez?.prebook_price_minor ?? 0),
        priority: String(ez?.allocation_priority ?? 0),
      }
    })
  const update = (i: number, key: keyof ZoneRow, value: string) =>
    setRows(current.map((r, j) => (j === i ? { ...r, [key]: value } : r)))
  const save = useMutation({
    mutationFn: () =>
      must(
        staffApi.PUT('/admin/v1/events/{event_id}/zones', {
          params: { path: { event_id: event.id } },
          body: current
            .filter((r) => Number(r.capacity) > 0)
            .map((r) => ({
              zone_id: r.zone_id,
              capacity: Number(r.capacity),
              prebook_quota: Number(r.prebook_quota),
              prebook_price_minor: toMinor(r.price),
              allocation_priority: Number(r.priority),
            })),
        }),
      ),
    onSuccess: () => {
      setRows(null)
      void client.invalidateQueries({ queryKey: ['event', event.id] })
    },
  })
  return (
    <Card>
      <CardHeader title="Zone capacity for this event" sub="drive-up gets capacity − pre-book quota; priority 0 = no drive-up" />
      <CardBody className="space-y-3">
        <Table>
          <thead>
            <tr>
              <Th>Zone</Th>
              <Th>Capacity</Th>
              <Th>Pre-book quota</Th>
              <Th>Pre-book price</Th>
              <Th>Drive-up priority</Th>
              <Th className="text-right">Now</Th>
            </tr>
          </thead>
          <tbody>
            {current.map((r, i) => {
              const ez = event.zones.find((x) => x.zone_id === r.zone_id)
              return (
                <tr key={r.zone_id}>
                  <Td className="font-medium">{r.code}</Td>
                  {(['capacity', 'prebook_quota', 'price', 'priority'] as const).map((k) => (
                    <Td key={k}>
                      <Input aria-label={`${r.code} ${k}`} className="w-24" value={r[k]} onChange={(e) => update(i, k, e.target.value)} />
                    </Td>
                  ))}
                  <Td className="tabular text-right text-xs text-muted">
                    {ez ? `${ez.drive_up_occupied} drive-up · ${ez.prebook_held} booked · ${money(ez.prebook_price_minor)}` : '-'}
                  </Td>
                </tr>
              )
            })}
          </tbody>
        </Table>
        <ErrorMessage error={save.error} />
        <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
          Save zones
        </Button>
      </CardBody>
    </Card>
  )
}

function ReportsCard({ eventId }: { eventId: string }) {
  const [ids, setIds] = useState<string[]>([])
  const request = useMutation({
    mutationFn: (kind: 'CSV' | 'PDF') =>
      must(staffApi.POST('/admin/v1/events/{event_id}/reports', { params: { path: { event_id: eventId } }, body: { kind } })),
    onSuccess: (r) => setIds((list) => [r.id, ...list]),
  })
  return (
    <Card>
      <CardHeader title="Reports" sub="generated in the background by the reports worker, then downloaded from S3" />
      <CardBody className="space-y-3">
        <div className="flex gap-2">
          <Button size="sm" variant="secondary" onClick={() => request.mutate('CSV')} disabled={request.isPending}>
            Request CSV (all passages)
          </Button>
          <Button size="sm" variant="secondary" onClick={() => request.mutate('PDF')} disabled={request.isPending}>
            Request PDF (summary)
          </Button>
        </div>
        <ErrorMessage error={request.error} />
        {ids.map((id) => (
          <ReportRow key={id} id={id} />
        ))}
      </CardBody>
    </Card>
  )
}

function ReportRow({ id }: { id: string }) {
  const report = useQuery({
    queryKey: ['report', id],
    queryFn: () => must(staffApi.GET('/admin/v1/reports/{report_id}', { params: { path: { report_id: id } } })),
    refetchInterval: (q) => (q.state.data && ['READY', 'FAILED'].includes(q.state.data.status) ? false : 1_500),
  })
  const download = async () => {
    const { url } = await must(staffApi.GET('/admin/v1/reports/{report_id}/download', { params: { path: { report_id: id } } }))
    window.location.assign(url) // presigned URL, valid for 5 minutes
  }
  const r = report.data
  return (
    <div className="flex items-center gap-3 text-sm">
      <Badge>{r?.kind ?? '…'}</Badge>
      <Badge tone={r?.status === 'READY' ? 'ok' : r?.status === 'FAILED' ? 'bad' : 'warn'}>{(r?.status ?? 'queued').toLowerCase()}</Badge>
      <span className="text-xs text-muted">{dateTime(r?.created_at)}</span>
      {r?.status === 'READY' && (
        <Button size="sm" variant="ghost" onClick={() => void download()}>
          <Download className="size-4" aria-hidden /> Download
        </Button>
      )}
      {r?.error && <span className="text-xs text-bad">{r.error}</span>}
    </div>
  )
}

function DocumentsCard({ eventId }: { eventId: string }) {
  const client = useQueryClient()
  const docs = useQuery({
    queryKey: ['documents', eventId],
    queryFn: () => must(staffApi.GET('/admin/v1/events/{event_id}/documents', { params: { path: { event_id: eventId } } })),
  })
  // Browser -> S3 directly with a presigned PUT (docs/02 §3.6): the file never passes through the API.
  const upload = useMutation({
    mutationFn: async (file: File) => {
      const contentType = file.type || 'application/octet-stream'
      const slot = await must(
        staffApi.POST('/admin/v1/events/{event_id}/documents/upload-url', {
          params: { path: { event_id: eventId } },
          body: { filename: file.name, content_type: contentType, size_bytes: file.size },
        }),
      )
      const put = await fetch(slot.upload_url, { method: 'PUT', body: file, headers: { 'Content-Type': contentType } })
      if (!put.ok) throw new Error(`Upload to storage failed (${put.status})`)
      return must(staffApi.POST('/admin/v1/documents/{document_id}/confirm', { params: { path: { document_id: slot.document.id } } }))
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ['documents', eventId] }),
  })
  return (
    <Card>
      <CardHeader title="Documents" sub="traffic plans, contracts: uploaded straight to S3" />
      <CardBody className="space-y-3">
        <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-line px-3 py-1.5 text-sm hover:bg-raised">
          <Upload className="size-4" aria-hidden /> {upload.isPending ? 'Uploading…' : 'Upload a file'}
          <input
            type="file"
            className="sr-only"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) upload.mutate(file)
              e.target.value = ''
            }}
          />
        </label>
        <ErrorMessage error={upload.error} />
        {docs.data?.length === 0 && <p className="text-sm text-muted">No documents.</p>}
        <ul className="space-y-1 text-sm">
          {docs.data?.map((d) => (
            <li key={d.id} className="flex items-center gap-2">
              <a href={d.download_url} className="text-brand hover:underline" target="_blank" rel="noreferrer">
                {d.filename}
              </a>
              <span className="text-xs text-muted">
                {Math.ceil(d.size_bytes / 1024)} KB · {dateTime(d.created_at)}
              </span>
            </li>
          ))}
        </ul>
      </CardBody>
    </Card>
  )
}
