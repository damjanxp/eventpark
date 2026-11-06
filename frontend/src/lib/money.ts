/**
 * Money in forms: the user types "1200.50" (RSD), the API wants 120050 (para). Parsed as text, never through a
 * float: 0.1 + 0.2 !== 0.3 in floating point, and money must be exact.
 */
export function toMinor(input: string): number {
  const match = /^\s*(\d+)(?:[.,](\d{1,2}))?\s*$/.exec(input)
  if (!match) throw new Error(`Not an amount: "${input}"`)
  const units = Number.parseInt(match[1], 10)
  const cents = Number.parseInt((match[2] ?? '0').padEnd(2, '0'), 10)
  return units * 100 + cents
}

export function fromMinor(minor: number): string {
  return `${Math.trunc(minor / 100)}.${String(Math.abs(minor % 100)).padStart(2, '0')}`
}
