const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
const DASH = 45
const ZERO = 48

/** Parses `len` ASCII digits at `from`, or returns -1. */
function digits(s: string, from: number, len: number): number {
  let v = 0
  for (let i = from; i < from + len; i++) {
    const d = s.charCodeAt(i) - ZERO
    if (!(d >= 0 && d <= 9)) return -1
    v = v * 10 + d
  }
  return v
}

/** True for a real calendar date written as YYYY-MM-DD. */
export function isIsoDate(value: string): boolean {
  if (
    value.length !== 10 ||
    value.charCodeAt(4) !== DASH ||
    value.charCodeAt(7) !== DASH
  )
    return false
  const year = digits(value, 0, 4)
  const month = digits(value, 5, 2)
  const day = digits(value, 8, 2)
  if (year < 0 || month < 1 || month > 12 || day < 1) return false
  const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0
  const max = month === 2 && leap ? 29 : (DAYS_IN_MONTH[month - 1] ?? 0)
  return day <= max
}
