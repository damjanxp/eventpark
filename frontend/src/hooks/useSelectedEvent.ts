import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router'
import { must, staffApi } from '@/api/client'
import type { EventOut } from '@/api/types'

const ORDER: Record<string, number> = { LIVE: 0, ENDED: 1, PUBLISHED: 2, DRAFT: 3, ARCHIVED: 4, CANCELLED: 5 }

/** The event in ?event=..., else the LIVE one, else the most relevant. Kept in the URL so links/reloads keep it. */
export function useSelectedEvent() {
  const [params, setParams] = useSearchParams()
  const events = useQuery({
    queryKey: ['events'],
    queryFn: () => must(staffApi.GET('/admin/v1/events', { params: { query: { limit: 200 } } })),
    select: (page) =>
      [...page.items].sort(
        (a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || b.starts_at.localeCompare(a.starts_at),
      ),
  })
  const list: EventOut[] = events.data ?? []
  const chosen = params.get('event')
  const event = list.find((e) => e.id === chosen) ?? list[0]
  const select = (id: string) =>
    setParams((p) => {
      p.set('event', id)
      return p
    })
  return { events: list, event, select, isLoading: events.isLoading, error: events.error }
}
