import { useMutation, useQueryClient } from '@tanstack/react-query'
import { DoorOpen, Search, TicketX } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { staffAuth } from '@/api/auth'
import { must, staffApi } from '@/api/client'
import type { GateActivity, LookupResult } from '@/api/types'
import { useAuth } from '@/api/useAuth'
import { EventPicker, StatusDot } from '@/components/EventPicker'
import { GateGrid } from '@/components/GateGrid'
import { PassageFeed } from '@/components/PassageFeed'
import { OPERATORS } from '@/lib/roles'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardBody, CardHeader } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Empty, ErrorMessage, Loading } from '@/components/ui/feedback'
import { Field, Input, Select, Textarea } from '@/components/ui/input'
import { useGateActivity, usePassages, useVenue } from '@/hooks/useEventData'
import { queryKeys, useEventStream } from '@/hooks/useEventStream'
import { useSelectedEvent } from '@/hooks/useSelectedEvent'
import { dateTime, money, requestId } from '@/lib/format'

export function OperatorConsole() {
  const auth = useAuth(staffAuth)
  // UI only: PARKING_STAFF sees the console read-only. The API enforces this too (OperatorDep).
  const canAct = auth.status === 'authenticated' && OPERATORS.includes(auth.user.role)
  const { events, event, select, isLoading } = useSelectedEvent()
  const { status } = useEventStream(event?.id)
  const venue = useVenue(event?.venue_id)
  const tz = venue.data?.timezone
  const activity = useGateActivity(event?.id)
  const passages = usePassages(event?.id, status)
  const [openGate, setOpenGate] = useState<GateActivity | null>(null)
  const [lostOpen, setLostOpen] = useState(false)

  if (isLoading) return <Loading />
  if (!event) return <Empty>No events yet.</Empty>
  const exits = activity.data?.filter((g) => g.direction === 'EXIT') ?? []

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="mr-auto text-xl font-semibold">Operator console</h1>
        <StatusDot status={status} />
        <EventPicker events={events} value={event.id} onChange={select} />
      </div>

      <Card>
        <CardHeader
          title="Gates"
          sub="manual open is audited and needs a reason"
          action={
            canAct && (
              <Button variant="secondary" size="sm" onClick={() => setLostOpen(true)}>
                <TicketX className="size-4" aria-hidden /> Lost ticket
              </Button>
            )
          }
        />
        <CardBody>
          {activity.data ? (
            <GateGrid
              gates={activity.data}
              action={
                canAct
                  ? (g) => (
                      <Button size="sm" variant="secondary" className="w-full" onClick={() => setOpenGate(g)} disabled={g.device_status === 'DISABLED'}>
                        <DoorOpen className="size-4" aria-hidden /> Manual open
                      </Button>
                    )
                  : undefined
              }
            />
          ) : (
            <Loading />
          )}
        </CardBody>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <TicketLookup canAct={canAct} tz={tz} eventId={event.id} />
        <Card>
          <CardHeader title="Live passage feed" />
          <CardBody className="max-h-[32rem] overflow-y-auto py-1">
            <PassageFeed items={passages.data?.items ?? []} tz={tz} />
          </CardBody>
        </Card>
      </div>

      <Dialog open={!!openGate} onClose={() => setOpenGate(null)} title={`Manual open: ${openGate?.code ?? ''}`}>
        {openGate && <ManualOpenForm gate={openGate} eventId={event.id} onDone={() => setOpenGate(null)} />}
      </Dialog>
      <Dialog open={lostOpen} onClose={() => setLostOpen(false)} title="Lost ticket">
        <LostTicketForm exits={exits} eventId={event.id} onDone={() => setLostOpen(false)} />
      </Dialog>
    </div>
  )
}

function useInvalidateEvent(eventId: string) {
  const client = useQueryClient()
  return () => {
    for (const key of [queryKeys.activity, queryKeys.passages, queryKeys.occupancy, queryKeys.inside]) {
      void client.invalidateQueries({ queryKey: key(eventId) })
    }
    void client.invalidateQueries({ queryKey: ['lookup'] })
  }
}

