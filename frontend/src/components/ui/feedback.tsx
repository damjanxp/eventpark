import type { ReactNode } from 'react'
import { ApiError } from '@/api/auth'

export function ErrorMessage({ error }: { error: unknown }) {
  if (!error) return null
  const message = error instanceof Error ? error.message : String(error)
  const id = error instanceof ApiError ? error.requestId : undefined
  return (
    <p role="alert" className="rounded-md border border-bad/30 bg-bad/10 px-3 py-2 text-sm text-bad">
      {message}
      {id && <span className="ml-2 font-mono text-xs opacity-70">request {id}</span>}
    </p>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-muted">{children}</p>
}

export function Loading() {
  return <p className="py-6 text-center text-sm text-muted">Loading…</p>
}
