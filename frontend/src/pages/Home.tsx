import { CarFront, Gauge, Ticket } from 'lucide-react'
import { Link } from 'react-router'
import { Card, CardBody } from '@/components/ui/card'

export function Home() {
  return (
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-8 p-6">
      <div>
        <h1 className="flex items-center gap-2 text-3xl font-semibold">
          <CarFront className="size-8 text-brand" aria-hidden /> EventPark
        </h1>
        <p className="mt-2 text-muted">Parking for stadiums, concerts and fairs: pre-book a place, or drive up and pay on the way out.</p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Link to="/book">
          <Card className="h-full transition-colors hover:border-brand">
            <CardBody className="space-y-2">
              <Ticket className="size-6 text-brand" aria-hidden />
              <h2 className="font-semibold">Book parking</h2>
              <p className="text-sm text-muted">See upcoming events, choose a zone or a VIP space, pay, get your QR code.</p>
            </CardBody>
          </Card>
        </Link>
        <Link to="/login">
          <Card className="h-full transition-colors hover:border-brand">
            <CardBody className="space-y-2">
              <Gauge className="size-6 text-brand" aria-hidden />
              <h2 className="font-semibold">Staff</h2>
              <p className="text-sm text-muted">Live dashboard, operator console, event setup and reports.</p>
            </CardBody>
          </Card>
        </Link>
      </div>
    </div>
  )
}
