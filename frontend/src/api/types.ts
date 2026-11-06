import type { components } from './schema'

type S = components['schemas']
export type EventOut = S['EventOut']
export type EventDetail = S['EventDetailOut']
export type Occupancy = S['OccupancyOut']
export type GateActivity = S['GateActivityOut']
export type PassageRow = S['PassageRowOut']
export type InsideRow = S['InsideOut']
export type InsidePage = S['Page_InsideOut_']
export type HourlyStats = S['HourlyStatsOut']
export type LookupResult = S['LookupOut']
export type History = S['HistoryOut']
export type Venue = S['VenueOut']
export type Gate = S['GateOut']
export type Zone = S['eventpark__api__admin__venues__ZoneOut']
export type Device = S['DeviceOut']
export type Report = S['ReportOut']
export type Document = S['DocumentOut']
export type PublicEvent = S['PublicEvent']
export type PublicEventDetail = S['PublicEventDetail']
export type PublicZone = S['eventpark__api__public__ZoneOut']
export type Booking = S['BookingOut']
export type Space = S['eventpark__api__public__SpaceOut']

/** One zone in the occupancy read model (cache/read_model.py). */
export type ZoneOccupancy = {
  capacity: number
  prebook_quota: number
  prebook_held: number
  prebook_inside: number
  drive_up_capacity: number
  drive_up_occupied: number
  drive_up_inside: number
  inside: number
  entering: number
  available_drive_up: number
  available_prebook: number
}
