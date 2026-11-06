import { CarFront } from 'lucide-react'
import { Link, Outlet } from 'react-router'
import { driverAuth } from '@/api/auth'
import { useAuth } from '@/api/useAuth'

export function BookingLayout() {
  const auth = useAuth(driverAuth)
  return (
    <div className="min-h-screen">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-14 max-w-4xl items-center gap-4 px-4">
          <Link to="/book" className="flex items-center gap-2 font-semibold">
            <CarFront className="size-5 text-brand" aria-hidden /> EventPark parking
          </Link>
          <nav className="ml-auto flex items-center gap-3 text-sm">
            {auth.status === 'authenticated' ? (
              <>
                <Link to="/book/bookings" className="hover:underline">
                  My bookings
                </Link>
                <button className="text-muted hover:text-ink" onClick={() => void driverAuth.logout()}>
                  Sign out
                </button>
              </>
            ) : (
              <Link to="/book/login" className="hover:underline">
                Sign in
              </Link>
            )}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-4xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
