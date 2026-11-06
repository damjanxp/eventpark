import { useQuery } from '@tanstack/react-query'
import { QRCodeSVG } from 'qrcode.react'
import { Link, useParams } from 'react-router'
import { driverApi, must } from '@/api/client'
import type { Booking } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardBody } from '@/components/ui/card'
import { ErrorMessage, Loading } from '@/components/ui/feedback'
import { dateTime, money } from '@/lib/format'

export function BookingStatusBadge({ status }: { status: Booking['status'] }) {
  const tone = status === 'CONFIRMED' ? 'ok' : status === 'PENDING_PAYMENT' ? 'warn' : 'neutral'
  return <Badge tone={tone}>{status.replace('_', ' ').toLowerCase()}</Badge>
}

export function BookingPage() {
  const { bookingId = '' } = useParams()
  const booking = useQuery({
    queryKey: ['booking', bookingId],
    queryFn: () => must(driverApi.GET('/public/v1/bookings/{booking_id}', { params: { path: { booking_id: bookingId } } })),
    // Payment confirmation arrives asynchronously (provider webhook -> API): poll until it's settled.
    refetchInterval: (q) => (q.state.data?.status === 'PENDING_PAYMENT' ? 2_000 : false),
  })
  if (booking.isLoading) return <Loading />
  if (booking.error || !booking.data) return <ErrorMessage error={booking.error ?? 'Booking not found'} />
  const b = booking.data

  return (
    <div className="mx-auto max-w-md space-y-4">
      <Link to="/book/bookings" className="text-sm text-muted hover:underline">
        ← My bookings
      </Link>
      <Card>
        <CardBody className="space-y-4 p-6 text-center">
          <div className="flex items-center justify-center gap-2">
            <h1 className="text-lg font-semibold">
              Zone {b.zone_code}
              {b.space_label && ` · space ${b.space_label}`}
            </h1>
            <BookingStatusBadge status={b.status} />
          </div>
          {b.status === 'CONFIRMED' && b.ticket_code ? (
            <>
              <div className="mx-auto w-fit rounded-lg bg-white p-4">
                <QRCodeSVG value={b.ticket_code} size={220} level="M" title="Your parking pass" />
              </div>
              <p className="font-mono text-xs text-muted">{b.ticket_code}</p>
              <p className="text-sm">Show this at the entry lane. It works once for entry and once for exit.</p>
            </>
          ) : b.status === 'PENDING_PAYMENT' ? (
            <>
              <p className="text-sm text-muted">
                Waiting for your payment of {money(b.price_minor, b.currency)}. Your place is held until{' '}
                {dateTime(b.expires_at)}.
              </p>
              {b.checkout_url && (
                <Button size="lg" onClick={() => window.location.assign(b.checkout_url!)}>
                  Pay now
                </Button>
              )}
              <p className="text-xs text-muted">Already paid? This page updates by itself in a few seconds.</p>
            </>
          ) : (
            <p className="text-sm text-muted">This booking is {b.status.toLowerCase()}.</p>
          )}
          {b.plate && <p className="text-xs text-muted">Plate {b.plate}</p>}
        </CardBody>
      </Card>
    </div>
  )
}
