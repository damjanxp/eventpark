import { QueryClient } from '@tanstack/react-query'
import type { Occupancy, ZoneOccupancy } from '@/api/types'
import { applyMessage, backoffMs, queryKeys, totalsOf } from './useEventStream'

const zone = (inside: number): ZoneOccupancy => ({
  capacity: 100,
  prebook_quota: 20,
  prebook_held: 5,
  prebook_inside: 2,
  drive_up_capacity: 80,
  drive_up_occupied: inside,
  drive_up_inside: inside,
  inside,
  entering: 0,
  available_drive_up: 80 - inside,
  available_prebook: 15,
})

test('backoff grows exponentially, is capped at 30 s and jittered', () => {
  expect(backoffMs(0, () => 0)).toBe(500)
  expect(backoffMs(0, () => 1)).toBe(1_000)
  expect(backoffMs(3, () => 1)).toBe(8_000)
  expect(backoffMs(20, () => 1)).toBe(30_000)
  expect(backoffMs(20, () => 0)).toBe(15_000)
})

test('an occupancy push replaces the cached snapshot (absolute values, like the read model)', () => {
  const client = new QueryClient()
  applyMessage(client, 'e1', { type: 'occupancy', event_id: 'e1', zones: { A: zone(10), B: zone(5) } })
  const cached = client.getQueryData<Occupancy>(queryKeys.occupancy('e1'))
  expect(cached?.totals.inside).toBe(15)
  expect(cached?.totals.capacity).toBe(200)
  expect(cached?.source).toBe('live')
})

test('a passage push invalidates the lists that show joined data', async () => {
  const client = new QueryClient()
  client.setQueryData(queryKeys.passages('e1'), { items: [], next_cursor: null })
  client.setQueryData(queryKeys.passages('e2'), { items: [], next_cursor: null })
  applyMessage(client, 'e1', { type: 'passage', event: 'passage.completed' })
  expect(client.getQueryState(queryKeys.passages('e1'))?.isInvalidated).toBe(true)
  expect(client.getQueryState(queryKeys.passages('e2'))?.isInvalidated).toBe(false) // other events untouched
})

test('alerts become readable text', () => {
  const client = new QueryClient()
  const alert = applyMessage(client, 'e1', { type: 'alert', alert: 'zone_threshold', zone: 'A', level: '90' })
  expect(alert?.text).toBe('Zone A is 90% full')
  expect(applyMessage(client, 'e1', { type: 'ping' })).toBeNull()
})

test('totals sum every zone', () => {
  expect(totalsOf({ A: zone(1), B: zone(2) }).available_drive_up).toBe(157)
})
