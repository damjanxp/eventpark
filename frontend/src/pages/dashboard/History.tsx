import { useQuery } from '@tanstack/react-query'
import { must, staffApi } from '@/api/client'
import { Card, CardBody, CardHeader } from '@/components/ui/card'
import { Empty, ErrorMessage, Loading } from '@/components/ui/feedback'
import { Table, Td, Th } from '@/components/ui/table'
import { dateTime, money } from '@/lib/format'

/** Past events: summaries computed once at archive time (event_summaries), so this page never scans passages. */
export function HistoryPage() {
  const history = useQuery({ queryKey: ['history'], queryFn: () => must(staffApi.GET('/admin/v1/history/events')) })
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Event history</h1>
      <Card>
        <CardHeader title="Archived events" sub="peak occupancy, totals and revenue per event" />
        <CardBody className="p-0">
          {history.isLoading ? (
            <Loading />
          ) : history.error ? (
            <div className="p-4">
              <ErrorMessage error={history.error} />
            </div>
          ) : !history.data?.length ? (
            <Empty>No archived events yet. An event gets a summary when it is archived.</Empty>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Event</Th>
                  <Th>Date</Th>
                  <Th className="text-right">Peak</Th>
                  <Th className="text-right">Entries</Th>
                  <Th className="text-right">Exits</Th>
                  <Th className="text-right">Revenue</Th>
                  <Th className="text-right">Avg stay</Th>
                  <Th className="text-right">Busiest hour</Th>
                  <Th className="text-right">Rejections</Th>
                </tr>
              </thead>
              <tbody className="tabular">
                {history.data.map((h) => (
                  <tr key={h.event_id}>
                    <Td className="font-medium">{h.name}</Td>
                    <Td>{dateTime(h.starts_at)}</Td>
                    <Td className="text-right">{h.peak_occupancy}</Td>
                    <Td className="text-right">{h.total_entries}</Td>
                    <Td className="text-right">{h.total_exits}</Td>
                    <Td className="text-right">{money(h.revenue_minor)}</Td>
                    <Td className="text-right">{h.avg_stay_minutes == null ? '-' : `${Math.round(h.avg_stay_minutes)} min`}</Td>
                    <Td className="text-right">{h.busiest_hour ? dateTime(h.busiest_hour) : '-'}</Td>
                    <Td className="text-right">{h.rejection_rate == null ? '-' : `${(h.rejection_rate * 100).toFixed(1)}%`}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </CardBody>
      </Card>
    </div>
  )
}
