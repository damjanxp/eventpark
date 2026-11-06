import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { driverApi, must } from '@/api/client'
import { Card, CardBody } from '@/components/ui/card'
import { Empty, ErrorMessage, Loading } from '@/components/ui/feedback'
import { dateTime, money } from '@/lib/format'
import { BookingStatusBadge } from './BookingPage'

export function MyBookings() {
  const bookings = useQuery({ queryKey: ['my-bookings'], queryFn: () => must(driverApi.GET('/public/v1/bookings')) })
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">My bookings</h1>
      {bookings.isLoading && <Loading />}
      <ErrorMessage error={bookings.error} />
      {bookings.data?.length === 0 && (
        <Empty>
          No bookings yet. <Link to="/book" className="text-brand hover:underline">Find an event</Link>
        </Empty>
      )}
      <div className="space-y-2">
        {bookings.data?.map((b) => (
          <Link key={b.id} to={`/book/bookings/${b.id}`} className="block">
            <Card className="transition-colors hover:border-brand">
              <CardBody className="flex flex-wrap items-center gap-3 py-3">
                <span className="font-medium">
                  Zone {b.zone_code}
                  {b.space_label && ` · ${b.space_label}`}
                </span>
                <BookingStatusBadge status={b.status} />
                <span className="ml-auto text-sm text-muted">
                  {money(b.price_minor, b.currency)} · {dateTime(b.created_at)}
                </span>
              </CardBody>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  )
}
