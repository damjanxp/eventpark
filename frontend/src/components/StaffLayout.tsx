import { CarFront, Gauge, History, LogOut, Radio, Settings } from 'lucide-react'
import { NavLink, Outlet } from 'react-router'
import { staffAuth } from '@/api/auth'
import { useAuth } from '@/api/useAuth'
import { cn } from '@/lib/cn'
import { MANAGERS, OPERATORS } from '@/lib/roles'

const links = [
  { to: '/dashboard', label: 'Dashboard', icon: Gauge, roles: null },
  { to: '/operator', label: 'Operator', icon: Radio, roles: OPERATORS.concat('PARKING_STAFF') },
  { to: '/history', label: 'History', icon: History, roles: null },
  { to: '/setup', label: 'Setup', icon: Settings, roles: MANAGERS },
] as const

export function StaffLayout() {
  const auth = useAuth(staffAuth)
  const role = auth.status === 'authenticated' ? auth.user.role : null
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-4 px-4">
          <span className="flex items-center gap-2 font-semibold">
            <CarFront className="size-5 text-brand" aria-hidden /> EventPark
          </span>
          <nav className="flex flex-1 gap-1 overflow-x-auto">
            {links
              .filter((l) => l.roles === null || (role && (l.roles as readonly string[]).includes(role)))
              .map(({ to, label, icon: Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-muted hover:bg-raised hover:text-ink',
                      isActive && 'bg-raised text-ink',
                    )
                  }
                >
                  <Icon className="size-4" aria-hidden />
                  <span className="hidden sm:inline">{label}</span>
                </NavLink>
              ))}
          </nav>
          {auth.status === 'authenticated' && (
            <div className="flex items-center gap-3 text-sm">
              <span className="hidden text-muted md:inline">
                {auth.user.full_name} · {auth.user.role.replace('_', ' ').toLowerCase()}
              </span>
              <button
                className="flex items-center gap-1 rounded-md px-2 py-1 text-muted hover:bg-raised hover:text-ink"
                onClick={() => void staffAuth.logout()}
              >
                <LogOut className="size-4" aria-hidden /> <span className="sr-only sm:not-sr-only">Sign out</span>
              </button>
            </div>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
