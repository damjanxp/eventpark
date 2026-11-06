import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { driverAuth } from '@/api/auth'
import { driverApi, must } from '@/api/client'
import type { PublicZone } from '@/api/types'
import { useAuth } from '@/api/useAuth'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardBody, CardHeader } from '@/components/ui/card'
import { ErrorMessage, Loading } from '@/components/ui/feedback'
import { Field, Input } from '@/components/ui/input'
import { cn } from '@/lib/cn'
import { dateTime, money, requestId } from '@/lib/format'

export function EventPage() {
  const { eventId = '' } = useParams()
  const auth = useAuth(driverAuth)
  const navigate = useNavigate()
  const [zone, setZone] = useState<PublicZone | null>(null)
  const [spaceId, setSpaceId] = useState<string | null>(null)
  const [plate, setPlate] = useState('')
  // One idempotency key per booking attempt: a double click or a retry after a timeout can't book twice.
  const [key] = useState(requestId)

  const event = useQuery({
    queryKey: ['public-event', eventId],
    // Availability changes as others book: re-read every 5 s (the read model makes this cheap for the API).
    refetchInterval: 5_000,
    queryFn: () => must(driverApi.GET('/public/v1/events/{event_id}', { params: { path: { event_id: eventId } } })),
  })
  const spaces = useQuery({
    queryKey: ['spaces', eventId, zone?.event_zone_id],
    enabled: !!zone?.has_spaces,
    refetchInterval: 5_000,
    queryFn: () =>
      must(
        driverApi.GET('/public/v1/events/{event_id}/zones/{event_zone_id}/spaces', {
          params: { path: { event_id: eventId, event_zone_id: zone!.event_zone_id } },
        }),
      ),
  })
  const book = useMutation({
    mutationFn: () =>
      must(
        driverApi.POST('/public/v1/bookings', {
          params: { header: { 'idempotency-key': key } },
          body: { event_id: eventId, event_zone_id: zone!.event_zone_id, space_id: spaceId, plate },
        }),
      ),
    onSuccess: (booking) => {
      // Off to the payment provider's checkout page; it sends the driver back to /book/bookings/{id}.
      if (booking.checkout_url) window.location.assign(booking.checkout_url)
      else navigate(`/book/bookings/${booking.id}`)
    },
  })

  if (event.isLoading) return <Loading />
  if (event.error || !event.data) return <ErrorMessage error={event.error ?? 'Event not found'} />
  const e = event.data
  const needsSpace = zone?.has_spaces && !spaceId

  return (
    <div className="space-y-6">
      <div>
        <Link to="/book" className="text-sm text-muted hover:underline">
          ← All events
        </Link>
        <h1 className="mt-1 text-2xl font-semibold">{e.name}</h1>
        <p className="text-sm text-muted">
          {dateTime(e.starts_at)} · gates open {dateTime(e.gates_open_at)} · booking until {dateTime(e.booking_closes_at)}
        </p>
      </div>

      {!e.bookable_now && (
        <p className="rounded-md border border-line bg-raised px-3 py-2 text-sm">
          Online booking is closed for this event. You can still drive up and pay at the exit.
        </p>
      )}
      <Card>
        <CardHeader title="1. Choose a zone" sub="availability updates every few seconds" />
        <CardBody className="grid gap-3 sm:grid-cols-3">
          {e.zones.map((z) => {
            const soldOut = z.available <= 0
            return (
              <button
                key={z.event_zone_id}
                disabled={soldOut || !e.bookable_now}
                onClick={() => {
                  setZone(z)
                  setSpaceId(null)
                }}
                className={cn(
                  'rounded-lg border border-line p-4 text-left transition-colors hover:border-brand disabled:cursor-not-allowed disabled:opacity-50',
                  zone?.event_zone_id === z.event_zone_id && 'border-brand ring-2 ring-brand/30',
                )}
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold">{z.zone_name}</span>
                  {z.has_spaces && <Badge tone="brand">numbered</Badge>}
                </div>
                <p className="mt-2 text-lg font-semibold">{money(z.price_minor, z.currency)}</p>
                <p className={cn('text-sm', soldOut ? 'text-bad' : 'text-muted')}>{soldOut ? 'Sold out' : `${z.available} left`}</p>
              </button>
            )
          })}
        </CardBody>
      </Card>

      {zone?.has_spaces && (
        <Card>
          <CardHeader title="2. Pick your space" />
          <CardBody>
            {spaces.isLoading ? (
              <Loading />
            ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(4.5rem,1fr))] gap-2">
                {spaces.data?.map((s) => (
                  <button
                    key={s.id}
                    onClick={() => setSpaceId(s.id)}
                    className={cn(
                      'rounded-md border border-line px-2 py-2 text-sm font-medium hover:border-brand',
                      spaceId === s.id && 'border-brand bg-brand text-brand-ink',
                    )}
                  >
                    {s.label}
                  </button>
                ))}
                {spaces.data?.length === 0 && <p className="text-sm text-muted">No free spaces left.</p>}
              </div>
            )}
          </CardBody>
        </Card>
      )}

      {zone && (
        <Card>
          <CardHeader title={zone.has_spaces ? '3. Your car and payment' : '2. Your car and payment'} />
          <CardBody className="space-y-3">
            <Field label="Licence plate (optional)" hint="Helps staff find your ticket if you lose your phone.">
              <Input value={plate} maxLength={20} onChange={(ev) => setPlate(ev.target.value)} placeholder="BG 123-AB" className="max-w-xs" />
            </Field>
            <ErrorMessage error={book.error} />
            {auth.status === 'authenticated' ? (
              <Button size="lg" disabled={!!needsSpace || book.isPending} onClick={() => book.mutate()}>
                Pay {money(zone.price_minor, zone.currency)}
              </Button>
            ) : (
              <Button size="lg" onClick={() => navigate('/book/login', { state: { from: `/book/events/${eventId}` } })}>
                Sign in to book
              </Button>
            )}
            <p className="text-xs text-muted">Your place is held for 15 minutes while you pay.</p>
          </CardBody>
        </Card>
      )}
    </div>
  )
}
