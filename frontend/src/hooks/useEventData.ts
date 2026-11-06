/** The dashboard's queries for one event. The keys match useEventStream's, so pushes land in the same cache. */
import { keepPreviousData, useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { must, staffApi } from '@/api/client'
import type { InsidePage } from '@/api/types'
import { pollInterval, queryKeys, type StreamStatus } from './useEventStream'

export function useVenue(venueId: string | undefined) {
  return useQuery({
    queryKey: ['venue', venueId],
    enabled: !!venueId,
    staleTime: 5 * 60_000,
    queryFn: () => must(staffApi.GET('/admin/v1/venues/{venue_id}', { params: { path: { venue_id: venueId! } } })),
  })
}

export function useOccupancy(eventId: string | undefined, status: StreamStatus) {
  return useQuery({
    queryKey: queryKeys.occupancy(eventId ?? ''),
    enabled: !!eventId,
    refetchInterval: pollInterval(status),
    queryFn: () => must(staffApi.GET('/admin/v1/events/{event_id}/occupancy', { params: { path: { event_id: eventId! } } })),
  })
}

export function useGateActivity(eventId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.activity(eventId ?? ''),
    enabled: !!eventId,
    refetchInterval: 10_000, // heartbeats ("online", "last seen") change without any passage, so always poll this one
    queryFn: () =>
      must(staffApi.GET('/admin/v1/events/{event_id}/gates/activity', { params: { path: { event_id: eventId! } } })),
  })
}

export function useHourly(eventId: string | undefined, status: StreamStatus) {
  return useQuery({
    queryKey: queryKeys.hourly(eventId ?? ''),
    enabled: !!eventId,
    refetchInterval: status === 'live' ? 30_000 : 5_000,
    queryFn: () =>
      must(staffApi.GET('/admin/v1/events/{event_id}/stats/hourly', { params: { path: { event_id: eventId! } } })),
  })
}

export function usePassages(eventId: string | undefined, status: StreamStatus) {
  return useQuery({
    queryKey: queryKeys.passages(eventId ?? ''),
    enabled: !!eventId,
    placeholderData: keepPreviousData,
    refetchInterval: pollInterval(status),
    queryFn: () =>
      must(
        staffApi.GET('/admin/v1/events/{event_id}/passages', {
          params: { path: { event_id: eventId! }, query: { limit: 30 } },
        }),
      ),
  })
}

export function useInside(eventId: string | undefined, zone: string | undefined) {
  return useInfiniteQuery({
    queryKey: [...queryKeys.inside(eventId ?? ''), zone ?? ''],
    enabled: !!eventId,
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last: InsidePage) => last.next_cursor ?? undefined,
    queryFn: ({ pageParam }): Promise<InsidePage> =>
      must(
        staffApi.GET('/admin/v1/events/{event_id}/inside', {
          params: { path: { event_id: eventId! }, query: { limit: 25, cursor: pageParam, zone: zone || undefined } },
        }),
      ),
  })
}