function ManualOpenForm({ gate, eventId, onDone }: { gate: GateActivity; eventId: string; onDone: () => void }) {
  const [reason, setReason] = useState('')
  const [ticketCode, setTicketCode] = useState('')
  // One id per dialog: a double click or a retried request opens the barrier once (server-side idempotency).
  const [rid] = useState(requestId)
  const invalidate = useInvalidateEvent(eventId)
  const open = useMutation({
    mutationFn: () =>
      must(
        staffApi.POST('/admin/v1/gates/{gate_id}/manual-open', {
          params: { path: { gate_id: gate.gate_id } },
          body: { request_id: rid, reason, ticket_code: ticketCode.trim() || null },
        }),
      ),
    onSuccess: invalidate,
  })
  if (open.data) {
    return (
      <div className="space-y-3 text-sm">
        <p>
          Command sent: the barrier at <strong>{gate.code}</strong> opens on the device's next heartbeat (≤ 5 s).
        </p>
        {open.data.amount_due_minor > 0 && (
          <p className="rounded-md bg-warn/15 px-3 py-2">
            Released unpaid: <strong>{money(open.data.amount_due_minor, open.data.currency)}</strong>
          </p>
        )}
        <Button onClick={onDone}>Done</Button>
      </div>
    )
  }
  const submit = (e: FormEvent) => {
    e.preventDefault()
    open.mutate()
  }
  return (
    <form onSubmit={submit} className="space-y-3">
      <Field label="Reason (required, goes into the audit log)">
        <Textarea required minLength={3} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      </Field>
      {gate.direction === 'EXIT' && (
        <Field label="Ticket code (optional)" hint="Let this car out: the ticket is marked exited and any fee due is recorded as released unpaid.">
          <Input value={ticketCode} onChange={(e) => setTicketCode(e.target.value)} placeholder="EP1-…" />
        </Field>
      )}
      <ErrorMessage error={open.error} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" variant="danger" disabled={open.isPending}>
          Open barrier
        </Button>
      </div>
    </form>
  )
}

function LostTicketForm({ exits, eventId, onDone }: { exits: GateActivity[]; eventId: string; onDone: () => void }) {
  const [gateId, setGateId] = useState(exits[0]?.gate_id ?? '')
  const [plate, setPlate] = useState('')
  const [code, setCode] = useState('')
  const [rid] = useState(requestId)
  const invalidate = useInvalidateEvent(eventId)
  const lost = useMutation({
    mutationFn: () =>
      must(
        staffApi.POST('/admin/v1/tickets/lost', {
          body: { request_id: rid, gate_id: gateId, plate: plate.trim() || null, ticket_code: code.trim() || null },
        }),
      ),
    onSuccess: invalidate,
  })
  if (lost.data) {
    return (
      <div className="space-y-3 text-sm">
        <p>
          Collected <strong>{money(lost.data.amount_paid_minor, lost.data.currency)}</strong>. The barrier opens shortly.
        </p>
        <p className="text-muted">{lost.data.ticket_id ? 'The ticket was found and voided.' : 'No matching ticket was found.'}</p>
        <Button onClick={onDone}>Done</Button>
      </div>
    )
  }
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault()
        lost.mutate()
      }}
    >
      <p className="text-sm text-muted">Collect the flat lost-ticket fee at the booth, then confirm here.</p>
      <Field label="Exit gate">
        <Select value={gateId} onChange={(e) => setGateId(e.target.value)} required>
          {exits.map((g) => (
            <option key={g.gate_id} value={g.gate_id}>
              {g.code} · {g.name}
            </option>
          ))}
        </Select>
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Plate (if known)">
          <Input value={plate} onChange={(e) => setPlate(e.target.value)} placeholder="BG 123-AB" />
        </Field>
        <Field label="Ticket code (if known)">
          <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="EP1-…" />
        </Field>
      </div>
      <ErrorMessage error={lost.error} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={lost.isPending || !gateId}>
          Fee collected, open barrier
        </Button>
      </div>
    </form>
  )
}

