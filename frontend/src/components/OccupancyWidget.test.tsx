import { render, screen } from '@testing-library/react'
import type { ZoneOccupancy } from '@/api/types'
import { OccupancyWidget } from './OccupancyWidget'

const z = (over: Partial<ZoneOccupancy>): ZoneOccupancy => ({
  capacity: 100,
  prebook_quota: 20,
  prebook_held: 8,
  prebook_inside: 3,
  drive_up_capacity: 80,
  drive_up_occupied: 40,
  drive_up_inside: 38,
  inside: 41,
  entering: 2,
  available_drive_up: 40,
  available_prebook: 12,
  ...over,
})

test('shows both pools per zone, sorted, with an accessible description', () => {
  render(<OccupancyWidget zones={{ B: z({}), A: z({ drive_up_occupied: 80, available_drive_up: 0 }) }} />)
  const names = screen.getAllByText(/^Zone [AB]$/).map((el) => el.textContent)
  expect(names).toEqual(['Zone A', 'Zone B'])
  expect(screen.getByText('Drive-up 80/80 · 0 free')).toBeInTheDocument()
  expect(screen.getAllByText(/3 in · 5 expected · 12 unsold/)).toHaveLength(2)
  expect(screen.getByRole('img', { name: /Zone B: drive-up 40 of 80/ })).toBeInTheDocument()
})

test('empty event', () => {
  render(<OccupancyWidget zones={{}} />)
  expect(screen.getByText(/No zones configured/)).toBeInTheDocument()
})
