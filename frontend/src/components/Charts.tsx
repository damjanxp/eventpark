/**
 * Hourly charts. One measure per axis (no dual axes): counts in one chart, money in another.
 * Series colors are fixed by entity (entries = slot 1, exits = slot 2), a legend is always shown for 2 series,
 * the tooltip gives exact values, and a table view sits underneath for screen readers / exact reading.
 */
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { HourlyStats } from '@/api/types'
import { hour, money } from '@/lib/format'
import { Table, Td, Th } from './ui/table'

const axis = { stroke: 'var(--color-muted)', fontSize: 12, tickLine: false, axisLine: false }
const tooltipStyle = {
  contentStyle: {
    background: 'var(--color-surface)',
    border: '1px solid var(--color-line)',
    borderRadius: 8,
    fontSize: 12,
    color: 'var(--color-ink)',
  },
  cursor: { stroke: 'var(--color-line)', fill: 'var(--color-raised)' },
}

type Row = HourlyStats['hours'][number] & { label: string; revenue: number }

function rows(stats: HourlyStats, tz?: string): Row[] {
  return stats.hours.map((h) => ({ ...h, label: hour(h.hour_start, tz), revenue: h.revenue_minor / 100 }))
}

export function EntriesExitsChart({ stats, tz }: { stats: HourlyStats; tz?: string }) {
  const data = rows(stats, tz)
  return (
    <div className="h-64" role="img" aria-label="Entries and exits per hour (table below)">
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 12, left: -16, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--color-line)" />
          <XAxis dataKey="label" {...axis} />
          <YAxis allowDecimals={false} {...axis} />
          <Tooltip {...tooltipStyle} />
          <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
          <Line name="Entries" dataKey="entries" stroke="var(--color-series-1)" strokeWidth={2} dot={{ r: 4 }} activeDot={{ r: 5 }} />
          <Line name="Exits" dataKey="exits" stroke="var(--color-series-2)" strokeWidth={2} dot={{ r: 4 }} activeDot={{ r: 5 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export function RevenueChart({ stats, tz, currency = 'RSD' }: { stats: HourlyStats; tz?: string; currency?: string }) {
  const data = rows(stats, tz)
  return (
    <div className="h-48" role="img" aria-label="Revenue per hour (table below)">
      <ResponsiveContainer>
        <BarChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--color-line)" />
          <XAxis dataKey="label" {...axis} />
          <YAxis {...axis} />
          <Tooltip {...tooltipStyle} formatter={(v) => [`${Number(v).toLocaleString('en-US')} ${currency}`, 'Revenue']} />
          <Bar dataKey="revenue" fill="var(--color-series-1)" radius={[4, 4, 0, 0]} maxBarSize={28} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

export function HourlyTable({ stats, tz, currency = 'RSD' }: { stats: HourlyStats; tz?: string; currency?: string }) {
  return (
    <details className="mt-3 text-sm">
      <summary className="cursor-pointer text-xs text-muted">Show as table</summary>
      <Table className="mt-2">
        <thead>
          <tr>
            <Th>Hour</Th>
            <Th className="text-right">Entries</Th>
            <Th className="text-right">Exits</Th>
            <Th className="text-right">Rejections</Th>
            <Th className="text-right">Revenue</Th>
            <Th className="text-right">Released unpaid</Th>
            <Th className="text-right">Avg decision</Th>
          </tr>
        </thead>
        <tbody className="tabular">
          {stats.hours.map((h) => (
            <tr key={h.hour_start}>
              <Td>{hour(h.hour_start, tz)}</Td>
              <Td className="text-right">{h.entries}</Td>
              <Td className="text-right">{h.exits}</Td>
              <Td className="text-right">{h.rejections}</Td>
              <Td className="text-right">{money(h.revenue_minor, currency)}</Td>
              <Td className="text-right">{money(h.released_unpaid_minor, currency)}</Td>
              <Td className="text-right">{h.avg_decision_ms == null ? '-' : `${Math.round(h.avg_decision_ms)} ms`}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </details>
  )
}
