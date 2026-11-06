/**
 * Live updates for one event over WebSocket (docs/02 §3.7 + §13).
 *
 *   1. POST /admin/v1/ws-ticket          -> one-time ticket (browsers can't put a bearer token on a WebSocket)
 *   2. WS /ws/v1/events/{id}?ticket=...  -> first message: the full occupancy; then pushes
 *   3. Each push PATCHES the TanStack Query cache, so components read the same query whether its data came from
 *      REST or the socket, and never need to know which.
 *   4. Socket drops -> status 'polling': queries refetch every 5 s (usePollInterval) while we reconnect with
 *      exponential backoff + jitter (1 s, 2 s, 4 s ... 30 s), so a server restart doesn't get a thundering herd.
 *
 * Messages (workers/stats.py): {type: 'occupancy', zones} | {type: 'passage', ...} | {type: 'alert', ...} | {type: 'ping'}
 */
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { staffApi } from '@/api/client'
import type { Occupancy, ZoneOccupancy } from '@/api/types'

export type StreamStatus = 'connecting' | 'live' | 'polling'

export type LiveAlert = {
  id: string
  at: number
  alert: string
  zone?: string
  level?: string
  gate_code?: string
  printer_status?: string
  text: string
}

type Message =
  | { type: 'occupancy'; event_id: string; zones: Record<string, ZoneOccupancy>; source?: string }
  | { type: 'passage'; event: string; [key: string]: unknown }
  | ({ type: 'alert'; alert: string } & Record<string, unknown>)
  | { type: 'ping' }

const TOTAL_KEYS = [
  'capacity',
  'inside',
  'entering',
  'drive_up_occupied',
  'prebook_held',
  'available_drive_up',
  'available_prebook',
] as const

/** Same totals the REST endpoint computes (api/admin/events.py occupancy). */
export function totalsOf(zones: Record<string, ZoneOccupancy>): Record<string, number> {
  return Object.fromEntries(
    TOTAL_KEYS.map((k) => [k, Object.values(zones).reduce((sum, z) => sum + (Number(z[k]) || 0), 0)]),
  )
}

export function backoffMs(attempt: number, random = Math.random): number {
  const base = Math.min(30_000, 1_000 * 2 ** attempt)
  return Math.round(base / 2 + random() * (base / 2)) // "equal jitter": between half and the full delay
}

export const queryKeys = {
  occupancy: (id: string) => ['occupancy', id] as const,
  passages: (id: string) => ['passages', id] as const,
  activity: (id: string) => ['activity', id] as const,
  inside: (id: string) => ['inside', id] as const,
  hourly: (id: string) => ['hourly', id] as const,
}

/** Apply one message to the query cache. Exported for tests. */
export function applyMessage(client: QueryClient, eventId: string, message: Message): LiveAlert | null {
  switch (message.type) {
    case 'occupancy':
      client.setQueryData<Occupancy>(queryKeys.occupancy(eventId), {
        event_id: eventId,
        zones: message.zones as Occupancy['zones'],
        totals: totalsOf(message.zones),
        source: 'live',
      })
      return null
    case 'passage':
      // The pushed passage has ids, not the joined fields the tables show (gate code, zone): re-fetch those lists.
      // invalidateQueries only refetches queries a component is currently showing.
      for (const key of [queryKeys.passages, queryKeys.activity, queryKeys.inside, queryKeys.hourly]) {
        void client.invalidateQueries({ queryKey: key(eventId) })
      }
      return null
    case 'alert':
      return toAlert(message)
    default:
      return null
  }
}

function toAlert(m: { alert: string } & Record<string, unknown>): LiveAlert {
  const zone = m.zone as string | undefined
  const level = m.level as string | undefined
  const gate = m.gate_code as string | undefined
  let text = m.alert
  if (m.alert === 'zone_threshold') text = level === 'full' ? `Zone ${zone} is full` : `Zone ${zone} is ${level}% full`
  if (m.alert === 'printer') text = `Printer at ${gate}: ${String(m.printer_status).replace('_', ' ').toLowerCase()}`
  if (m.alert === 'device_offline') text = `Gate ${gate} is offline`
  return { id: crypto.randomUUID(), at: Date.now(), alert: m.alert, zone, level, gate_code: gate, text }
}

export function useEventStream(eventId: string | undefined): { status: StreamStatus; alerts: LiveAlert[] } {
  const client = useQueryClient()
  // State is tagged with the event it belongs to: switching events shows 'connecting' and no stale alerts
  // without resetting state inside the effect.
  const [stream, setStream] = useState<{ eventId?: string; status: StreamStatus; alerts: LiveAlert[] }>({
    status: 'connecting',
    alerts: [],
  })

  useEffect(() => {
    if (!eventId) return
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | undefined
    let attempt = 0
    let disposed = false
    const setStatus = (status: StreamStatus) =>
      setStream((s) => (s.eventId === eventId ? { ...s, status } : { eventId, status, alerts: [] }))
    const addAlert = (alert: LiveAlert) =>
      setStream((s) => ({ ...s, eventId, alerts: [alert, ...(s.eventId === eventId ? s.alerts : [])].slice(0, 20) }))

    const scheduleReconnect = () => {
      if (disposed) return
      setStatus('polling')
      retry = setTimeout(connect, backoffMs(attempt++))
    }

    async function connect() {
      if (disposed) return
      const { data, response } = await staffApi.POST('/admin/v1/ws-ticket').catch(() => ({ data: undefined, response: null }))
      if (disposed) return
      if (!data || !response?.ok) return scheduleReconnect() // 503 = Redis down: poll until it's back
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${location.host}/ws/v1/events/${eventId}?ticket=${encodeURIComponent(data.ticket)}`)
      socket.onopen = () => {
        attempt = 0
        setStatus('live')
      }
      socket.onmessage = (e) => {
        let message: Message
        try {
          message = JSON.parse(e.data as string)
        } catch {
          return
        }
        const alert = applyMessage(client, eventId!, message)
        if (alert) addAlert(alert)
      }
      socket.onclose = () => {
        socket = null
        scheduleReconnect()
      }
    }

    void connect()
    return () => {
      disposed = true
      clearTimeout(retry)
      socket?.close()
    }
  }, [client, eventId])

  if (stream.eventId !== eventId) return { status: 'connecting', alerts: [] }
  return { status: stream.status, alerts: stream.alerts }
}

/** refetchInterval for queries that the stream keeps fresh: none while live, 5 s while polling. */
export function pollInterval(status: StreamStatus): number | false {
  return status === 'live' ? false : 5_000
}
