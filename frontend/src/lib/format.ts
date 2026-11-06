/** Money is integer minor units (para) everywhere; only formatting divides by 100. */
export function money(minor: number | null | undefined, currency = 'RSD'): string {
  const value = minor ?? 0
  const units = Math.trunc(value / 100)
  const cents = Math.abs(value % 100)
  return `${units.toLocaleString('en-US')}.${String(cents).padStart(2, '0')} ${currency}`
}

/** Times are stored in UTC; the venue's time zone is used only for display. */
export function dateTime(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return '-'
  return new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short', timeZone }).format(new Date(iso))
}

export function time(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return '-'
  return new Intl.DateTimeFormat('en-GB', { timeStyle: 'medium', timeZone }).format(new Date(iso))
}

export function hour(iso: string, timeZone?: string): string {
  return new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone }).format(new Date(iso))
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'never'
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  return `${Math.round(seconds / 3600)} h ago`
}

export function duration(fromIso: string | null | undefined, now = Date.now()): string {
  if (!fromIso) return '-'
  const minutes = Math.max(0, Math.round((now - new Date(fromIso).getTime()) / 60_000))
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

export function requestId(): string {
  return crypto.randomUUID()
}
