import { useQuery } from '@tanstack/react-query'
import { CalendarDays } from 'lucide-react'
import { Link } from 'react-router'
import { driverApi, must } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Card, CardBody } from '@/components/ui/card'
import { Empty, ErrorMessage, Loading } from '@/components/ui/feedback'
import { dateTime } from '@/lib/format'

export function EventList() {
  const events = useQuery({ queryKey: ['public-events'], queryFn: () => must(driverApi.GET('/public/v1/events')) })
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Upcoming events</h1>
      {events.isLoading && <Loading />}
      <ErrorMessage error={events.error} />
      {events.data?.length === 0 && <Empty>No events open for booking right now.</Empty>}
      <div className="grid gap-3 sm:grid-cols-2">
        {events.data?.map((e) => (
          <Link key={e.id} to={`/book/events/${e.id}`}>
            <Card className="h-full transition-colors hover:border-brand">
              <CardBody className="space-y-2">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="font-semibold">{e.name}</h2>
                  {e.bookable_now ? <Badge tone="ok">bookable</Badge> : <Badge>{e.status.toLowerCase()}</Badge>}
                </div>
                <p className="flex items-center gap-1.5 text-sm text-muted">
                  <CalendarDays className="size-4" aria-hidden /> {dateTime(e.starts_at)}
                </p>
                <p className="text-xs text-muted">Gates open {dateTime(e.gates_open_at)}</p>
              </CardBody>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  )
}