function TicketLookup({ canAct, tz, eventId }: { canAct: boolean; tz?: string; eventId: string }) {
  const [query, setQuery] = useState('')
  const [voiding, setVoiding] = useState<LookupResult | null>(null)
  const search = useMutation({
    mutationKey: ['lookup'],
    mutationFn: (q: string) => {
      const isCode = q.toUpperCase().startsWith('EP')
      return must(staffApi.GET('/admin/v1/tickets/lookup', { params: { query: isCode ? { code: q } : { plate: q } } }))
    },
  })
  return (
    <Card>
      <CardHeader title="Ticket lookup" sub="by ticket code (EP1-…) or plate" />
      <CardBody className="space-y-3">
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (query.trim()) search.mutate(query.trim())
          }}
        >
          <Input aria-label="Ticket code or plate" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="EP1-… or BG 123-AB" />
          <Button type="submit" variant="secondary" disabled={search.isPending}>
            <Search className="size-4" aria-hidden /> Find
          </Button>
        </form>
        <ErrorMessage error={search.error} />
        {search.data?.length === 0 && <Empty>Nothing found.</Empty>}
        {search.data?.map((t) => (
          <div key={t.ticket_id} className="rounded-md border border-line p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs">{t.code}</span>
              <Badge tone={t.status === 'INSIDE' ? 'ok' : t.status === 'VOID' ? 'bad' : 'neutral'}>{t.status.toLowerCase()}</Badge>
              <Badge>{t.type === 'PREBOOKED' ? 'pre-booked' : 'drive-up'}</Badge>
              {canAct && !['VOID', 'EXITED', 'EXPIRED'].includes(t.status) && (
                <Button size="sm" variant="ghost" className="ml-auto text-bad" onClick={() => setVoiding(t)}>
                  Void
                </Button>
              )}
            </div>
            <p className="mt-1 text-xs text-muted">
              {t.event_name} · zone {t.zone}
              {t.space && ` · ${t.space}`} · plate {t.plate ?? '-'} · in {dateTime(t.entered_at, tz)} · out {dateTime(t.exited_at, tz)}
              {t.paid_minor > 0 && ` · paid ${money(t.paid_minor)}`}
            </p>
            {t.passages.length > 0 && (
              <ol className="mt-2 space-y-0.5 text-xs text-muted">
                {t.passages.map((p) => (
                  <li key={p.id}>
                    {dateTime(p.authorized_at, tz)} · {p.gate_code} {p.direction.toLowerCase()} · {p.status.toLowerCase()}
                    {p.manual && ' · manual'}
                  </li>
                ))}
              </ol>
            )}
          </div>
        ))}
      </CardBody>
      <Dialog open={!!voiding} onClose={() => setVoiding(null)} title={`Void ticket ${voiding?.code ?? ''}`}>
        {voiding && (
          <VoidForm
            ticket={voiding}
            eventId={eventId}
            onDone={() => {
              setVoiding(null)
              if (query.trim()) search.mutate(query.trim())
            }}
          />
        )}
      </Dialog>
    </Card>
  )
}

function VoidForm({ ticket, eventId, onDone }: { ticket: LookupResult; eventId: string; onDone: () => void }) {
  const [reason, setReason] = useState('')
  const invalidate = useInvalidateEvent(eventId)
  const voidIt = useMutation({
    mutationFn: () =>
      must(staffApi.POST('/admin/v1/tickets/{ticket_id}/void', { params: { path: { ticket_id: ticket.ticket_id } }, body: { reason } })),
    onSuccess: () => {
      invalidate()
      onDone()
    },
  })
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault()
        voidIt.mutate()
      }}
    >
      <p className="text-sm text-muted">The ticket stops working at every gate; any place it holds is released.</p>
      <Field label="Reason (required)">
        <Textarea required minLength={3} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      </Field>
      <ErrorMessage error={voidIt.error} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" variant="danger" disabled={voidIt.isPending}>
          Void ticket
        </Button>
      </div>
    </form>
  )
}
