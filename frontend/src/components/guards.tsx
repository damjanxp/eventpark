import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'
import type { AuthStore, Role } from '@/api/auth'
import { useAuth } from '@/api/useAuth'
import { Loading } from './ui/feedback'

/**
 * Route guard. UX only: hiding a page is NOT security. The API checks the role on every request
 * (require_roles in api/deps.py), so a user who edits this check still gets 403s.
 */
export function RequireRole({
  store,
  roles,
  loginPath,
  children,
}: {
  store: AuthStore
  roles: Role[]
  loginPath: string
  children: ReactNode
}) {
  const auth = useAuth(store)
  const location = useLocation()
  if (auth.status === 'loading') return <Loading />
  if (auth.status === 'anonymous') return <Navigate to={loginPath} state={{ from: location.pathname }} replace />
  if (!roles.includes(auth.user.role)) {
    return (
      <div className="mx-auto max-w-md p-8 text-center">
        <h1 className="text-lg font-semibold">Not for your role</h1>
        <p className="mt-2 text-sm text-muted">
          You are signed in as {auth.user.role.replace('_', ' ').toLowerCase()}; this area needs {roles.join(' or ')}.
        </p>
      </div>
    )
  }
  return children
}
