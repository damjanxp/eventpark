/* oxlint-disable react/only-export-components -- route table, not a hot-reloaded component module */
import { lazy, Suspense } from 'react'
import { createBrowserRouter, Navigate } from 'react-router'
import { driverAuth, staffAuth } from './api/auth'
import { RequireRole } from './components/guards'
import { MANAGERS, OPERATORS, STAFF } from './lib/roles'
import { StaffLayout } from './components/StaffLayout'
import { BookingLayout } from './pages/book/BookingLayout'
import { BookingPage } from './pages/book/BookingPage'
import { DriverLogin } from './pages/book/DriverLogin'
import { EventList } from './pages/book/EventList'
import { EventPage } from './pages/book/EventPage'
import { MyBookings } from './pages/book/MyBookings'
import { Home } from './pages/Home'
import { StaffLogin } from './pages/staff/StaffLogin'
import { Loading } from './components/ui/feedback'

// Staff pages load on demand (code splitting): drivers on the booking site never download the chart library.
const Dashboard = lazy(() => import('./pages/dashboard/Dashboard').then((m) => ({ default: m.Dashboard })))
const HistoryPage = lazy(() => import('./pages/dashboard/History').then((m) => ({ default: m.HistoryPage })))
const OperatorConsole = lazy(() => import('./pages/operator/OperatorConsole').then((m) => ({ default: m.OperatorConsole })))
const Setup = lazy(() => import('./pages/setup/Setup').then((m) => ({ default: m.Setup })))

const staff = (roles: typeof STAFF, element: React.ReactNode) => (
  <RequireRole store={staffAuth} roles={roles} loginPath="/login">
    <Suspense fallback={<Loading />}>{element}</Suspense>
  </RequireRole>
)

// SPA routes never start with /admin, /public, /gate, /ws: those paths belong to the API (ADR-029).
export const router = createBrowserRouter([
  { path: '/', element: <Home /> },
  { path: '/login', element: <StaffLogin /> },
  {
    element: <StaffLayout />,
    children: [
      { path: '/dashboard', element: staff(STAFF, <Dashboard />) },
      { path: '/operator', element: staff(OPERATORS.concat('PARKING_STAFF'), <OperatorConsole />) },
      { path: '/history', element: staff(STAFF, <HistoryPage />) },
      { path: '/setup', element: staff(MANAGERS, <Setup />) },
    ],
  },
  {
    path: '/book',
    element: <BookingLayout />,
    children: [
      { index: true, element: <EventList /> },
      { path: 'events/:eventId', element: <EventPage /> },
      { path: 'login', element: <DriverLogin /> },
      {
        path: 'bookings',
        element: (
          <RequireRole store={driverAuth} roles={['DRIVER']} loginPath="/book/login">
            <MyBookings />
          </RequireRole>
        ),
      },
      {
        path: 'bookings/:bookingId',
        element: (
          <RequireRole store={driverAuth} roles={['DRIVER']} loginPath="/book/login">
            <BookingPage />
          </RequireRole>
        ),
      },
    ],
  },
  { path: '*', element: <Navigate to="/" replace /> },
])
