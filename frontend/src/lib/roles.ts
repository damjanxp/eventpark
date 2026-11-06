import type { Role } from '@/api/auth'

export const STAFF = ['ADMIN', 'EVENT_MANAGER', 'GATE_OPERATOR', 'PARKING_STAFF'] as Role[]
export const OPERATORS = ['ADMIN', 'GATE_OPERATOR'] as Role[]
export const MANAGERS = ['ADMIN', 'EVENT_MANAGER'] as Role[]

export function homeFor(role: Role): string {
  if (role === 'DRIVER') return '/book'
  if (role === 'GATE_OPERATOR') return '/operator'
  return '/dashboard'
}
