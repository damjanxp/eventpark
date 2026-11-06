import { money } from './format'
import { fromMinor, toMinor } from './money'

test('amounts are parsed as text, never through floats', () => {
  expect(toMinor('1200')).toBe(120_000)
  expect(toMinor('0.1')).toBe(10)
  expect(toMinor('0.29')).toBe(29) // 0.29 * 100 = 28.999999999999996 in floating point
  expect(toMinor('12,5')).toBe(1_250)
  expect(() => toMinor('1.234')).toThrow()
  expect(() => toMinor('-5')).toThrow()
})

test('round trip and display', () => {
  expect(fromMinor(120_050)).toBe('1200.50')
  expect(toMinor(fromMinor(7))).toBe(7)
  expect(money(150_000)).toBe('1,500.00 RSD')
})
